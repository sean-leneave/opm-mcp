"""Background job manager for ``flow`` runs.

Reservoir simulations are long. The MCP tool layer cannot block while a 4-hour
run completes, so ``start_flow_job`` spawns ``flow`` under WSL with a captured
log directory and returns a job id immediately. Subsequent tool calls
(``flow_status``, ``flow_log_tail``, ``flow_cancel``) operate on that id.

Each job lives in its own directory under ``SETTINGS.work_dir/jobs/<id>/`` and
this state genuinely survives a server restart:

* The bash wrapper records the **Linux** ``flow`` PID (``flow.pid``) and writes
  its exit code to a sentinel (``flow.rc``) when it finishes.
* At import, :func:`_load_jobs_from_disk` rehydrates every ``job.json`` and
  re-derives the live state from those sentinels — so a job started before the
  restart can still be polled and even cancelled (via ``wsl kill``).
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import wsl
from .config import SETTINGS

_JOB_LOCK = threading.RLock()
_JOBS: dict[str, "FlowJob"] = {}

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
    started_at: str
    output_dir_wsl: str
    pid: Optional[int] = None  # Windows-side wsl.exe launcher PID
    wsl_pid: Optional[int] = None  # Linux-side flow PID (for cross-restart kill)
    finished_at: Optional[str] = None
    returncode: Optional[int] = None
    state: str = "running"  # running | completed | failed | cancelled | unknown
    error: Optional[str] = None
    last_progress: Optional[str] = None
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)

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
            "started_at": self.started_at,
            "output_dir_wsl": self.output_dir_wsl,
            "pid": self.pid,
            "wsl_pid": self.wsl_pid,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "state": self.state,
            "error": self.error,
            "last_progress": self.last_progress,
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
# Starting a job
# ---------------------------------------------------------------------------


def start_flow_job(
    deck_path: str,
    *,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
) -> FlowJob:
    """Spawn a ``flow`` run in the background and register it.

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
        Convenience for ``--threads-per-process=N``. ``None`` uses the
        ``OPM_MCP_FLOW_THREADS`` default (0 = let Flow decide).
    convergence_info : str | None
        Value for ``--output-extra-convergence-info`` (``steps``,
        ``iterations``, ``steps,iterations`` or ``none``). Defaults to writing
        both so :func:`opm_mcp.tools.analysis.flow_convergence_report` has data.
        Pass ``None``/``"none"`` to disable.
    """
    deck_wsl = wsl.to_wsl_path(deck_path)
    case_name = Path(deck_wsl).stem

    job_id = uuid.uuid4().hex[:12]
    workdir_win = SETTINGS.work_dir / "jobs" / job_id
    workdir_win.mkdir(parents=True, exist_ok=True)
    workdir_wsl = wsl.to_wsl_path(str(workdir_win))

    out_dir_wsl = wsl.to_wsl_path(output_dir) if output_dir else f"{workdir_wsl}/out"

    argv = [SETTINGS.flow_bin, deck_wsl, f"--output-dir={out_dir_wsl}"]
    t = threads if threads is not None else SETTINGS.flow_default_threads
    if t and t > 0:
        argv.append(f"--threads-per-process={t}")
    if convergence_info and convergence_info.lower() != "none":
        argv.append(f"--output-extra-convergence-info={convergence_info}")
    if extra_args:
        argv.extend(extra_args)

    log_path_win = workdir_win / _LOG_FILE
    log_path_wsl = wsl.to_wsl_path(str(log_path_win))
    pid_wsl = wsl.to_wsl_path(str(workdir_win / _PID_FILE))

    # One clean spawn (no more "spawn, kill, respawn"): write the shell's own
    # PID, then ``exec`` flow so it inherits that exact PID. Two payoffs:
    #   * the PID file holds flow's real Linux PID -> cross-restart ``wsl kill``.
    #   * because the launcher's child *becomes* flow, ``Popen.wait()`` returns
    #     flow's true exit code (``$!``/``$BASHPID`` are unreliable under wsl.exe,
    #     so we deliberately avoid them).
    flow_cmd = " ".join(shlex.quote(a) for a in argv)
    bash_line = (
        f"mkdir -p {shlex.quote(out_dir_wsl)} && "
        f"echo $$ > {shlex.quote(pid_wsl)} && "
        f"exec {flow_cmd} > {shlex.quote(log_path_wsl)} 2>&1"
    )
    proc = wsl.popen(["bash", "-lc", bash_line])

    wsl_pid = _read_pidfile(workdir_win / _PID_FILE, timeout=5.0)

    job = FlowJob(
        job_id=job_id,
        deck_path_win=wsl.to_windows_path(deck_wsl),
        deck_path_wsl=deck_wsl,
        case_name=case_name,
        workdir_win=str(workdir_win),
        workdir_wsl=workdir_wsl,
        argv=argv,
        log_path=str(log_path_win),
        started_at=_utcnow(),
        output_dir_wsl=out_dir_wsl,
        pid=proc.pid,
        wsl_pid=wsl_pid,
        _proc=proc,
    )
    with _JOB_LOCK:
        _JOBS[job_id] = job
        _prune_locked()
    _save_metadata(job)

    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    return job


def _read_pidfile(path: Path, timeout: float = 5.0) -> int | None:
    """Poll briefly for the bash wrapper to write flow's PID."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            txt = path.read_text(encoding="utf-8").strip()
            if txt:
                return int(txt)
        except (FileNotFoundError, ValueError):
            time.sleep(0.1)
    return None


def _watch(job: FlowJob) -> None:
    proc = job._proc
    if proc is None:
        return
    # Because the wrapper ``exec``s flow, the launcher's child *is* flow, so its
    # return code is flow's true exit status.
    rc = proc.wait()
    with _JOB_LOCK:
        if job.wsl_pid is None:
            job.wsl_pid = _read_pidfile(job.pidfile_path, timeout=0.1)
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
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise KeyError(f"Unknown job id: {job_id}")
    _refresh_state(job)
    progress = _last_progress_line(job.log_path)
    with _JOB_LOCK:
        job.last_progress = progress
    return job


def _refresh_state(job: FlowJob) -> None:
    """Update a job's state from its live process handle or the Linux PID."""
    if job.state != "running":
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
        return
    # Reloaded job (no handle): the only honest signal is whether the Linux PID
    # is still alive. If it is, the run continues; if not, it finished while we
    # were down and we classify from the log (exact rc was lost with the handle).
    if job.wsl_pid is not None and wsl.wsl_pid_alive(job.wsl_pid):
        return
    state, err = _classify_finished(job.log_path)
    with _JOB_LOCK:
        job.state = state
        job.error = err
        job.finished_at = job.finished_at or _utcnow()
    _save_metadata(job)


def list_jobs() -> list[FlowJob]:
    with _JOB_LOCK:
        items = list(_JOBS.values())
    for job in items:
        _refresh_state(job)
    return sorted(items, key=lambda j: j.started_at, reverse=True)


def cancel_job(job_id: str) -> FlowJob:
    job = get_job(job_id)
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
    if job.wsl_pid is not None and wsl.wsl_pid_alive(job.wsl_pid):
        wsl.wsl_kill(job.wsl_pid)
        time.sleep(0.5)
        if wsl.wsl_pid_alive(job.wsl_pid):
            wsl.wsl_kill(job.wsl_pid, force=True)
    with _JOB_LOCK:
        job.state = "cancelled"
        job.finished_at = _utcnow()
    _save_metadata(job)
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
    finished = [j for j in _JOBS.values() if j.state != "running"]
    finished.sort(key=lambda j: j.started_at)
    to_remove = len(_JOBS) - SETTINGS.job_history_size
    for job in finished[:to_remove]:
        _JOBS.pop(job.job_id, None)


def _load_jobs_from_disk() -> None:
    """Rehydrate jobs from prior server runs so their handles survive restarts."""
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
            started_at=d.get("started_at", _utcnow()),
            output_dir_wsl=d.get("output_dir_wsl", ""),
            pid=d.get("pid"),
            wsl_pid=d.get("wsl_pid"),
            finished_at=d.get("finished_at"),
            returncode=d.get("returncode"),
            state=d.get("state", "unknown"),
            error=d.get("error"),
            last_progress=d.get("last_progress"),
        )
        _JOBS[job_id] = job
    # Re-derive live state for anything that claimed to be running.
    for job in list(_JOBS.values()):
        if job.state == "running":
            _refresh_state(job)


_load_jobs_from_disk()
