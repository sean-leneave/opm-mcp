"""Fix 1 (path confinement) and fix 2 (Flow option policy)."""

from __future__ import annotations

import os

import pytest

import opm_mcp.config as cfg
from opm_mcp import guard

pytestmark = pytest.mark.strict_roots


@pytest.fixture()
def roots(tmp_path):
    """Allow exactly one root: tmp_path/allowed."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    original = cfg.SETTINGS.roots
    object.__setattr__(cfg.SETTINGS, "roots", (allowed,))
    yield allowed
    object.__setattr__(cfg.SETTINGS, "roots", original)


# --- path confinement ------------------------------------------------------


def test_path_inside_root_is_allowed(roots):
    deck = roots / "case" / "SPE1.DATA"
    assert guard.confine(deck) == str(deck)


def test_new_output_file_inside_root_is_allowed(roots):
    # Output paths need not exist yet.
    assert guard.is_allowed(roots / "out" / "not-yet.png")


def test_absolute_path_outside_root_is_refused(roots, tmp_path):
    with pytest.raises(guard.PathNotAllowed):
        guard.confine(tmp_path / "elsewhere" / "x.DATA")


def test_dotdot_escape_is_refused(roots):
    with pytest.raises(guard.PathNotAllowed):
        guard.confine(roots / ".." / "secret.txt")


def test_prefix_sibling_is_not_inside(roots, tmp_path):
    # /x/allowed-evil must not pass a naive startswith('/x/allowed') check.
    sibling = tmp_path / "allowed-evil"
    sibling.mkdir()
    assert not guard.is_allowed(sibling / "f")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_escape_is_refused(roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_ed25519").write_text("not a real key", encoding="utf-8")
    link = roots / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(guard.PathNotAllowed):
        guard.confine(link / "id_ed25519")


def test_confine_args_checks_named_parameters(roots, tmp_path):
    calls = []

    @guard.confine_args
    def tool(case_path: str, output_path: str | None = None, title: str | None = None):
        calls.append((case_path, output_path))
        return "ran"

    assert tool(str(roots / "A"), output_path=str(roots / "a.png"), title="/etc/passwd") == "ran"
    with pytest.raises(guard.PathNotAllowed):
        tool(str(tmp_path / "B"))
    with pytest.raises(guard.PathNotAllowed):
        tool(str(roots / "A"), output_path="/etc/cron.d/x")
    assert len(calls) == 1, "a refused call must not run the tool"


def test_settings_roots_default_to_work_dir(tmp_path, monkeypatch):
    import importlib

    monkeypatch.delenv("OPM_MCP_ROOTS", raising=False)
    monkeypatch.delenv("OPM_MCP_PUBLISH", raising=False)
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path / "wd"))
    importlib.reload(cfg)
    try:
        assert cfg.SETTINGS.roots == (tmp_path / "wd",)
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_settings_roots_always_include_work_and_publish(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("OPM_MCP_ROOTS", str(tmp_path / "models"))
    monkeypatch.setenv("OPM_MCP_WORK_DIR", str(tmp_path / "wd"))
    monkeypatch.setenv("OPM_MCP_PUBLISH", str(tmp_path / "pub"))
    importlib.reload(cfg)
    try:
        assert cfg.SETTINGS.roots == (tmp_path / "models", tmp_path / "wd", tmp_path / "pub")
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


# --- Flow option policy ----------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--linear-solver=cprw"],
        ["--tolerance-mb=1e-7", "--newton-max-iterations=20"],
        ["--enable-tuning=true"],
        [],
        None,
    ],
)
def test_allowed_flow_options_pass(args):
    assert guard.validate_flow_extra_args(args) == list(args or [])


@pytest.mark.parametrize(
    "bad",
    [
        "--output-dir=/tmp/x",  # server-controlled
        "--parameter-file=params.txt",  # forbidden: points at a file
        "OTHER.DATA",  # positional: a second deck
        "--threads-per-process 4",  # two tokens in one: ambiguous
        "--some-new-option=1",  # not on the allow-list
        "--linear-solver=../../etc/x",  # value with a path
        "-x",  # short options are not accepted
    ],
)
def test_disallowed_flow_options_are_rejected(bad):
    with pytest.raises(guard.FlowOptionRejected):
        guard.validate_flow_extra_args([bad])


def test_extra_allow_env_extends_but_never_unlocks_forbidden(monkeypatch):
    object.__setattr__(cfg.SETTINGS, "flow_extra_allow", ("--some-new-option", "--output-dir"))
    try:
        assert guard.validate_flow_extra_args(["--some-new-option=1"]) == ["--some-new-option=1"]
        with pytest.raises(guard.FlowOptionRejected):
            guard.validate_flow_extra_args(["--output-dir=x"])
    finally:
        object.__setattr__(cfg.SETTINGS, "flow_extra_allow", ())


def test_core_budget(monkeypatch):
    object.__setattr__(cfg.SETTINGS, "max_cores", 8)
    try:
        guard.check_cores(8, 1)
        guard.check_cores(4, 2)
        with pytest.raises(ValueError):
            guard.check_cores(4, 4)
        with pytest.raises(ValueError):
            guard.check_cores(16, None)
    finally:
        object.__setattr__(cfg.SETTINGS, "max_cores", 0)
    guard.check_cores(64, 64)  # 0 = no limit
