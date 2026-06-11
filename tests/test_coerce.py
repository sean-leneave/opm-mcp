"""Boundary coercion tests — pure Python, no WSL needed."""

from __future__ import annotations

import pytest

from opm_mcp.coerce import arg_list, key_list


def test_arg_list_none_and_list_pass_through() -> None:
    assert arg_list(None) is None
    assert arg_list(["--enable-tuning=true"]) == ["--enable-tuning=true"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("--enable-tuning=true", ["--enable-tuning=true"]),
        ("--a --b=1", ["--a", "--b=1"]),
        ('--linear-solver "ilu0 v2"', ["--linear-solver", "ilu0 v2"]),
        ("", []),
    ],
)
def test_arg_list_splits_strings_shell_style(raw: str, expected: list[str]) -> None:
    assert arg_list(raw) == expected


def test_key_list_none_and_list_pass_through() -> None:
    assert key_list(None) is None
    assert key_list(["FOPR", "WBHP:B-1H"]) == ["FOPR", "WBHP:B-1H"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("FOPR", ["FOPR"]),
        ("FOPR,FGPR FPR", ["FOPR", "FGPR", "FPR"]),
        ("  WWCT:* ,WOPR:B-1H  ", ["WWCT:*", "WOPR:B-1H"]),
        ("", []),
    ],
)
def test_key_list_splits_on_commas_and_whitespace(raw: str, expected: list[str]) -> None:
    assert key_list(raw) == expected
