"""Tests for the efficient tail reader in the job manager (no WSL needed)."""

from __future__ import annotations

from opm_mcp import jobs


def test_read_tail_basic(tmp_path):
    p = tmp_path / "flow.log"
    p.write_text("".join(f"line {i}\n" for i in range(1000)), encoding="utf-8")
    tail = jobs._read_tail(p, 10)
    assert len(tail) == 10
    assert tail[-1].strip() == "line 999"
    assert tail[0].strip() == "line 990"


def test_read_tail_more_than_file(tmp_path):
    p = tmp_path / "flow.log"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    tail = jobs._read_tail(p, 100)
    assert [t.strip() for t in tail] == ["a", "b", "c"]


def test_read_tail_missing_file(tmp_path):
    assert jobs._read_tail(tmp_path / "nope.log", 10) == []


def test_read_tail_crosses_block_boundary(tmp_path):
    # Lines long enough that the tail spans several 8 KiB read blocks.
    p = tmp_path / "flow.log"
    p.write_text("".join(f"{i}:{'x' * 200}\n" for i in range(500)), encoding="utf-8")
    tail = jobs._read_tail(p, 50)
    assert len(tail) == 50
    assert tail[-1].startswith("499:")
    assert tail[0].startswith("450:")


def test_last_progress_line_prefers_report_step(tmp_path):
    p = tmp_path / "flow.log"
    p.write_text(
        "Reading deck\nReport step 3/10 at day 90\nsome trailing noise\n",
        encoding="utf-8",
    )
    assert "Report step 3/10" in jobs._last_progress_line(str(p))
