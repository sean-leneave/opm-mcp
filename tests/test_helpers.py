"""Unit tests for pure helpers added in v0.2.0 (no resdata/WSL needed)."""

from __future__ import annotations

import pytest

from opm_mcp.tools.analysis import _parse_obs_date
from opm_mcp.tools.results import _expand_keys


class FakeSummary:
    def __init__(self, keys):
        self._keys = list(keys)

    def keys(self):
        return list(self._keys)


KEYS = ["FOPR", "FPR", "WBHP:PROD", "WBHP:INJ", "WOPR:PROD", "WWCT:PROD"]


def test_expand_plain_keys_pass_through():
    resolved, unmatched, truncated = _expand_keys(FakeSummary(KEYS), ["FOPR", "NOPE"])
    assert resolved == ["FOPR", "NOPE"]  # existence is the caller's check
    assert unmatched == []
    assert truncated is False


def test_expand_wildcard_matches_sorted():
    resolved, unmatched, _ = _expand_keys(FakeSummary(KEYS), ["WBHP:*"])
    assert resolved == ["WBHP:INJ", "WBHP:PROD"]
    assert unmatched == []


def test_expand_wildcard_case_insensitive():
    resolved, _, _ = _expand_keys(FakeSummary(KEYS), ["wbhp:*"])
    assert resolved == ["WBHP:INJ", "WBHP:PROD"]


def test_expand_unmatched_pattern_reported():
    resolved, unmatched, _ = _expand_keys(FakeSummary(KEYS), ["GOPR:*"])
    assert resolved == []
    assert unmatched == ["GOPR:*"]


def test_expand_dedupes_and_caps():
    resolved, _, truncated = _expand_keys(FakeSummary(KEYS), ["W*", "WBHP:PROD"], cap=3)
    assert truncated is True
    assert len(resolved) == 3
    assert len(set(resolved)) == 3


@pytest.mark.parametrize(
    "raw",
    ["2026-04-01", "01.04.2026", "04/01/2026", "01-Apr-2026", "20260401"],
)
def test_parse_obs_date_formats(raw):
    d = _parse_obs_date(raw)
    assert (d.year, d.month, d.day) == (2026, 4, 1)


def test_parse_obs_date_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_obs_date("not-a-date")
