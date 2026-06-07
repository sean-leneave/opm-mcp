"""Tests for the text-based deck editor (no WSL needed with validate=False)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opm_mcp.tools import edit

_DECK = """\
RUNSPEC
TITLE
  BASE CASE
DIMENS
  5 5 3 /
SCHEDULE
WCONPROD
 'PROD' 'OPEN' 'ORAT' 500 1* 1* 1* 1* 1000 /
/
TSTEP
 30 60 90 /
END
"""


@pytest.fixture()
def base_deck(tmp_path) -> Path:
    p = tmp_path / "BASE.DATA"
    p.write_text(_DECK, encoding="utf-8")
    return p


def test_replace_regex_changes_rate(base_deck):
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "replace_regex", "pattern": "ORAT' 500", "replacement": "ORAT' 300", "count": 1}],
        validate=False,
    )
    assert out["ok"], out
    assert out["applied"][0]["n_replacements"] == 1
    text = Path(out["output_path"]).read_text(encoding="utf-8")
    assert "ORAT' 300" in text and "ORAT' 500" not in text


def test_replace_regex_miss_is_reported(base_deck):
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "replace_regex", "pattern": "NONEXISTENT", "replacement": "x"}],
        validate=False,
    )
    # A pattern that matches nothing should surface as a failure, not silently pass.
    assert out["ok"] is False
    assert out["applied"][0]["n_replacements"] == 0


def test_append_before_end_inserts_block(base_deck):
    block = "WCONPROD\n 'PROD' 'OPEN' 'ORAT' 250 /\n/"
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "append_before_end", "text": block}],
        validate=False,
    )
    assert out["ok"], out
    text = Path(out["output_path"]).read_text(encoding="utf-8")
    # New control must appear before END.
    assert text.index("ORAT' 250") < text.index("END")


def test_set_title(base_deck):
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "set_title", "text": "NEW TITLE"}],
        validate=False,
    )
    assert out["ok"], out
    text = Path(out["output_path"]).read_text(encoding="utf-8")
    assert "NEW TITLE" in text and "BASE CASE" not in text


def test_insert_after_keyword(base_deck):
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "insert_after_keyword", "keyword": "SCHEDULE", "text": "-- injected"}],
        validate=False,
    )
    assert out["ok"], out
    text = Path(out["output_path"]).read_text(encoding="utf-8")
    assert text.index("-- injected") > text.index("SCHEDULE")


def test_original_is_never_mutated(base_deck):
    before = base_deck.read_text(encoding="utf-8")
    edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "replace_regex", "pattern": "500", "replacement": "300"}],
        validate=False,
    )
    assert base_deck.read_text(encoding="utf-8") == before


def test_unknown_op_fails_cleanly(base_deck):
    out = edit.clone_deck(
        str(base_deck),
        overrides=[{"op": "frobnicate", "text": "x"}],
        validate=False,
    )
    assert out["ok"] is False
    assert "unknown op" in (out.get("error") or "")
