"""Path translation tests — pure Python, no WSL needed."""

from __future__ import annotations

import os

import pytest

from opm_mcp.wsl import to_wsl_path, to_windows_path


@pytest.mark.parametrize(
    "win, wsl",
    [
        (r"C:\Users\me\case.DATA", "/mnt/c/Users/me/case.DATA"),
        (r"D:\models\NORNE\NORNE_ATW2013.DATA", "/mnt/d/models/NORNE/NORNE_ATW2013.DATA"),
        ("C:/Users/me/case.DATA", "/mnt/c/Users/me/case.DATA"),
        (r"E:\projects\opm\spe1.DATA", "/mnt/e/projects/opm/spe1.DATA"),
    ],
)
def test_drive_letter_paths_map_to_mnt(win: str, wsl: str) -> None:
    assert to_wsl_path(win) == wsl


@pytest.mark.parametrize(
    "raw",
    [
        "/home/ogezi/case.DATA",
        "/mnt/c/Users/me/case.DATA",
        "/usr/local/bin/flow",
    ],
)
def test_already_wsl_paths_pass_through(raw: str) -> None:
    assert to_wsl_path(raw) == raw


def test_unc_wsl_share_path_strips_distro_segment() -> None:
    inp = r"\\wsl$\Ubuntu-24.04\home\me\case.DATA"
    assert to_wsl_path(inp) == "/home/me/case.DATA"


def test_unc_localhost_form_also_works() -> None:
    inp = r"\\wsl.localhost\Ubuntu-24.04\home\me\case.DATA"
    assert to_wsl_path(inp) == "/home/me/case.DATA"


@pytest.mark.skipif(os.name != "nt", reason="to_windows_path is a no-op on POSIX hosts by design")
def test_to_windows_path_round_trip_for_mnt() -> None:
    win = r"C:\Users\me\case.DATA"
    assert to_windows_path(to_wsl_path(win)) == win


def test_empty_string_passes_through() -> None:
    assert to_wsl_path("") == ""


def test_to_windows_path_is_noop_on_posix_host(monkeypatch) -> None:
    """On a non-Windows host (CI, native-Linux backend) '/' paths are local."""
    import opm_mcp.wsl as wslmod

    monkeypatch.setattr(wslmod, "_ON_WINDOWS", False)
    assert wslmod.to_windows_path("/tmp/case/CASE.DATA") == "/tmp/case/CASE.DATA"

