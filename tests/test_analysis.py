"""Tests for the INFOSTEP/INFOITER table parsing in analysis tools."""

from __future__ import annotations


from opm_mcp.tools import analysis

# A trimmed real INFOSTEP: header + 3 steps, last one non-converged (Conv=0).
_INFOSTEP = """\
  Time(day)  TStep(day)  Assembly    LSetup    LSolve    LocSol    Update    Output WellIt Lins NewtIt LinIt Conv
          0           1    0.0010    0.0110    0.0121    0.0000    0.0007    0.0000      0   12     11     6    1
          1 0.7272727273    0.0003    0.0004    0.0005    0.0000    0.0002    0.0000      0    3      2     0    1
   1.727272 2.181818182    0.0002    0.0004    0.0005    0.0000    0.0001    0.0000      0    3      4     0    0
"""


def test_whitespace_table_parses_header_and_rows(tmp_path):
    p = tmp_path / "CASE.INFOSTEP"
    p.write_text(_INFOSTEP, encoding="utf-8")
    rows, headers = analysis._read_whitespace_table(p)
    assert headers[0] == "Time(day)"
    assert headers[-1] == "Conv"
    assert len(rows) == 3


def test_convergence_report_counts(tmp_path):
    (tmp_path / "CASE.INFOSTEP").write_text(_INFOSTEP, encoding="utf-8")
    rep = analysis.flow_convergence_report(str(tmp_path / "CASE.DATA"))
    assert rep["ok"], rep
    assert rep["n_timesteps"] == 3
    assert rep["n_converged_steps"] == 2
    assert rep["n_wasted_steps"] == 1
    assert rep["total_newton_iters"] == 11 + 2 + 4
    assert rep["max_newton_in_step"] == 11
    # The non-converged step's stage time counts as wasted.
    assert rep["wall_time_s_wasted_on_cut_steps"] > 0


def test_convergence_report_missing_file(tmp_path):
    rep = analysis.flow_convergence_report(str(tmp_path / "NOPE.DATA"))
    assert rep["ok"] is False
    assert "INFOSTEP not found" in rep["error"]


def test_safe_float():
    assert analysis._safe_float("3.14") == 3.14
    assert analysis._safe_float("notanumber") == 0.0
    assert analysis._safe_float("") == 0.0
