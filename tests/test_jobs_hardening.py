"""Fixes 2-4 and publishing, against a stub 'flow' binary (POSIX native backend).

* detached runs (fix 3): flow runs in its own session, so the server's process
  group can die without taking the simulation with it;
* fixed environment (fix 4): flow sees only the recorded ``run_env``;
* option policy (fix 2): refused before any job folder is created;
* publishing: completed case files are copied to OPM_MCP_PUBLISH.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX-only: native backend with a stub flow")

STUB = r"""#!/bin/bash
# Stub flow: writes its environment and fake case files to --output-dir.
out=""
for a in "$@"; do
  case "$a" in --output-dir=*) out="${a#--output-dir=}";; esac
done
name="$(basename "$1" .DATA)"
echo "stub flow starting"
env | sort > "$out/stub-env.txt"
for s in EGRID INIT UNRST SMSPEC UNSMRY PRT; do echo "$s" > "$out/$name.$s"; done
echo "Report step  1/1 at day 1/1"
sleep 1
exit 0
"""


@pytest.fixture()
def jobs_mod(tmp_path, monkeypatch):
    stub = tmp_path / "fakeflow"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("OPM_MCP_FLOW_BIN", str(stub))
    monkeypatch.setenv("OPM_MCP_MAX_PARALLEL", "1")
    monkeypatch.setenv("OPM_MCP_ROOTS", str(tmp_path / "models"))
    monkeypatch.setenv("OPM_MCP_PUBLISH", str(tmp_path / "pub"))
    monkeypatch.setenv("OPM_MCP_MAX_CORES", "8")
    # A variable a login shell or the server might carry: must NOT reach flow.
    monkeypatch.setenv("LEAKY_SECRET", "should-not-leak")
    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    (tmp_path / "models").mkdir()
    import opm_mcp.config as cfg

    importlib.reload(cfg)
    import opm_mcp.guard as guard
    import opm_mcp.jobs as jobs

    importlib.reload(guard)
    importlib.reload(jobs)
    yield jobs
    monkeypatch.undo()
    importlib.reload(cfg)
    importlib.reload(guard)
    importlib.reload(jobs)


def _deck(tmp_path: Path) -> Path:
    deck = tmp_path / "models" / "CASE1.DATA"
    deck.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    return deck


def _wait(jobs_mod, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = jobs_mod.get_job(job_id)
        if j.state not in ("running", "queued"):
            return j
        time.sleep(0.1)
    raise TimeoutError(job_id)


def test_flow_runs_in_its_own_session(jobs_mod, tmp_path):
    job = jobs_mod.start_flow_job(str(_deck(tmp_path)), convergence_info=None)
    pid = jobs_mod._read_pidfile(job.pidfile_path, timeout=5.0)
    assert pid is not None
    assert os.getsid(pid) != os.getsid(0), "flow must not share the server's session"
    _wait(jobs_mod, job.job_id)


def test_flow_sees_only_the_recorded_environment(jobs_mod, tmp_path):
    job = jobs_mod.start_flow_job(str(_deck(tmp_path)), convergence_info=None)
    done = _wait(jobs_mod, job.job_id)
    assert done.state == "completed"
    out = Path(done.output_dir_wsl)
    seen = dict(line.split("=", 1) for line in (out / "stub-env.txt").read_text().splitlines() if "=" in line)
    assert "LEAKY_SECRET" not in seen
    assert "OMP_NUM_THREADS" not in seen
    for key, value in done.run_env.items():
        assert seen.get(key) == value
    meta = json.loads((Path(done.workdir_win) / "job.json").read_text())
    assert meta["run_env"] == done.run_env
    assert meta["run_env"]["LC_ALL"] == "C.UTF-8"


def test_completed_job_is_published(jobs_mod, tmp_path):
    job = jobs_mod.start_flow_job(str(_deck(tmp_path)), convergence_info=None)
    done = _wait(jobs_mod, job.job_id)
    deadline = time.time() + 5
    while not done.published_to and time.time() < deadline:
        time.sleep(0.1)
        done = jobs_mod.get_job(job.job_id)
    assert done.published_to == str(tmp_path / "pub" / f"CASE1-{job.job_id}")
    pub = Path(done.published_to)
    for suffix in ("EGRID", "INIT", "UNRST", "SMSPEC", "UNSMRY", "PRT"):
        assert (pub / f"CASE1.{suffix}").is_file()
    assert (pub / "flow.log").is_file()
    assert json.loads((pub / "job.json").read_text())["published_to"] == str(pub)


def test_refused_options_create_nothing(jobs_mod, tmp_path):
    jobs_dir = tmp_path / "wd" / "jobs"
    before = set(jobs_dir.iterdir())
    with pytest.raises(jobs_mod.guard.FlowOptionRejected):
        jobs_mod.start_flow_job(str(_deck(tmp_path)), extra_args=["--output-dir=/tmp/x"], convergence_info=None)
    with pytest.raises(ValueError):
        jobs_mod.start_flow_job(str(_deck(tmp_path)), mpi_processes=16, convergence_info=None)
    assert set(jobs_dir.iterdir()) == before


def test_deck_outside_roots_is_refused(jobs_mod, tmp_path):
    outside = tmp_path / "elsewhere.DATA"
    outside.write_text("RUNSPEC\nEND\n", encoding="utf-8")
    with pytest.raises(jobs_mod.guard.PathNotAllowed):
        jobs_mod.start_flow_job(str(outside), convergence_info=None)


def test_accepted_extra_args_are_recorded(jobs_mod, tmp_path):
    job = jobs_mod.start_flow_job(
        str(_deck(tmp_path)), extra_args=["--enable-tuning=true"], convergence_info=None
    )
    assert "--enable-tuning=true" in job.argv
    _wait(jobs_mod, job.job_id)
    meta = json.loads((Path(job.workdir_win) / "job.json").read_text())
    assert "--enable-tuning=true" in meta["argv"]


# --- Durability: PID identity (boot id + start time) -------------------------
# After a reboot or PID wrap, a recorded PID may belong to an unrelated process.
# A job must then read as finished (never "running" forever), and cancel must not
# signal the stranger.

identity_only = pytest.mark.skipif(
    not Path("/proc/sys/kernel/random/boot_id").exists(), reason="needs Linux /proc"
)


def _plant_running_job(tmp_path: Path, pid: int, identity: str | None) -> str:
    job_id = "planted0001"
    wd = tmp_path / "wd" / "jobs" / job_id
    wd.mkdir(parents=True)
    (wd / "flow.log").write_text("stub flow starting\n", encoding="utf-8")
    meta = {
        "job_id": job_id,
        "deck_path_win": str(tmp_path / "models" / "CASE1.DATA"),
        "deck_path_wsl": str(tmp_path / "models" / "CASE1.DATA"),
        "case_name": "CASE1",
        "workdir_win": str(wd),
        "workdir_wsl": str(wd),
        "argv": ["flow", "CASE1.DATA"],
        "log_path": str(wd / "flow.log"),
        "created_at": "2026-11-20T00:00:00+00:00",
        "started_at": "2026-11-20T00:00:00+00:00",
        "output_dir_wsl": str(wd / "out"),
        "wsl_pid": pid,
        "pid_identity": identity,
        "state": "running",
    }
    (wd / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    return job_id


@identity_only
def test_pid_identity_is_recorded(jobs_mod, tmp_path):
    job = jobs_mod.start_flow_job(str(_deck(tmp_path)), convergence_info=None)
    done = _wait(jobs_mod, job.job_id)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    meta = json.loads((Path(done.workdir_win) / "job.json").read_text())
    assert meta["pid_identity"] and meta["pid_identity"].startswith(boot + ":")


@identity_only
def test_reused_pid_reads_as_finished_and_is_not_killed(jobs_mod, tmp_path):
    import subprocess

    stranger = subprocess.Popen(["sleep", "30"])
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        job_id = _plant_running_job(tmp_path, stranger.pid, f"{boot}:1")  # wrong start time
        listed = {j.job_id: j for j in jobs_mod.list_jobs()}
        assert listed[job_id].state != "running"
        jobs_mod.cancel_job(job_id)
        time.sleep(0.7)
        assert stranger.poll() is None, "cancel must not signal a process that merely reuses the PID"
    finally:
        stranger.kill()
        stranger.wait()


@identity_only
def test_matching_identity_stays_running_and_cancel_stops_it(jobs_mod, tmp_path):
    import subprocess

    ours = subprocess.Popen(["sleep", "30"])
    try:
        ident = jobs_mod.wsl.wsl_pids_identity([ours.pid])[ours.pid]
        assert ident
        job_id = _plant_running_job(tmp_path, ours.pid, ident)
        listed = {j.job_id: j for j in jobs_mod.list_jobs()}
        assert listed[job_id].state == "running"
        assert jobs_mod.cancel_job(job_id).state == "cancelled"
        ours.wait(timeout=5)
    finally:
        if ours.poll() is None:
            ours.kill()
            ours.wait()


@identity_only
def test_dead_pid_with_identity_reads_as_finished(jobs_mod, tmp_path):
    import subprocess

    gone = subprocess.Popen(["true"])
    gone.wait()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    job_id = _plant_running_job(tmp_path, gone.pid, f"{boot}:1")
    listed = {j.job_id: j for j in jobs_mod.list_jobs()}
    assert listed[job_id].state != "running"
