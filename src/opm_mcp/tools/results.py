"""Result-file readers — UNSMRY / UNRST / EGRID / INIT.

Runs natively on Windows via :mod:`resdata` (the open-source ECL reader), so no
WSL round-trip is needed. Accepts both Windows and WSL-style paths; both are
normalised to a Windows path before resdata is asked to open them.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def _iso(dt64: np.datetime64) -> str:
    """ISO-8601 string from a numpy datetime64 (timezone-aware UTC)."""
    secs = dt64.astype("datetime64[s]").astype(int)
    return datetime.fromtimestamp(int(secs), tz=timezone.utc).replace(tzinfo=None).isoformat()


# Small mtime-keyed cache so repeated reads of the same case in one session
# (read_summary -> field_summary -> plot_summary -> material_balance) don't
# re-parse the SMSPEC each time. Norne has ~10k vectors; this matters.
_SUMMARY_CACHE: dict[str, tuple[float, Any]] = {}


def _open_summary(path: str):
    from resdata.summary import Summary  # local import keeps server startup fast

    root = str(_case_root(path))
    smspec = Path(root + ".SMSPEC")
    try:
        mtime = smspec.stat().st_mtime
    except OSError:
        mtime = -1.0
    cached = _SUMMARY_CACHE.get(root)
    if cached and cached[0] == mtime:
        return cached[1]
    summary = Summary(root)
    _SUMMARY_CACHE[root] = (mtime, summary)
    return summary


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
    resolved, unmatched, truncated = _expand_keys(summary, keys)
    available = set(summary.keys())
    missing = [k for k in resolved if k not in available] + unmatched
    requested = [k for k in resolved if k in available]
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
    units = {k: _unit_of(summary, k) for k in requested}
    unit_system = getattr(summary, "unit_system", None)
    return {
        "ok": True,
        "case_path": str(_case_root(case_path)),
        "unit_system": str(unit_system) if unit_system is not None else "unknown",
        "units": units,
        "time": [_iso(d) for d in dates[idx]],
        "days": np.asarray(summary.days)[idx].tolist() if hasattr(summary, "days") else None,
        "vectors": vectors,
        "missing": missing,
        "n_points": len(idx),
        "wildcard_truncated": truncated,
    }


def _unit_of(summary, key: str) -> str | None:
    """Per-vector engineering unit (e.g. STB/DAY, PSIA, MSCF/DAY)."""
    try:
        return summary.unit(key)
    except Exception:
        return None


def _expand_keys(summary, keys: list[str], cap: int = 40) -> tuple[list[str], list[str], bool]:
    """Expand glob patterns (``WWCT:*``, ``GOPR:G?``) against the SMSPEC keys.

    Returns ``(resolved, unmatched_patterns, truncated)``. Plain keys pass
    through untouched (existence is checked by the caller); patterns that match
    nothing are reported, not silently dropped. ``cap`` bounds the expansion so
    a careless ``*`` on a 10k-vector case doesn't flood the payload.
    """
    from fnmatch import fnmatchcase

    available = list(summary.keys())
    out: list[str] = []
    unmatched: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if any(c in key for c in "*?["):
            pat = key.upper()
            matches = sorted(k for k in available if fnmatchcase(k.upper(), pat))
            if not matches:
                unmatched.append(key)
            for m in matches:
                if m not in seen:
                    seen.add(m)
                    out.append(m)
        else:
            if key not in seen:
                seen.add(key)
                out.append(key)
    truncated = bool(cap) and len(out) > cap
    return (out[:cap] if truncated else out), unmatched, truncated


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
                out[k] = {
                    "label": label,
                    "unit": _unit_of(summary, k),
                    "first": float(arr[0]),
                    "last": float(arr[-1]),
                }
    dates = summary.numpy_dates
    if len(dates):
        out["t0"] = _iso(dates[0])
        out["t_end"] = _iso(dates[-1])
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
                    "unit": _unit_of(summary, key),
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


def rft_data(case_path: str, well: str | None = None, date: str | None = None) -> dict[str, Any]:
    """Read RFT surveys (pressure vs depth at well connections) from CASE.RFT.

    Used to QC perforation pressures against the reservoir gradient and to
    compare with wireline formation-test data. Requires the deck to request
    RFT output (``WRFTPLT 'WELL' 'REPT' /`` in SCHEDULE).

    Parameters
    ----------
    well : str | None
        Filter to one well name (case-insensitive). None = all wells.
    date : str | None
        Filter to surveys whose date starts with this string (``"2026-04"``).

    Cell ``i``/``j``/``k`` are reported 1-based, matching deck conventions.
    """
    from resdata.rft import ResdataRFTFile

    root = _case_root(case_path)
    rft_path = Path(str(root) + ".RFT")
    if not rft_path.exists():
        return {
            "ok": False,
            "error": f"RFT file not found: {rft_path}",
            "hint": "Add `WRFTPLT 'WELLNAME' 'REPT' /` to the SCHEDULE section and rerun.",
        }
    f = ResdataRFTFile(str(rft_path))
    surveys: list[dict[str, Any]] = []
    for rft in f:
        wname = rft.get_well_name() if hasattr(rft, "get_well_name") else getattr(rft, "name", "?")
        d = rft.get_date() if hasattr(rft, "get_date") else getattr(rft, "date", None)
        dstr = str(d)
        if well and wname.upper() != well.upper():
            continue
        if date and not dstr.startswith(date):
            continue
        cells = []
        for c in rft:
            cells.append(
                {
                    "depth": _maybe_float(getattr(c, "depth", None)),
                    "pressure": _maybe_float(getattr(c, "pressure", None)),
                    "swat": _maybe_float(getattr(c, "swat", None)),
                    "sgas": _maybe_float(getattr(c, "sgas", None)),
                    "i": _cell_index(c, "i"),
                    "j": _cell_index(c, "j"),
                    "k": _cell_index(c, "k"),
                }
            )
        cells.sort(key=lambda x: (x["depth"] is None, x["depth"]))
        surveys.append({"well": wname, "date": dstr, "n_cells": len(cells), "cells": cells})
    return {
        "ok": True,
        "case_path": str(root),
        "n_surveys": len(surveys),
        "surveys": surveys,
    }


def _maybe_float(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _cell_index(cell, axis: str) -> int | None:
    """1-based cell index from a resdata RFT cell, tolerant of API variants."""
    getter = getattr(cell, f"get_{axis}", None)
    raw = getter() if callable(getter) else getattr(cell, axis, None)
    try:
        return None if raw is None else int(raw) + 1
    except (TypeError, ValueError):
        return None


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
