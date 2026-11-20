"""Background job manager for ``flow`` runs.

Reservoir simulations are long. The MCP tool layer cannot block while a 4-hour
run completes, so ``start_flow_job`` registers the run and returns immediately;
subsequent tool calls (``flow_status``, ``flow_log_tail``, ``flow_cancel``)
operate on the returned job id.

Three properties matter here:

* **Bounded concurrency.** ``clone_deck`` makes it trivial to launch five
  variants in one breath; running them all at once thrashes a workstation.
  At most ``SETTINGS.max_parallel`` jobs run; the rest wait in state
  ``"queued"`` and are launched as slots free up.

* **Restart safety.** The bash wrapper records the **Linux** ``flow`` PID
  (``flow.pid`` — written via ``$$`` then ``exec``, because ``$!``/``$BASHPID``
  are unreliable under ``wsl.exe``). Jobs are rehydrated lazily from
  ``job.json`` files, so a run started before a server restart can still be
  polled and cancelled (via ``wsl kill``); queued jobs resume.

* **Provenance.** Each job records the flow version and the SHA-256 of the
  top-level deck, so "which deck produced this curve?" stays answerable long
  after a sensitivity study is done.

Hardening in the sean-leneave fork:

* **Fixed run environment.** Flow starts under ``env -i`` with a short, explicit
  environment (``run_env``, recorded in ``job.json``) and a non-login,
  non-interactive bash, so shell profiles cannot change a run's PATH, thread
  counts or MPI settings between runs.
* **Option policy.** ``extra_args`` go through :func:`guard.validate_flow_extra_args`
  and the core budget through :func:`guard.check_cores` before anything starts.
* **Publishing.** When ``OPM_MCP_PUBLISH`` is set, a completed job's case files,
  log and ``job.json`` are copied to ``<publish>/<case>-<job_id>/`` for viewing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import guard, wsl
from .config import SETTINGS

_JOB_LOCK = threading.RLock()
_JOBS: dict[str, "FlowJob"] = {}
_LOADED = False
_FLOW_VERSION: str | None = None

_PID_FILE = "flow.pid"
_LOG_FILE = "flow.log"


@dataclass
class FlowJob:
    job_id: str
    deck_path_win: str
    deck_path_wsl: str
    case_name: str
    workdir_win: str
    workdir_wsl: str
    argv: list[str]
    log_path: str
    created_at: str
    output_dir_wsl: str
    started_at: Optional[str] = None
    pid: Optional[int] = None  # Windows-side launcher PID (wsl.exe / bash)
    wsl_pid: Optional[int] = None  # Linux-side flow PID (for cross-restart kill)
    pid_identity: Optional[str] = None  # "<boot_id>:<start_time>" of wsl_pid; survives PID reuse
    finished_at: Optional[str] = None
    returncode: Optional[int] = None
    state: str = "queued"  # queued | running | completed | failed | cancelled | unknown
    error: Optional[str] = None
    last_progress: Optional[str] = None
    flow_version: Optional[str] = None
    deck_sha256: Optional[str] = None
    run_env: dict = field(default_factory=dict)
    published_to: Optional[str] = None
    publish_error: Optional[str] = None
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _publishing: bool = field(default=False, repr=False)

    def to_dict(self) -> dict:
        # ``asdict`` deepcopies, which trips over the Popen handle's internal
        # thread lock. Serialise the non-process fields by hand instead.
        return {
            "job_id": self.job_id,
            "deck_path_win": self.deck_path_win,
            "deck_path_wsl": self.deck_path_wsl,
            "case_name": self.case_name,
            "workdir_win": self.workdir_win,
            "workdir_wsl": self.workdir_wsl,
            "argv": list(self.argv),
            "log_path": self.log_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "output_dir_wsl": self.output_dir_wsl,
            "pid": self.pid,
            "wsl_pid": self.wsl_pid,
            "pid_identity": self.pid_identity,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "state": self.state,
            "error": self.error,
            "last_progress": self.last_progress,
            "flow_version": self.flow_version,
            "deck_sha256": self.deck_sha256,
            "run_env": dict(self.run_env),
            "published_to": self.published_to,
            "publish_error": self.publish_error,
        }

    @property
    def pidfile_path(self) -> Path:
        return Path(self.workdir_win) / _PID_FILE


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_metadata(job: FlowJob) -> None:
    meta_path = Path(job.workdir_win) / "job.json"
    meta_path.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Starting / queueing jobs
# ---------------------------------------------------------------------------


def start_flow_job(
    deck_path: str,
    *,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
    mpi_processes: int | None = None,
) -> FlowJob:
    """Register a ``flow`` run; it launches now or queues if all slots are busy.

    Parameters
    ----------
    deck_path : str
        Windows or WSL path to the ECLIPSE DATA deck.
    extra_args : list[str] | None
        Additional ``flow`` flags appended verbatim after the deck name.
    output_dir : str | None
        Where ``flow`` should write its outputs (``--output-dir=``). When
        ``None`` we create a per-job directory under the server work dir so the
        deck's source folder is left clean.
    threads : int | None
        Convenience for ``--threads-per-process=N`` (per MPI rank when
        combined with ``mpi_processes``).
    convergence_info : str | None
        Value for ``--output-extra-convergence-info``. Defaults to writing
        INFOSTEP+INFOITER so ``flow_convergence_report`` has data; ``"none"``
        disables.
    mpi_processes : int | None
        Run distributed via ``mpirun -np N``. Requires Open MPI in the distro.
    """
    _ensure_loaded()
    # Guardrails first: nothing is created or launched for a refused request.
    guard.confine(deck_path, what="deck_path")
    if output_dir:
        guard.confine(output_dir, what="output_dir")
    accepted_extra = guard.validate_flow_extra_args(extra_args)
    t_check = threads if threads is not None else SETTINGS.flow_default_threads
    guard.check_cores(mpi_processes, t_check)

    deck_wsl = wsl.to_wsl_path(deck_path)
    case_name = Path(deck_wsl).stem

    job_id = uuid.uuid4().hex[:12]
    workdir_win = SETTINGS.work_dir / "jobs" / job_id
    workdir_win.mkdir(parents=True, exist_ok=True)
    workdir_wsl = wsl.to_wsl_path(str(workdir_win))

    out_dir_wsl = wsl.to_wsl_path(output_dir) if output_dir else f"{workdir_wsl}/out"

    argv: list[str] = []
    if mpi_processes and mpi_processes > 1:
        _require_mpirun()
        argv += ["mpirun", "-np", str(int(mpi_processes))]
    argv += [SETTINGS.flow_bin, deck_wsl, f"--output-dir={out_dir_wsl}"]
    t = threads if threads is not None else SETTINGS.flow_default_threads
    if t and t > 0:
        argv.append(f"--threads-per-process={t}")
    if convergence_info and convergence_info.lower() != "none":
        argv.append(f"--output-extra-convergence-info={convergence_info}")
    if accepted_extra:
        argv.extend(accepted_extra)

    deck_win = wsl.to_windows_path(deck_wsl)
    job = FlowJob(
        job_id=job_id,
        deck_path_win=deck_win,
        deck_path_wsl=deck_wsl,
        case_name=case_name,
        workdir_win=str(workdir_win),
        workdir_wsl=workdir_wsl,
        argv=argv,
        log_path=str(workdir_win / _LOG_FILE),
        created_at=_utcnow(),
        output_dir_wsl=out_dir_wsl,
        deck_sha256=_sha256_of(deck_win),
    )
    with _JOB_LOCK:
        _JOBS[job_id] = job
        _prune_locked()
    _save_metadata(job)
    _pump()
    return job


def _require_mpirun() -> None:
    res = wsl.run(["bash", "-lc", "command -v mpirun || true"], check=False, timeout=15)
    if not res.stdout.strip():
        raise wsl.WSLError(
            "mpirun not found in the distro. Install Open MPI "
            "(e.g. `sudo apt-get install openmpi-bin`) or drop mpi_processes."
        )


def _sha256_of(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _pump() -> None:
    """Launch queued jobs while free slots remain (oldest first)."""
    with _JOB_LOCK:
        running = sum(1 for j in _JOBS.values() if j.state == "running")
        queued = sorted(
            (j for j in _JOBS.values() if j.state == "queued"),
            key=lambda j: j.created_at,
        )
        for job in queued:
            if running >= SETTINGS.max_parallel:
                break
            _launch_locked(job)
            running += 1


def _launch_locked(job: FlowJob) -> None:
    log_path_wsl = wsl.to_wsl_path(job.log_path)
    pid_wsl = wsl.to_wsl_path(str(job.pidfile_path))

    # Write the shell's own PID, then ``exec`` flow so it inherits that exact
    # PID. Two payoffs: the PID file holds flow's real Linux PID (cross-restart
    # kill), and because the launcher's child *becomes* flow, ``Popen.wait()``
    # returns flow's true exit code. (``$!``/``$BASHPID`` are unreliable under
    # wsl.exe, so we deliberately avoid them.)
    flow_cmd = " ".join(shlex.quote(a) for a in job.argv)
    bash_line = (
        f"mkdir -p {shlex.quote(job.output_dir_wsl)} && "
        f"echo $$ > {shlex.quote(pid_wsl)} && "
        f"exec {flow_cmd} > {shlex.quote(log_path_wsl)} 2>&1"
    )
    run_env = _run_environment()
    job.run_env = run_env
    launcher = (
        ["env", "-i"]
        + [f"{k}={v}" for k, v in sorted(run_env.items())]
        + ["bash", "--noprofile", "--norc", "-c", bash_line]
    )
    proc = wsl.popen(launcher, stdout_path=os.devnull, merge_stderr=True)
    job._proc = proc
    job.pid = proc.pid
    job.state = "running"
    job.started_at = _utcnow()
    _save_metadata(job)
    threading.Thread(target=_watch, args=(job,), daemon=True).start()


def _watch(job: FlowJob) -> None:
    proc = job._proc
    if proc is None:
        return
    if job.wsl_pid is None:
        job.wsl_pid = _read_pidfile(job.pidfile_path, timeout=5.0)
    if job.wsl_pid is not None and job.pid_identity is None:
        job.pid_identity = wsl.wsl_pids_identity([job.wsl_pid]).get(int(job.wsl_pid))
    if job.flow_version is None:
        job.flow_version = _get_flow_version()
    _save_metadata(job)
    # Because the wrapper ``exec``s flow, the launcher's child *is* flow, so
    # its return code is flow's true exit status.
    rc = proc.wait()
    with _JOB_LOCK:
        job.returncode = rc
        job.finished_at = _utcnow()
        if job.state == "cancelled":
            pass
        elif rc == 0:
            job.state = "completed"
        else:
            job.state = "failed"
            job.error = _scan_log_for_fatal(job.log_path)
        _save_metadata(job)
    if job.state == "completed":
        _publish(job)
    _pump()


_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_LINUX_HOME: str | None = None


def _linux_home() -> str:
    """HOME of the Linux user that runs flow (local on POSIX, queried once via WSL)."""
    global _LINUX_HOME
    if _LINUX_HOME is None:
        if os.name != "nt":
            _LINUX_HOME = os.path.expanduser("~")
        else:
            res = wsl.run(["sh", "-c", 'printf %s "$HOME"'], check=False, timeout=15)
            _LINUX_HOME = res.stdout.strip() or "/tmp"
    return _LINUX_HOME


def _run_environment() -> dict[str, str]:
    """The complete, explicit environment flow runs with (recorded per job).

    Deliberately excludes OMP_NUM_THREADS and MPI variables: thread counts are
    passed as flow options (``--threads-per-process``) so they appear in argv.
    """
    path = _BASE_PATH
    flow_dir = os.path.dirname(SETTINGS.flow_bin) if SETTINGS.flow_bin.startswith("/") else ""
    if flow_dir and flow_dir not in path.split(":"):
        path = f"{flow_dir}:{path}"
    env = {"PATH": path, "HOME": _linux_home(), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    user = os.environ.get("USER") if os.name != "nt" else None
    if user:
        env["USER"] = user
    return env


_PUBLISH_SUFFIXES = (
    "EGRID", "INIT", "UNRST", "SMSPEC", "UNSMRY", "ESMRY", "PRT", "DBG", "RFT", "INFOSTEP", "INFOITER",
)


def _publish(job: FlowJob) -> None:
    """Copy a completed job's case files, log and job.json to the publish dir.

    Failures are recorded on the job (``publish_error``) and never change its
    state: the simulation itself succeeded.
    """
    if SETTINGS.publish_dir is None:
        return
    with _JOB_LOCK:  # the watcher thread and a status poll can both get here
        if job.published_to or job._publishing:
            return
        job._publishing = True
    try:
        src = Path(wsl.to_windows_path(job.output_dir_wsl))
        dest = SETTINGS.publish_dir / f"{job.case_name}-{job.job_id}"
        guard.confine(dest, what="publish destination")
        dest.mkdir(parents=True, exist_ok=False)
        for suffix in _PUBLISH_SUFFIXES:
            f = src / f"{job.case_name}.{suffix}"
            if f.is_file():
                shutil.copy2(f, dest / f.name)
        for extra in (Path(job.log_path), Path(job.workdir_win) / "job.json"):
            if extra.is_file():
                shutil.copy2(extra, dest / extra.name)
        with _JOB_LOCK:
            job.published_to = str(dest)
            job.publish_error = None
    except Exception as exc:  # noqa: BLE001 - publishing must not break the job
        with _JOB_LOCK:
            job.publish_error = f"{type(exc).__name__}: {exc}"
    finally:
        with _JOB_LOCK:
            job._publishing = False
    _save_metadata(job)
    # Refresh the published copy of job.json so it carries published_to.
    if job.published_to:
        try:
            shutil.copy2(Path(job.workdir_win) / "job.json", Path(job.published_to) / "job.json")
        except OSError:
            pass


def _get_flow_version() -> str | None:
    global _FLOW_VERSION
    if _FLOW_VERSION is not None:
        return _FLOW_VERSION
    try:
        res = wsl.run([SETTINGS.flow_bin, "--version"], check=False, timeout=20)
        line = (res.stdout or res.stderr).strip().splitlines()
        _FLOW_VERSION = line[0] if line else None
    except Exception:
        _FLOW_VERSION = None
    return _FLOW_VERSION


def _read_pidfile(path: Path, timeout: float = 5.0) -> int | None:
    """Poll briefly for the bash wrapper to write flow's PID."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            txt = path.read_text(encoding="utf-8").strip()
            if txt:
                return int(txt)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.1)
    return None


def _classify_finished(log_path: str) -> tuple[str, str | None]:
    """Classify a job that finished while we weren't watching (post-restart)."""
    fatal = _scan_log_for_fatal(log_path)
    return ("failed", fatal) if fatal else ("completed", None)


def _scan_log_for_fatal(log_path: str) -> str | None:
    for line in reversed(_read_tail(log_path, 200)):
        if any(tok in line for tok in ("FATAL", "Error:", "ERROR", "Aborted", "exception")):
            return line.strip()
    return None


# ---------------------------------------------------------------------------
# Querying jobs
# ---------------------------------------------------------------------------


def get_job(job_id: str) -> FlowJob:
    _ensure_loaded()
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise KeyError(f"Unknown job id: {job_id}")
    _refresh_state(job)
    progress = _last_progress_line(job.log_path)
    with _JOB_LOCK:
        job.last_progress = progress
    return job


def queue_position(job: FlowJob) -> int | None:
    """1-based position in the launch queue, or None if not queued."""
    if job.state != "queued":
        return None
    with _JOB_LOCK:
        queued = sorted(
            (j for j in _JOBS.values() if j.state == "queued"),
            key=lambda j: j.created_at,
        )
        for i, j in enumerate(queued, start=1):
            if j.job_id == job.job_id:
                return i
    return None


def _liveness(items: list[FlowJob]) -> dict[str, bool]:
    """Is each job's Flow process still the one we started? (one round-trip for all)

    Jobs with a recorded ``pid_identity`` are alive only if the PID exists *and* has
    the same boot id and start time, so a reboot or PID reuse never makes a dead run
    look alive. Jobs recorded before identities existed fall back to a bare PID check.
    """
    items = [j for j in items if j.wsl_pid]
    if not items:
        return {}
    idents = wsl.wsl_pids_identity([j.wsl_pid for j in items])
    out: dict[str, bool] = {}
    legacy: list[FlowJob] = []
    for j in items:
        if j.pid_identity and idents:
            out[j.job_id] = idents.get(int(j.wsl_pid)) == j.pid_identity
        else:
            legacy.append(j)
    if legacy:
        alive = wsl.wsl_pids_alive([j.wsl_pid for j in legacy])
        for j in legacy:
            out[j.job_id] = alive.get(int(j.wsl_pid), False)
    return out


def _refresh_state(job: FlowJob, alive: bool | None = None) -> None:
    """Update a job's state from its live handle or the Linux PID.

    ``alive`` lets :func:`list_jobs` inject a batched liveness answer instead
    of paying one WSL round-trip per job.
    """
    if job.state not in ("running",):
        return
    # Live process handle (job started in this server instance): rc is exact.
    if job._proc is not None:
        rc = job._proc.poll()
        if rc is None:
            return
        with _JOB_LOCK:
            job.returncode = rc
            job.finished_at = job.finished_at or _utcnow()
            job.state = "completed" if rc == 0 else "failed"
            if job.state == "failed":
                job.error = _scan_log_for_fatal(job.log_path)
        _save_metadata(job)
        if job.state == "completed":
            _publish(job)
        _pump()
        return
    # Reloaded job (no handle): the only honest signal is whether the Linux PID
    # is still alive. If it is, the run continues; if not, it finished while we
    # were down and we classify from the log (exact rc was lost with the handle).
    if job.wsl_pid is not None:
        is_alive = alive if alive is not None else _liveness([job]).get(job.job_id, False)
        if is_alive:
            return
    state, err = _classify_finished(job.log_path)
    with _JOB_LOCK:
        job.state = state
        job.error = err
        job.finished_at = job.finished_at or _utcnow()
    _save_metadata(job)
    if job.state == "completed":
        _publish(job)
    _pump()


def list_jobs() -> list[FlowJob]:
    _ensure_loaded()
    with _JOB_LOCK:
        items = list(_JOBS.values())
    # Batch the PID-liveness checks for reloaded running jobs (one WSL
    # round-trip total instead of one per job).
    need = [j for j in items if j.state == "running" and j._proc is None and j.wsl_pid]
    alive = _liveness(need)
    for job in items:
        hint = alive.get(job.job_id) if job in need else None
        _refresh_state(job, alive=hint)
    return sorted(items, key=lambda j: j.created_at, reverse=True)


def cancel_job(job_id: str) -> FlowJob:
    job = get_job(job_id)
    if job.state == "queued":
        with _JOB_LOCK:
            job.state = "cancelled"
            job.finished_at = _utcnow()
        _save_metadata(job)
        return job
    if job.state != "running":
        return job
    # Prefer the live handle; fall back to killing the Linux PID directly so we
    # can cancel jobs inherited from a previous server instance.
    if job._proc is not None and job._proc.poll() is None:
        try:
            if os.name == "nt":
                job._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                time.sleep(0.5)
            job._proc.terminate()
            try:
                job._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job._proc.kill()
        except Exception:
            pass
    # Only signal the PID if it is still *our* Flow: after a reboot the number may
    # belong to an unrelated process, which must never be killed.
    if job.wsl_pid is not None and _liveness([job]).get(job.job_id, False):
        wsl.wsl_kill(job.wsl_pid)
        time.sleep(0.5)
        if _liveness([job]).get(job.job_id, False):
            wsl.wsl_kill(job.wsl_pid, force=True)
    with _JOB_LOCK:
        job.state = "cancelled"
        job.finished_at = _utcnow()
    _save_metadata(job)
    _pump()
    return job


def tail_log(job_id: str, lines: int | None = None) -> str:
    job = get_job(job_id)
    n = lines or SETTINGS.log_tail_default
    return "".join(_read_tail(job.log_path, n))


# ---------------------------------------------------------------------------
# Efficient tail reading (seek from the end; don't slurp the whole file)
# ---------------------------------------------------------------------------


def _read_tail(path: str | Path, n_lines: int, block_size: int = 8192) -> list[str]:
    """Return the last ``n_lines`` lines of a (possibly large) text file.

    Reads backward in blocks so a multi-MB Flow log doesn't cost O(filesize)
    per status poll.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            remaining = end
            data = b""
            newlines = 0
            while remaining > 0 and newlines <= n_lines:
                read_size = min(block_size, remaining)
                remaining -= read_size
                fh.seek(remaining)
                chunk = fh.read(read_size)
                data = chunk + data
                newlines = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return []
    lines = text.splitlines(keepends=True)
    return lines[-n_lines:]


def _last_progress_line(log_path: str) -> str | None:
    tail = _read_tail(log_path, 60)
    for line in reversed(tail):
        s = line.strip()
        if not s:
            continue
        # Flow prints lines like "Report step  10/365 ... in 0.42s"
        if "Report step" in s or "Time step" in s:
            return s
    return tail[-1].strip() if tail else None


# ---------------------------------------------------------------------------
# Persistence / restart recovery
# ---------------------------------------------------------------------------


def _prune_locked() -> None:
    if len(_JOBS) <= SETTINGS.job_history_size:
        return
    finished = [j for j in _JOBS.values() if j.state not in ("running", "queued")]
    finished.sort(key=lambda j: j.created_at)
    to_remove = len(_JOBS) - SETTINGS.job_history_size
    for job in finished[:to_remove]:
        _JOBS.pop(job.job_id, None)


def _ensure_loaded() -> None:
    """Rehydrate jobs from prior server runs, once, on first use (not import).

    Reloaded jobs are **never** auto-launched. The work directory is shared by
    every process that imports this module, so a job persisted as ``queued`` is
    very likely still owned and about to be launched by the process that
    submitted it. Auto-resuming here raced that owner and started a second
    ``flow`` on the same output directory, corrupting both runs. Adoption is
    now explicit: see :func:`resume_orphaned_jobs`.
    """
    global _LOADED
    if _LOADED:
        return
    with _JOB_LOCK:
        if _LOADED:
            return
        _LOADED = True
        _load_jobs_from_disk()


def resume_orphaned_jobs() -> list[str]:
    """Explicitly adopt jobs a previous process left queued, and launch them.

    Only call this when no other process is submitting to the same work
    directory — otherwise the two will race for the same output paths.
    """
    _ensure_loaded()
    with _JOB_LOCK:
        adopted = [j.job_id for j in _JOBS.values() if j.state == "queued"]
    _pump()
    return adopted


def _load_jobs_from_disk() -> None:
    jobs_dir = SETTINGS.work_dir / "jobs"
    if not jobs_dir.is_dir():
        return
    for meta in jobs_dir.glob("*/job.json"):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = d.get("job_id")
        if not job_id or job_id in _JOBS:
            continue
        job = FlowJob(
            job_id=job_id,
            deck_path_win=d.get("deck_path_win", ""),
            deck_path_wsl=d.get("deck_path_wsl", ""),
            case_name=d.get("case_name", ""),
            workdir_win=d.get("workdir_win", str(meta.parent)),
            workdir_wsl=d.get("workdir_wsl", ""),
            argv=d.get("argv", []),
            log_path=d.get("log_path", str(meta.parent / _LOG_FILE)),
            created_at=d.get("created_at") or d.get("started_at") or _utcnow(),
            started_at=d.get("started_at"),
            output_dir_wsl=d.get("output_dir_wsl", ""),
            pid=d.get("pid"),
            wsl_pid=d.get("wsl_pid"),
            pid_identity=d.get("pid_identity"),
            finished_at=d.get("finished_at"),
            returncode=d.get("returncode"),
            state=d.get("state", "unknown"),
            error=d.get("error"),
            last_progress=d.get("last_progress"),
            flow_version=d.get("flow_version"),
            deck_sha256=d.get("deck_sha256"),
            run_env=d.get("run_env") or {},
            published_to=d.get("published_to"),
            publish_error=d.get("publish_error"),
        )
        _JOBS[job_id] = job
