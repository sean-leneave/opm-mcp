"""Tests for env-driven config defaults."""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def test_settings_defaults(tmp_path, monkeypatch):
    """When env vars are unset we fall back to documented defaults."""
    for var in (
        "OPM_MCP_WSL_DISTRO",
        "OPM_MCP_FLOW_BIN",
        "OPM_MCP_RESINSIGHT_BIN",
        "OPM_MCP_FLOW_THREADS",
        "OPM_MCP_JOB_HISTORY",
        "OPM_MCP_LOG_TAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path))

    import opm_mcp.config as cfg

    importlib.reload(cfg)
    s = cfg.SETTINGS

    assert s.wsl_distro == "Ubuntu-24.04"
    assert s.flow_bin == "flow"
    assert s.resinsight_bin == "ResInsight"
    assert s.work_dir == tmp_path
    assert (tmp_path / "jobs").is_dir()
    assert (tmp_path / "artifacts").is_dir()
    assert s.flow_default_threads == 0
    assert s.job_history_size == 50
    assert s.log_tail_default == 200


def test_settings_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("OPM_MCP_WSL_DISTRO", "Ubuntu")
    monkeypatch.setenv("OPM_MCP_FLOW_BIN", "/opt/opm/bin/flow")
    monkeypatch.setenv("OPM_MCP_FLOW_THREADS", "8")
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path))

    import opm_mcp.config as cfg

    importlib.reload(cfg)
    s = cfg.SETTINGS

    assert s.wsl_distro == "Ubuntu"
    assert s.flow_bin == "/opt/opm/bin/flow"
    assert s.flow_default_threads == 8
    assert s.work_dir == tmp_path
