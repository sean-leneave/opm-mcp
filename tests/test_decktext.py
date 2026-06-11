"""deck_text / deck_grep tests — pure Python, no WSL needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from opm_mcp.tools.decktext import collect_files, deck_grep, deck_text

DECK = """\
RUNSPEC
TITLE
TEST CASE FOR DECKTEXT
-- PATHS gives include files symbolic prefixes, like GASLIFT-12's $MODEL05
PATHS
 'INC'  './include'  /
/
GRID
INCLUDE
 '$INC/grid.inc' /
PROPS
INCLUDE
 'props.inc' /  -- relative to the deck folder
SCHEDULE
WCONPROD
 'P-1' OPEN ORAT 1500.0 1* 1* 3000.0 1* 100.0 30 1 /
/
END
"""

GRID_INC = """\
DX
 100*50.0 /
-- a comment mentioning WCONPROD that should still be greppable
"""

PROPS_INC = """\
SWOF
 0.1 0.0 1.0 0.0
 0.9 1.0 0.0 0.0 /
"""


@pytest.fixture()
def deck(tmp_path: Path) -> Path:
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "grid.inc").write_text(GRID_INC, encoding="utf-8")
    (tmp_path / "props.inc").write_text(PROPS_INC, encoding="utf-8")
    deck = tmp_path / "TEST.DATA"
    deck.write_text(DECK, encoding="utf-8")
    return deck


def test_collect_files_resolves_paths_aliases_and_relative_includes(deck: Path) -> None:
    files, missing = collect_files(str(deck))
    names = [f.name for f in files]
    assert names[0] == "TEST.DATA"
    assert "grid.inc" in names  # via $INC alias
    assert "props.inc" in names  # plain relative
    assert missing == []


def test_deck_text_slices_with_line_numbers(deck: Path) -> None:
    out = deck_text(str(deck), start_line=2, n_lines=2)
    assert out["ok"] is True
    assert out["start_line"] == 2 and out["end_line"] == 3
    assert "TITLE" in out["text"] and "TEST CASE" in out["text"]
    assert out["file_win"].endswith("TEST.DATA")
    assert "file_wsl" in out


def test_deck_text_selects_include_by_bare_name(deck: Path) -> None:
    out = deck_text(str(deck), file="grid.inc")
    assert out["ok"] is True
    assert out["file_win"].endswith("grid.inc")
    assert "DX" in out["text"]


def test_deck_text_unknown_include_lists_tree(deck: Path) -> None:
    out = deck_text(str(deck), file="nope.inc")
    assert out["ok"] is False
    assert any(p.endswith("TEST.DATA") for p in out["files_in_tree"])


def test_deck_grep_finds_keyword_in_deck_and_includes(deck: Path) -> None:
    out = deck_grep(str(deck), r"^SWOF|^WCONPROD", context=1)
    assert out["ok"] is True
    hits = {(Path(m["file"]).name, m["text"].strip()) for m in out["matches"]}
    assert ("TEST.DATA", "WCONPROD") in hits
    assert ("props.inc", "SWOF") in hits


def test_deck_grep_respects_search_includes_false(deck: Path) -> None:
    out = deck_grep(str(deck), "SWOF", search_includes=False)
    assert out["n_matches"] == 0
    assert len(out["files_searched"]) == 1


def test_deck_grep_caps_matches(deck: Path) -> None:
    out = deck_grep(str(deck), ".", max_matches=3)
    assert out["n_matches"] == 3
    assert out["truncated"] is True


def test_deck_grep_bad_regex_is_reported(deck: Path) -> None:
    out = deck_grep(str(deck), "([")
    assert out["ok"] is False
    assert "bad regex" in out["error"]
