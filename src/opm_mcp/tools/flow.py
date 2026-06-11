"""OPM Flow tools — start, watch, and stop reservoir simulations.

These are thin wrappers around :mod:`opm_mcp.jobs` shaped for MCP. Each tool
returns a JSON-serialisable dict so Claude gets structured data back.
"""

from __future__ import annotations

import re
from typing import Any

from .. import jobs, wsl
from ..config import SETTINGS


def run_flow(
    deck_path: str,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
    mpi_processes: int | None = None,
) -> dict[str, Any]:
    """Start a Flow simulation in the background.

    Parameters
    ----------
    deck_path : str
        Path to the ECLIPSE-format ``.DATA`` deck. May be a Windows path
        (``C:\\...\\CASE.DATA``) or an already-WSL path (``/home/.../CASE.DATA``).
    extra_args : list[str] | None
        Additional ``flow`` flags, e.g. ``["--enable-tuning=true"]``.
    output_dir : str | None
        Where Flow should write its output. Defaults to a per-job folder under
        the server work dir.
    threads : int | None
        Convenience for ``--threads-per-process``.
    convergence_info : str | None
        ``--output-extra-convergence-info`` value. Defaults to
        ``"steps,iterations"`` so ``flow_convergence_report`` has data. Set to
        ``"none"`` to skip the extra diagnostic files.
    mpi_processes : int | None
        Run distributed via ``mpirun -np N`` (per-rank ``threads`` still apply).

    Returns
    -------
    dict
        ``job_id``, ``state`` (``"running"`` or ``"queued"`` when all
        ``OPM_MCP_MAX_PARALLEL`` slots are busy), paths, and the resolved argv.
        Poll :func:`flow_status` with the ``job_id`` until ``state`` is terminal.
    """
    job = jobs.start_flow_job(
        deck_path,
        extra_args=extra_args,
        output_dir=output_dir,
        threads=threads,
        convergence_info=convergence_info,
        mpi_processes=mpi_processes,
    )
    out_win = wsl.to_windows_path(job.output_dir_wsl)
    sep = "\\" if "\\" in out_win or ":" in out_win else "/"
    return {
        "job_id": job.job_id,
        "pid": job.pid,
        "wsl_pid": job.wsl_pid,
        "case_name": job.case_name,
        "log_path": job.log_path,
        "output_dir_wsl": job.output_dir_wsl,
        "output_dir_win": out_win,
        "case_prefix_win": f"{out_win}{sep}{job.case_name}",
        "state": job.state,
        "queue_position": jobs.queue_position(job),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "deck_sha256": job.deck_sha256,
        "argv": job.argv,
    }


def flow_status(job_id: str) -> dict[str, Any]:
    """Return the current state of a Flow job, including the latest progress line."""
    job = jobs.get_job(job_id)
    out_win = wsl.to_windows_path(job.output_dir_wsl) if job.output_dir_wsl else f"{job.workdir_win}\\out"
    sep = "\\" if "\\" in out_win or ":" in out_win else "/"
    return {
        "job_id": job.job_id,
        "state": job.state,
        "queue_position": jobs.queue_position(job),
        "pid": job.pid,
        "wsl_pid": job.wsl_pid,
        "returncode": job.returncode,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "case_name": job.case_name,
        "last_progress": job.last_progress,
        "error": job.error,
        "log_path": job.log_path,
        "output_dir": out_win,
        "case_prefix_win": f"{out_win}{sep}{job.case_name}",
        "flow_version": job.flow_version,
        "deck_sha256": job.deck_sha256,
    }


def flow_log_tail(job_id: str, lines: int = 200, grep: str | None = None) -> dict[str, Any]:
    """Return the last ``lines`` lines of the merged stdout/stderr log.

    With ``grep`` (case-insensitive regex) the *whole* log is filtered first
    and the last ``lines`` matching lines come back, each prefixed with its
    line number — e.g. ``grep="will be shut|Problem:|Error:"`` pulls the
    well-shut / solver-failure story out of a long run without host-side grep.
    """
    if grep:
        try:
            rx = re.compile(grep, re.IGNORECASE)
        except re.error as exc:
            return {"ok": False, "job_id": job_id, "error": f"bad regex: {exc}"}
        job = jobs.get_job(job_id)
        matched: list[str] = []
        try:
            with open(job.log_path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        matched.append(f"{i}: {line.rstrip()}")
        except OSError as exc:
            return {"ok": False, "job_id": job_id, "error": str(exc)}
        return {
            "job_id": job_id,
            "lines": lines,
            "grep": grep,
            "n_matches": len(matched),
            "tail": "\n".join(matched[-lines:]),
        }
    text = jobs.tail_log(job_id, lines=lines)
    return {"job_id": job_id, "lines": lines, "tail": text}


def flow_cancel(job_id: str) -> dict[str, Any]:
    """Cancel a running Flow job."""
    job = jobs.cancel_job(job_id)
    return {
        "job_id": job.job_id,
        "state": job.state,
        "returncode": job.returncode,
        "finished_at": job.finished_at,
    }


def flow_jobs() -> dict[str, Any]:
    """List recent Flow jobs known to this server (most recent first)."""
    return {
        "max_parallel": SETTINGS.max_parallel,
        "jobs": [
            {
                "job_id": j.job_id,
                "state": j.state,
                "case_name": j.case_name,
                "created_at": j.created_at,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "returncode": j.returncode,
            }
            for j in jobs.list_jobs()
        ],
    }
