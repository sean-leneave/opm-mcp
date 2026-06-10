"""Job-manager integration tests against a stub 'flow' binary.

POSIX-only: they exercise the native-Linux backend (no WSL), which is exactly
what ubuntu CI runs. On Windows the real path is covered by manual smoke tests.
"""

from __future__ import annotations

import importlib
import os
import stat
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX-only: uses the native backend with a stub flow binary")


@pytest.fixture()
def jobs_mod(tmp_path, monkeypatch):
    """Reload config+jobs with an isolated work dir and a stub flow binary."""
    stub = tmp_path / "fakeflow"
    stub.write_text(
        "#!/bin/bash\n"
        'echo "stub flow starting"\n'
        'echo "Report step  1/1 at day 1/1"\n'
        "sleep 0.4\n"
        'echo "done"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("OPM_MCP_FLOW_BIN", str(stub))
    monkeypatch.setenv("OPM_MCP_MAX_PARALLEL", "1")
    import opm_mcp.config as cfg

    importlib.reload(cfg)
    import opm_mcp.jobs as jobs

    importlib.reload(jobs)
    yield jobs
    # Leave modules in a sane state for other tests.
    monkeypatch.undo()
    importlib.reload(cfg)
    importlib.reload(jobs)


def _wait(jobs_mod, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = jobs_mod.get_job(job_id)
        if j.state not in ("running", "queued"):
            return j
        time.sleep(0.1)
    raise TimeoutError(job_id)


def test_lifecycle_completes_with_rc_and_progress(jobs_mod, tmp_path):
    deck = tmp_path / "X.DATA"
    deck.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    job = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    done = _wait(jobs_mod, job.job_id)
    assert done.state == "completed"
    assert done.returncode == 0
    assert done.wsl_pid is not None
    assert "Report step" in (done.last_progress or "")
    assert "stub flow starting" in jobs_mod.tail_log(job.job_id)


def test_queue_respects_max_parallel(jobs_mod, tmp_path):
    deck = tmp_path / "X.DATA"
    deck.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    a = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    b = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    assert a.state == "running"
    assert b.state == "queued"
    assert jobs_mod.queue_position(b) == 1
    done_b = _wait(jobs_mod, b.job_id)
    assert done_b.state == "completed"
    assert jobs_mod.get_job(a.job_id).state == "completed"


def test_failed_run_classified_with_error(jobs_mod, tmp_path):
    bad = tmp_path / "badflow"
    bad.write_text("#!/bin/bash\necho 'Error: deck exploded'\nexit 3\n", encoding="utf-8")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    import opm_mcp.config as cfg

    object.__setattr__(cfg.SETTINGS, "flow_bin", str(bad))  # frozen dataclass override
    jobs_mod.SETTINGS = cfg.SETTINGS
    deck = tmp_path / "Y.DATA"
    deck.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    job = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    done = _wait(jobs_mod, job.job_id)
    assert done.state == "failed"
    assert done.returncode == 3
    assert "deck exploded" in (done.error or "")


def test_cancel_queued_job(jobs_mod, tmp_path):
    deck = tmp_path / "X.DATA"
    deck.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    a = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    b = jobs_mod.start_flow_job(str(deck), convergence_info=None)
    assert b.state == "queued"
    cancelled = jobs_mod.cancel_job(b.job_id)
    assert cancelled.state == "cancelled"
    _wait(jobs_mod, a.job_id)
