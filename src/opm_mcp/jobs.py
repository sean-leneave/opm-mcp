"""Background job manager for ``flow`` runs.

Reservoir simulations are long. The MCP tool layer cannot block while a 4-hour
run completes, so ``start_flow_job`` spawns ``flow`` under WSL with a captured
log directory and returns a job id immediately. Subsequent tool calls
(``flow_status``, ``flow_log_tail``, ``flow_cancel``) operate on that id.

Each job lives in its own directory under ``SETTINGS.work_dir/jobs/<id>/`` so we
can recover state across server restarts.
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

_JOB_LOCK = threading.Lock()
_JOBS: dict[str, "FlowJob"] = {}


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
    pid: Optional[int] = None
    finished_at: Optional[str] = None
    returncode: Optional[int] = None
    state: str = "running"  # running | completed | failed | cancelled
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
            "pid": self.pid,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "state": self.state,
            "error": self.error,
            "last_progress": self.last_progress,
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_metadata(job: FlowJob) -> None:
    meta_path = Path(job.workdir_win) / "job.json"
    meta_path.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")


def start_flow_job(
    deck_path: str,
    *,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
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
    """
    deck_win = str(Path(deck_path).expanduser()) if not deck_path.startswith("/") else deck_path
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
    if extra_args:
        argv.extend(extra_args)

    log_path_win = workdir_win / "flow.log"
    log_path_wsl = wsl.to_wsl_path(str(log_path_win))

    # `flow` streams progress to stdout; capture stdout+stderr into one file.
    proc = wsl.popen(
        argv,
        stdout_path=log_path_win,
        stderr_path=log_path_win,  # appended via 2>&1 inside Popen? No — we merge below.
    )
    # Popen above opened stderr as a separate handle to the same file, which
    # would clobber stdout. Reopen stderr as the same descriptor by re-spawning
    # through bash with `2>&1` instead. Cheap workaround: kill & restart.
    proc.kill()
    proc.wait(timeout=10)
    merged_cmd = f"exec {SETTINGS.flow_bin} {shlex.quote(deck_wsl)} --output-dir={shlex.quote(out_dir_wsl)}"
    if t and t > 0:
        merged_cmd += f" --threads-per-process={t}"
    if extra_args:
        merged_cmd += " " + " ".join(shlex.quote(a) for a in extra_args)
    merged_cmd += f" >{shlex.quote(log_path_wsl)} 2>&1"
    bash_argv = ["bash", "-lc", f"mkdir -p {shlex.quote(out_dir_wsl)} && {merged_cmd}"]
    proc = wsl.popen(bash_argv)

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
        pid=proc.pid,
        _proc=proc,
    )
    with _JOB_LOCK:
        _JOBS[job_id] = job
        _prune_locked()
    _save_metadata(job)

    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    return job


def _watch(job: FlowJob) -> None:
    proc = job._proc
    assert proc is not None
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


def _scan_log_for_fatal(log_path: str) -> str | None:
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-200:]
    except OSError:
        return None
    for line in reversed(tail):
        if any(tok in line for tok in ("FATAL", "ERROR", "Aborted")):
            return line.strip()
    return None


def get_job(job_id: str) -> FlowJob:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise KeyError(f"Unknown job id: {job_id}")
    if job._proc is not None and job.state == "running":
        rc = job._proc.poll()
        if rc is not None:
            with _JOB_LOCK:
                job.returncode = rc
                job.finished_at = job.finished_at or _utcnow()
                job.state = "completed" if rc == 0 else "failed"
            _save_metadata(job)
    job.last_progress = _last_progress_line(job.log_path)
    return job


def list_jobs() -> list[FlowJob]:
    with _JOB_LOCK:
        items = list(_JOBS.values())
    return sorted(items, key=lambda j: j.started_at, reverse=True)


def cancel_job(job_id: str) -> FlowJob:
    job = get_job(job_id)
    if job.state != "running" or job._proc is None:
        return job
    try:
        if os.name == "nt":
            job._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            time.sleep(0.5)
        job._proc.terminate()
        try:
            job._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            job._proc.kill()
    finally:
        with _JOB_LOCK:
            job.state = "cancelled"
            job.finished_at = _utcnow()
            job.returncode = job._proc.returncode if job._proc else None
        _save_metadata(job)
    return job


def tail_log(job_id: str, lines: int | None = None) -> str:
    job = get_job(job_id)
    n = lines or SETTINGS.log_tail_default
    try:
        with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
            buf = fh.readlines()
    except FileNotFoundError:
        return ""
    return "".join(buf[-n:])


def _last_progress_line(log_path: str) -> str | None:
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            buf = fh.readlines()
    except FileNotFoundError:
        return None
    for line in reversed(buf):
        s = line.strip()
        if not s:
            continue
        # Flow prints lines like "Report step  10/365 ... in 0.42s"
        if "Report step" in s or "Time step" in s:
            return s
    return buf[-1].strip() if buf else None


def _prune_locked() -> None:
    if len(_JOBS) <= SETTINGS.job_history_size:
        return
    finished = [j for j in _JOBS.values() if j.state != "running"]
    finished.sort(key=lambda j: j.started_at)
    to_remove = len(_JOBS) - SETTINGS.job_history_size
    for job in finished[:to_remove]:
        _JOBS.pop(job.job_id, None)
