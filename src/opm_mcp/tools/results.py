"""Result-file readers — UNSMRY / UNRST / EGRID / INIT.

Runs natively on Windows via :mod:`resdata` (the open-source ECL reader), so no
WSL round-trip is needed. Accepts both Windows and WSL-style paths; both are
normalised to a Windows path before resdata is asked to open them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .. import wsl


def _to_local(path: str) -> Path:
    """Normalise a caller-supplied path (Win or WSL) to a Windows Path."""
    if not path:
        raise ValueError("path is required")
    if path.startswith("/"):
        path = wsl.to_windows_path(path)
    return Path(path).expanduser()


def _case_root(path: str) -> Path:
    """Strip a known result-file extension and return the case stem path."""
    p = _to_local(path)
    if p.suffix.upper() in {".SMSPEC", ".UNSMRY", ".EGRID", ".INIT", ".UNRST", ".DATA", ".PRT"}:
        return p.with_suffix("")
    return p


def _open_summary(path: str):
    from resdata.summary import Summary  # local import keeps server startup fast

    root = str(_case_root(path))
    return Summary(root)


def list_summary_vectors(case_path: str, pattern: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List summary keys available in a case (FOPR, WBHP:*, GOPR:*, ...).

    Parameters
    ----------
    case_path : str
        Path to any of CASE / CASE.SMSPEC / CASE.UNSMRY / CASE.DATA.
    pattern : str | None
        Optional glob (``*`` and ``?``) applied case-insensitively.
    limit : int
        Cap on returned keys. Use 0 for unlimited.
    """
    summary = _open_summary(case_path)
    keys = list(summary.keys())
    if pattern:
        from fnmatch import fnmatchcase

        pat = pattern.upper()
        keys = [k for k in keys if fnmatchcase(k.upper(), pat)]
    total = len(keys)
    if limit and total > limit:
        keys = keys[:limit]
    return {
        "case_path": str(_case_root(case_path)),
        "n_total": total,
        "n_returned": len(keys),
        "keys": keys,
        "pattern": pattern,
    }


def read_summary(
    case_path: str,
    keys: list[str],
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
) -> dict[str, Any]:
    """Read one or more summary vectors as time series.

    Returns ``time`` (ISO dates) and a dict ``vectors[key] = [values...]``,
    downsampled to at most ``max_points`` to keep tool payloads small.
    """
    if not keys:
        return {"ok": False, "error": "at least one key is required"}
    summary = _open_summary(case_path)
    available = set(summary.keys())
    missing = [k for k in keys if k not in available]
    requested = [k for k in keys if k in available]
    if not requested:
        return {"ok": False, "error": "no requested keys exist in case", "missing": missing}

    dates = np.array(summary.numpy_dates)
    mask = np.ones(len(dates), dtype=bool)
    if start:
        mask &= dates >= np.datetime64(start)
    if end:
        mask &= dates <= np.datetime64(end)
    idx = np.where(mask)[0]
    if max_points and len(idx) > max_points:
        idx = idx[np.linspace(0, len(idx) - 1, max_points).astype(int)]

    vectors = {k: summary.numpy_vector(k)[idx].tolist() for k in requested}
    unit_system = getattr(summary, "unit_system", None)
    return {
        "ok": True,
        "case_path": str(_case_root(case_path)),
        "unit_system": str(unit_system) if unit_system is not None else "unknown",
        "time": [datetime.utcfromtimestamp(d.astype("datetime64[s]").astype(int)).isoformat() for d in dates[idx]],
        "vectors": vectors,
        "missing": missing,
        "n_points": len(idx),
    }


def field_summary(case_path: str) -> dict[str, Any]:
    """Return key field-level KPIs (totals + final rates) for a case."""
    summary = _open_summary(case_path)
    keys = {
        "FOPT": "field oil production total",
        "FGPT": "field gas production total",
        "FWPT": "field water production total",
        "FWIT": "field water injection total",
        "FGIT": "field gas injection total",
        "FPR": "field average pressure",
        "FOPR": "field oil rate (final)",
        "FGPR": "field gas rate (final)",
        "FWPR": "field water rate (final)",
    }
    out: dict[str, Any] = {"case_path": str(_case_root(case_path))}
    available = set(summary.keys())
    for k, label in keys.items():
        if k in available:
            arr = summary.numpy_vector(k)
            if arr.size:
                out[k] = {"label": label, "first": float(arr[0]), "last": float(arr[-1])}
    dates = summary.numpy_dates
    if len(dates):
        out["t0"] = datetime.utcfromtimestamp(dates[0].astype("datetime64[s]").astype(int)).isoformat()
        out["t_end"] = datetime.utcfromtimestamp(dates[-1].astype("datetime64[s]").astype(int)).isoformat()
        out["n_report_steps"] = int(len(dates))
    return out


def well_summary(case_path: str, well: str) -> dict[str, Any]:
    """Per-well totals + final rates (oil/gas/water/BHP) for one well name."""
    summary = _open_summary(case_path)
    wanted = ["WOPR", "WGPR", "WWPR", "WBHP", "WOPT", "WGPT", "WWPT", "WWIR", "WGIR", "WWIT", "WGIT"]
    out: dict[str, Any] = {"case_path": str(_case_root(case_path)), "well": well, "metrics": {}}
    for tag in wanted:
        key = f"{tag}:{well}"
        if key in summary.keys():
            arr = summary.numpy_vector(key)
            if arr.size:
                out["metrics"][tag] = {
                    "first": float(arr[0]),
                    "last": float(arr[-1]),
                    "min": float(arr.min()),
                    "max": float(arr.max()),
                }
    if not out["metrics"]:
        out["error"] = f"no summary vectors for well '{well}' (case has {len(summary.keys())} keys)"
    return out


def grid_info(case_path: str) -> dict[str, Any]:
    """Static info from EGRID + INIT: dimensions, active count, porosity stats."""
    from resdata.grid import Grid
    from resdata.resfile import ResdataFile

    root = _case_root(case_path)
    egrid = root.with_suffix(".EGRID")
    init = root.with_suffix(".INIT")
    if not egrid.exists():
        return {"ok": False, "error": f"EGRID not found: {egrid}"}
    g = Grid(str(egrid))
    nx, ny, nz = g.get_nx(), g.get_ny(), g.get_nz()
    info: dict[str, Any] = {
        "ok": True,
        "case_path": str(root),
        "dims": [nx, ny, nz],
        "n_active": g.get_num_active(),
        "n_cells": nx * ny * nz,
    }
    if init.exists():
        f = ResdataFile(str(init))
        for kw in ("PORO", "PERMX", "PERMY", "PERMZ", "NTG"):
            if kw in f:
                arr = np.asarray(f[kw][0])
                info[kw] = {
                    "min": float(arr.min()),
                    "max": float(arr.max()),
                    "mean": float(arr.mean()),
                    "p50": float(np.median(arr)),
                }
    return info


def restart_info(case_path: str) -> dict[str, Any]:
    """List report steps available in CASE.UNRST and which keywords they contain."""
    from resdata.resfile import ResdataFile

    root = _case_root(case_path)
    unrst = root.with_suffix(".UNRST")
    if not unrst.exists():
        return {"ok": False, "error": f"UNRST not found: {unrst}"}
    f = ResdataFile(str(unrst))
    steps = []
    keywords: set[str] = set()
    for i in range(f.num_named_kw("SEQNUM") if "SEQNUM" in f else 0):
        seq = int(f["SEQNUM"][i][0])
        steps.append(seq)
    for kw in f.headers:
        keywords.add(kw[0])
    return {
        "ok": True,
        "case_path": str(root),
        "report_steps": steps,
        "keywords": sorted(keywords),
    }


def compare_summaries(case_a: str, case_b: str, keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Compare two summary cases on the supplied keys (default = field totals)."""
    default_keys = ["FOPT", "FGPT", "FWPT", "FOPR", "FGPR", "FWPR", "FPR"]
    keys = list(keys) if keys else default_keys
    sa = _open_summary(case_a)
    sb = _open_summary(case_b)
    diffs: dict[str, Any] = {}
    for k in keys:
        if k not in sa.keys() or k not in sb.keys():
            diffs[k] = {"missing_in": [c for c, s in (("a", sa), ("b", sb)) if k not in s.keys()]}
            continue
        a_last = float(sa.numpy_vector(k)[-1])
        b_last = float(sb.numpy_vector(k)[-1])
        denom = max(abs(a_last), abs(b_last), 1e-9)
        diffs[k] = {
            "a_last": a_last,
            "b_last": b_last,
            "abs_diff": b_last - a_last,
            "rel_diff": (b_last - a_last) / denom,
        }
    return {
        "case_a": str(_case_root(case_a)),
        "case_b": str(_case_root(case_b)),
        "metrics": diffs,
    }
