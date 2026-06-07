"""OPM Flow tools — start, watch, and stop reservoir simulations.

These are thin wrappers around :mod:`opm_mcp.jobs` shaped for MCP. Each tool
returns a JSON-serialisable dict so Claude gets structured data back.
"""

from __future__ import annotations

from typing import Any

from .. import jobs, wsl


def run_flow(
    deck_path: str,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
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

    Returns
    -------
    dict
        ``job_id``, ``pid``, ``log_path``, ``output_dir`` and the resolved
        argv. Poll :func:`flow_status` with the ``job_id`` until ``state`` is no
        longer ``"running"``.
    """
    job = jobs.start_flow_job(
        deck_path,
        extra_args=extra_args,
        output_dir=output_dir,
        threads=threads,
        convergence_info=convergence_info,
    )
    return {
        "job_id": job.job_id,
        "pid": job.pid,
        "wsl_pid": job.wsl_pid,
        "case_name": job.case_name,
        "log_path": job.log_path,
        "output_dir_wsl": job.output_dir_wsl,
        "output_dir_win": wsl.to_windows_path(job.output_dir_wsl),
        "case_prefix_win": f"{wsl.to_windows_path(job.output_dir_wsl)}\\{job.case_name}",
        "state": job.state,
        "started_at": job.started_at,
        "argv": job.argv,
    }


def flow_status(job_id: str) -> dict[str, Any]:
    """Return the current state of a Flow job, including the latest progress line."""
    job = jobs.get_job(job_id)
    out_win = wsl.to_windows_path(job.output_dir_wsl) if job.output_dir_wsl else f"{job.workdir_win}\\out"
    return {
        "job_id": job.job_id,
        "state": job.state,
        "pid": job.pid,
        "wsl_pid": job.wsl_pid,
        "returncode": job.returncode,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "case_name": job.case_name,
        "last_progress": job.last_progress,
        "error": job.error,
        "log_path": job.log_path,
        "output_dir": out_win,
        "case_prefix_win": f"{out_win}\\{job.case_name}",
    }


def flow_log_tail(job_id: str, lines: int = 200) -> dict[str, Any]:
    """Return the last ``lines`` lines of the merged stdout/stderr log."""
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
        "jobs": [
            {
                "job_id": j.job_id,
                "state": j.state,
                "case_name": j.case_name,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "returncode": j.returncode,
            }
            for j in jobs.list_jobs()
        ]
    }
