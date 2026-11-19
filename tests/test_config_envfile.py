"""Env-file loading (fork addition): ~/.config/opm-mcp/env or $OPM_MCP_ENV_FILE."""

from __future__ import annotations

import os

from opm_mcp import config


def test_parse_env_file_comments_quotes_and_scope(tmp_path):
    f = tmp_path / "env"
    f.write_text(
        "# full-line comment\n"
        "OPM_MCP_FLOW_BIN=/usr/bin/flow      # inline comment\n"
        "OPM_MCP_WORK_DIR=\"/home/opm/opm scratch\"\n"
        "OPM_MCP_LOG_TAIL=200#not-a-comment\n"
        "PATH=/evil\n"
        "LD_PRELOAD=/evil.so\n"
        "garbage line\n",
        encoding="utf-8",
    )
    values = config._parse_env_file(f)
    assert values == {
        "OPM_MCP_FLOW_BIN": "/usr/bin/flow",
        "OPM_MCP_WORK_DIR": "/home/opm/opm scratch",
        "OPM_MCP_LOG_TAIL": "200#not-a-comment",
    }


def test_env_file_does_not_override_process_environment(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("OPM_MCP_FLOW_BIN=/from/file\nOPM_MCP_WSL_DISTRO=FromFile\n", encoding="utf-8")
    monkeypatch.setenv("OPM_MCP_ENV_FILE", str(f))
    monkeypatch.setenv("OPM_MCP_FLOW_BIN", "/from/process")
    monkeypatch.delenv("OPM_MCP_WSL_DISTRO", raising=False)
    assert config.load_env_file() == f
    assert os.environ["OPM_MCP_FLOW_BIN"] == "/from/process"
    assert os.environ["OPM_MCP_WSL_DISTRO"] == "FromFile"


def test_missing_env_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("OPM_MCP_ENV_FILE", str(tmp_path / "nope"))
    assert config.load_env_file() is None
