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
    porv = _porv_array(str(root), str(egrid), values.size)

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
def _grid_geometry(egrid_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, tuple[int, int, int]]:
    """Per-active-cell ``(i, j, k, global_index)`` arrays + ``(nx, ny, nz)``.

    One cached pass over the grid serves layer bucketing (k), map rendering
    (i, j) and global→active compression (global_index) alike.
    """
    from resdata.grid import Grid

    g = Grid(egrid_path)
    n = g.get_num_active()
    iarr = np.empty(n, dtype=np.int32)
    jarr = np.empty(n, dtype=np.int32)
    karr = np.empty(n, dtype=np.int32)
    gidx = np.empty(n, dtype=np.int64)
    for a in range(n):
        i, j, k = g.get_ijk(active_index=a)
        iarr[a], jarr[a], karr[a] = i, j, k
        gidx[a] = g.get_global_index(active_index=a)
    return iarr, jarr, karr, gidx, (g.get_nx(), g.get_ny(), g.get_nz())


def _k_index_array(egrid_path: str) -> np.ndarray:
    """K-layer index (0-based) per active cell."""
    return _grid_geometry(egrid_path)[2]


def _porv_array(root: str, egrid_path: str, n_active: int) -> np.ndarray | None:
    """Active-cell pore volumes from INIT, or None if unavailable.

    PORV is the one INIT keyword written at **global** grid size (inactive
    cells hold zeros) — every real field model with inactive cells hits this,
    so compress global→active via the grid index map rather than silently
    dropping the weighting.
    """
    from resdata.resfile import ResdataFile

    init = Path(root + ".INIT")
    if not init.exists():
        return None
    try:
        fi = ResdataFile(str(init))
        if "PORV" not in fi:
            return None
        porv = np.array(fi["PORV"][0])
        if porv.size == n_active:
            return porv
        _, _, _, gidx, (nx, ny, nz) = _grid_geometry(egrid_path)
        if porv.size == nx * ny * nz:
            return porv[gidx]
        return None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Observed-vs-simulated (history-match QC)
# ---------------------------------------------------------------------------


def compare_to_observed(
    case_path: str,
    observed_csv: str,
    keys: list[str] | None = None,
    plot: bool = True,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Compare simulated summary vectors against observed data and quantify misfit.

    CSV layout: first column is the date (ISO ``YYYY-MM-DD``; ``DD.MM.YYYY``,
    ``MM/DD/YYYY``, ``DD-Mon-YYYY`` also accepted), remaining headers are
    summary keys (``WBHP:PROD``, ``FOPR``, ...). Blank cells are skipped, so
    sparse observations are fine. Simulated values are interpolated to the
    observation times; misfit is reported per key (n, RMSE, bias, MAE, NRMSE)
    and optionally plotted (sim line + obs markers, one panel per key).
    """
    import csv as _csv

    path = _results._to_local(observed_csv)
    if not path.exists():
        return {"ok": False, "error": f"observed CSV not found: {path}"}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in _csv.reader(fh) if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        return {"ok": False, "error": "CSV needs a header row plus at least one data row"}
    headers = [h.strip() for h in rows[0]]
    obs_keys = headers[1:]
    if keys:
        wanted_set = {k.upper() for k in keys}
        obs_keys = [k for k in obs_keys if k.upper() in wanted_set]
    if not obs_keys:
        return {"ok": False, "error": "no observed columns match the requested keys", "csv_columns": headers[1:]}

    obs_dates: list[Any] = []
    obs_cols: dict[str, list[float | None]] = {k: [] for k in obs_keys}
    for r in rows[1:]:
        try:
            obs_dates.append(_parse_obs_date(r[0]))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        for k in obs_keys:
            idx = headers.index(k)
            cell = r[idx].strip() if idx < len(r) else ""
            obs_cols[k].append(float(cell) if cell else None)

    summary = _results._open_summary(case_path)
    available = set(summary.keys())
    days = np.asarray(summary.days, dtype=float)
    dates = np.asarray(summary.numpy_dates)
    # Summary "days" are elapsed since simulation start; anchor obs dates there.
    start = dates[0] - np.timedelta64(int(round(days[0] * 86400)), "s")
    obs_days = np.array(
        [(np.datetime64(d) - start) / np.timedelta64(1, "s") / 86400.0 for d in obs_dates]
    )

    metrics: dict[str, Any] = {}
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    missing: list[str] = []
    for k in obs_keys:
        if k not in available:
            missing.append(k)
            continue
        vals = obs_cols[k]
        mask = np.array([v is not None for v in vals])
        if not mask.any():
            continue
        t = obs_days[mask]
        o = np.array([v for v in vals if v is not None], dtype=float)
        sim_vec = summary.numpy_vector(k)
        s = np.interp(t, days, sim_vec)
        resid = s - o
        span = float(o.max() - o.min())
        denom = span if span > 0 else max(abs(float(o.mean())), 1e-12)
        n_outside = int(((t < days[0]) | (t > days[-1])).sum())
        metrics[k] = {
            "n_obs": int(o.size),
            "rmse": float(np.sqrt(np.mean(resid**2))),
            "bias_sim_minus_obs": float(resid.mean()),
            "mae": float(np.abs(resid).mean()),
            "nrmse": float(np.sqrt(np.mean(resid**2)) / denom),
            "unit": _results._unit_of(summary, k),
            "n_obs_outside_sim_window": n_outside,
        }
        series[k] = (t, o, sim_vec)

    out: dict[str, Any] = {
        "ok": True,
        "case_path": str(_results._case_root(case_path)),
        "observed_csv": str(path),
        "metrics": metrics,
        "missing_in_summary": missing,
    }
    if plot and series:
        out["png_path"] = _plot_obs_panels(case_path, days, series, output_path)
    return out


def _parse_obs_date(raw: str):
    from datetime import datetime as _dt

    s = raw.strip()
    try:
        return _dt.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%m/%d/%Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return _dt.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable date {raw!r} (use ISO YYYY-MM-DD)")


def _plot_obs_panels(case_path, sim_days, series, output_path: str | None) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..config import SETTINGS

    keys = list(series)[:6]
    n = len(keys)
    ncols = 2 if n > 1 else 1
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.0 * ncols, 3.4 * nrows), squeeze=False)
    case_name = Path(str(_results._case_root(case_path))).name
    fig.suptitle(f"{case_name}: simulated vs observed", fontsize=12)
    for ax, k in zip(axes.flat, keys):
        t, o, sim_vec = series[k]
        ax.plot(sim_days, sim_vec, "-", lw=1.4, label="sim")
        ax.plot(t, o, "o", ms=4, mfc="none", label="obs")
        ax.set_title(k, fontsize=10, loc="left")
        ax.set_xlabel("Elapsed time (days)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
    for ax in axes.flat[n:]:
        ax.axis("off")
    if output_path:
        png = Path(output_path)
        png.parent.mkdir(parents=True, exist_ok=True)
    else:
        d = SETTINGS.work_dir / "artifacts" / "plots"
        d.mkdir(parents=True, exist_ok=True)
        png = d / f"{case_name}_obs_compare.png"
    fig.tight_layout()
    fig.savefig(png, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return str(png)


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
