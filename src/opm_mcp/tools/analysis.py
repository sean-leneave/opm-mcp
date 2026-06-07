"""Reservoir-engineering analytics on Flow results.

Three tools that turn the server from a *reader* into an *analyst*:

* :func:`material_balance_check` — OOIP/recovery, an exact oil mass-balance
  drift check, and a (clearly-flagged, surface-rate) voidage replacement ratio.
* :func:`flow_convergence_report` — parses the ``INFOSTEP``/``INFOITER`` files
  Flow writes under ``--output-extra-convergence-info`` and tells you where the
  solver time went and which steps were wasted on cut/non-converged iterations.
* :func:`restart_property` — pulls a dynamic 3D property (PRESSURE/SWAT/SGAS/
  SOIL) from the UNRST at a report step and reduces it to per-layer statistics,
  so spatial reasoning (sweep, coning, depletion) is possible without ResInsight.

All readers run natively on Windows via :mod:`resdata`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from . import results as _results


# ---------------------------------------------------------------------------
# Material balance
# ---------------------------------------------------------------------------


def material_balance_check(case_path: str) -> dict[str, Any]:
    """Field material-balance QC from summary totals.

    Returns OOIP/OGIP (initial in-place), cumulative production/injection,
    recovery factor, an **exact** oil mass-balance drift
    ``(FOIP0 - FOIPn - FOPT) / FOIP0`` (should be ~0), and a **surface** voidage
    replacement ratio (labelled as such — a true reservoir-condition VRR needs
    PVT FVFs, which this does not apply).
    """
    summary = _results._open_summary(case_path)
    avail = set(summary.keys())

    def last(key: str) -> float | None:
        if key not in avail:
            return None
        arr = summary.numpy_vector(key)
        return float(arr[-1]) if arr.size else None

    def first(key: str) -> float | None:
        if key not in avail:
            return None
        arr = summary.numpy_vector(key)
        return float(arr[0]) if arr.size else None

    foip0, foipn = first("FOIP"), last("FOIP")
    fgip0, fgipn = first("FGIP"), last("FGIP")
    fopt = last("FOPT") or 0.0
    fgpt = last("FGPT") or 0.0
    fwpt = last("FWPT") or 0.0
    fwit = last("FWIT") or 0.0
    fgit = last("FGIT") or 0.0

    out: dict[str, Any] = {
        "ok": True,
        "case_path": str(_results._case_root(case_path)),
        "unit_system": str(getattr(summary, "unit_system", "unknown")),
        "initial": {"FOIP": foip0, "FGIP": fgip0},
        "current": {"FOIP": foipn, "FGIP": fgipn},
        "cumulative": {
            "FOPT": fopt,
            "FGPT": fgpt,
            "FWPT": fwpt,
            "FWIT": fwit,
            "FGIT": fgit,
        },
    }

    notes: list[str] = []

    # Recovery factor.
    if foip0 and foip0 > 0:
        out["recovery_factor_oil"] = fopt / foip0

    # Exact oil mass balance: nothing injects oil, so FOIP0 should equal
    # FOIPn + cumulative oil produced. Drift flags reporting/numerical issues.
    if foip0 is not None and foipn is not None and foip0 > 0:
        drift = (foip0 - foipn - fopt) / foip0
        out["oil_mass_balance_drift"] = drift
        out["oil_mass_balance_ok"] = abs(drift) < 1e-3
        if abs(drift) >= 1e-3:
            notes.append(
                f"Oil MB drift {drift:.2%} exceeds 0.1% — check reporting "
                f"frequency or numerical losses."
            )
    else:
        notes.append("FOIP not in SUMMARY — add FOIP (and FGIP) to enable mass-balance QC.")

    # Surface voidage replacement ratio (approximate — NOT reservoir-condition).
    produced_surface = fopt + fwpt + fgpt
    injected_surface = fwit + fgit
    if produced_surface > 0:
        out["voidage_replacement_ratio_surface"] = injected_surface / produced_surface
        notes.append(
            "VRR shown is a SURFACE-volume ratio; a true reservoir-condition VRR "
            "needs Bo/Bw/Bg from PVT (see the pyrestoolbox MCP for PVT)."
        )

    out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Convergence / performance
# ---------------------------------------------------------------------------


def flow_convergence_report(case_path: str, worst_n: int = 5) -> dict[str, Any]:
    """Summarise solver performance from Flow's INFOSTEP / INFOITER files.

    Requires the run to have been launched with
    ``--output-extra-convergence-info=steps[,iterations]`` (the default for
    :func:`opm_mcp.tools.flow.run_flow`). Reports total wall time by stage,
    Newton/linear iteration counts, time wasted on non-converged steps, and the
    worst timesteps by iteration count.
    """
    root = _results._case_root(case_path)
    infostep = Path(str(root) + ".INFOSTEP")
    if not infostep.exists():
        return {
            "ok": False,
            "error": f"INFOSTEP not found: {infostep}",
            "hint": "Re-run with run_flow(convergence_info='steps,iterations').",
        }

    rows, headers = _read_whitespace_table(infostep)
    if not rows:
        return {"ok": False, "error": "INFOSTEP is empty"}

    col = {h: i for i, h in enumerate(headers)}

    def colvals(name: str) -> np.ndarray:
        idx = col.get(name)
        if idx is None:
            return np.zeros(len(rows))
        return np.array([_safe_float(r[idx]) if idx < len(r) else 0.0 for r in rows])

    tstep = colvals("TStep(day)")
    newt = colvals("NewtIt")
    linit = colvals("LinIt")
    conv = colvals("Conv")
    stage_cols = ["Assembly", "LSetup", "LSolve", "LocSol", "Update", "Output"]
    stage_time = {c: float(colvals(c).sum()) for c in stage_cols if c in col}
    total_time = float(sum(stage_time.values()))

    converged_mask = conv >= 0.5
    wasted_mask = ~converged_mask
    wasted_time = float(
        sum(colvals(c)[wasted_mask].sum() for c in stage_cols if c in col)
    )

    # Worst timesteps by Newton iterations.
    order = np.argsort(-newt)[:worst_n]
    worst = []
    time_idx = col.get("Time(day)")
    for i in order:
        worst.append(
            {
                "time_day": _safe_float(rows[i][time_idx]) if time_idx is not None else None,
                "tstep_day": float(tstep[i]),
                "newton_iters": int(newt[i]),
                "linear_iters": int(linit[i]),
                "converged": bool(converged_mask[i]),
            }
        )

    report: dict[str, Any] = {
        "ok": True,
        "case_path": str(root),
        "n_timesteps": len(rows),
        "n_converged_steps": int(converged_mask.sum()),
        "n_wasted_steps": int(wasted_mask.sum()),
        "total_newton_iters": int(newt.sum()),
        "total_linear_iters": int(linit.sum()),
        "avg_newton_per_step": float(newt.mean()) if len(newt) else 0.0,
        "max_newton_in_step": int(newt.max()) if len(newt) else 0,
        "avg_tstep_day": float(tstep[tstep > 0].mean()) if np.any(tstep > 0) else 0.0,
        "min_tstep_day": float(tstep[tstep > 0].min()) if np.any(tstep > 0) else 0.0,
        "max_tstep_day": float(tstep.max()) if len(tstep) else 0.0,
        "wall_time_s_by_stage": stage_time,
        "wall_time_s_total": total_time,
        "wall_time_s_wasted_on_cut_steps": wasted_time,
        "wasted_time_fraction": (wasted_time / total_time) if total_time > 0 else 0.0,
        "worst_steps_by_newton": worst,
    }

    # Optional: worst convergence residuals from INFOITER, if present.
    infoiter = Path(str(root) + ".INFOITER")
    if infoiter.exists():
        report["worst_residuals"] = _infoiter_worst(infoiter)

    notes = []
    if report["wasted_time_fraction"] > 0.15:
        notes.append(
            f"{report['wasted_time_fraction']:.0%} of solver time was spent on "
            f"non-converged/cut steps — consider TUNING (TSMINZ/TSFMAX) or tighter "
            f"initial timestep."
        )
    if report["max_newton_in_step"] >= 12:
        notes.append(
            "Some steps needed many Newton iterations; check well controls / "
            "saturation-function endpoints near those times."
        )
    report["notes"] = notes
    return report


def _infoiter_worst(path: Path, worst_n: int = 5) -> dict[str, Any]:
    rows, headers = _read_whitespace_table(path)
    if not rows:
        return {}
    col = {h: i for i, h in enumerate(headers)}
    result: dict[str, Any] = {}
    for phase in ("CNV_Oil", "CNV_Water", "CNV_Gas", "MB_Oil", "MB_Water", "MB_Gas"):
        idx = col.get(phase)
        if idx is None:
            continue
        vals = np.array([_safe_float(r[idx]) if idx < len(r) else 0.0 for r in rows])
        if vals.size:
            result[phase] = {"max_abs": float(np.nanmax(np.abs(vals)))}
    return result


# ---------------------------------------------------------------------------
# Restart property extraction
# ---------------------------------------------------------------------------

_DERIVED = {"SOIL"}


def restart_property(
    case_path: str,
    keyword: str = "PRESSURE",
    report_step: int | None = None,
    n_histogram_bins: int = 10,
) -> dict[str, Any]:
    """Per-layer statistics for a dynamic property at a UNRST report step.

    Parameters
    ----------
    keyword : str
        ``PRESSURE``, ``SWAT``, ``SGAS``, or the derived ``SOIL``
        (= 1 - SWAT - SGAS).
    report_step : int | None
        SEQNUM value to read. ``None`` = last available step.

    Returns overall stats, a per-K-layer table (pore-volume-weighted mean when
    INIT/PORV is available, else cell-count mean), and a coarse histogram.
    """
    from resdata.grid import Grid
    from resdata.resfile import ResdataFile

    root = _results._case_root(case_path)
    egrid = Path(str(root) + ".EGRID")
    unrst = Path(str(root) + ".UNRST")
    if not egrid.exists():
        return {"ok": False, "error": f"EGRID not found: {egrid}"}
    if not unrst.exists():
        return {"ok": False, "error": f"UNRST not found: {unrst}"}

    kw = keyword.upper()
    f = ResdataFile(str(unrst))
    seq = [int(f["SEQNUM"][i][0]) for i in range(f.num_named_kw("SEQNUM"))]
    if not seq:
        return {"ok": False, "error": "no report steps (SEQNUM) in UNRST"}
    target = seq[-1] if report_step is None else report_step
    if target not in seq:
        return {"ok": False, "error": f"report step {target} not in {seq}"}
    step_index = seq.index(target)

    try:
        values = _restart_array(f, kw, step_index)
    except KeyError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "available_dynamic_keywords": sorted({h[0] for h in f.headers}),
        }

    k_index = _k_index_array(str(egrid))
    if k_index.size != values.size:
        return {
            "ok": False,
            "error": f"grid/property size mismatch ({k_index.size} vs {values.size})",
        }
    porv = _porv_array(str(root), values.size)

    nz = int(k_index.max()) + 1 if k_index.size else 0
    layers = []
    for k in range(nz):
        m = k_index == k
        vk = values[m]
        if vk.size == 0:
            continue
        entry = {
            "k": k + 1,  # 1-based for the engineer
            "n_cells": int(vk.size),
            "min": float(vk.min()),
            "max": float(vk.max()),
            "mean": float(vk.mean()),
        }
        if porv is not None:
            w = porv[m]
            wsum = float(w.sum())
            entry["pv_weighted_mean"] = float((vk * w).sum() / wsum) if wsum > 0 else None
        layers.append(entry)

    counts, edges = np.histogram(values, bins=n_histogram_bins)

    return {
        "ok": True,
        "case_path": str(root),
        "keyword": kw,
        "report_step": target,
        "available_report_steps": seq,
        "overall": {
            "n_cells": int(values.size),
            "min": float(values.min()),
            "max": float(values.max()),
            "mean": float(values.mean()),
            "pv_weighted_mean": (
                float((values * porv).sum() / porv.sum()) if porv is not None and porv.sum() > 0 else None
            ),
        },
        "by_layer": layers,
        "histogram": {"counts": counts.tolist(), "bin_edges": [float(e) for e in edges]},
        "pv_weighted": porv is not None,
    }


def _restart_array(f, kw: str, step_index: int) -> np.ndarray:
    if kw in _DERIVED and kw == "SOIL":
        swat = np.array(f.iget_named_kw("SWAT", step_index)) if "SWAT" in f else None
        sgas = np.array(f.iget_named_kw("SGAS", step_index)) if "SGAS" in f else 0.0
        if swat is None:
            raise KeyError("SOIL requested but SWAT is not in UNRST")
        return 1.0 - swat - (sgas if isinstance(sgas, np.ndarray) else 0.0)
    if kw not in f:
        raise KeyError(f"{kw} not in UNRST")
    return np.array(f.iget_named_kw(kw, step_index))


@lru_cache(maxsize=8)
def _k_index_array(egrid_path: str) -> np.ndarray:
    """K-layer index (0-based) per active cell. Cached per grid."""
    from resdata.grid import Grid

    g = Grid(egrid_path)
    n = g.get_num_active()
    return np.fromiter((g.get_ijk(active_index=a)[2] for a in range(n)), dtype=np.int32, count=n)


def _porv_array(root: str, n_active: int) -> np.ndarray | None:
    from resdata.resfile import ResdataFile

    init = Path(root + ".INIT")
    if not init.exists():
        return None
    try:
        fi = ResdataFile(str(init))
        if "PORV" not in fi:
            return None
        porv = np.array(fi["PORV"][0])
        # PORV is over all cells; keep only the active ones if needed.
        if porv.size == n_active:
            return porv
        # Fall back: cannot align — skip weighting.
        return None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_whitespace_table(path: Path) -> tuple[list[list[str]], list[str]]:
    """Parse a whitespace-delimited table with a single header row."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], []
    rows: list[list[str]] = []
    headers: list[str] = []
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if not headers:
            headers = parts
            continue
        rows.append(parts)
    return rows, headers


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0
