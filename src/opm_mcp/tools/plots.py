"""Native plotting — render summary vectors to PNG without ResInsight/OpenGL.

ResInsight is the heavyweight 3D path and is GL-fragile under WSL. But ~90% of
"show me" requests in a reservoir-engineering chat are 2D line plots
(rates, pressures, cumulatives, water cut). Those we can serve with matplotlib
directly on the Windows side via :mod:`resdata` data — no WSL hop, no GL.

The PNG is written to a caller-chosen folder (default under the work dir's
``artifacts``), and the path is returned so Claude can surface the image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import SETTINGS
from . import results as _results


def _artifact_dir(path: str | None) -> Path:
    if path:
        from .. import wsl

        p = Path(path) if not path.startswith("/") else Path(wsl.to_windows_path(path))
    else:
        p = SETTINGS.work_dir / "artifacts" / "plots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(keys: list[str]) -> str:
    stem = "_".join(k.replace(":", "-") for k in keys[:3])
    if len(keys) > 3:
        stem += f"_plus{len(keys) - 3}"
    return stem[:80] or "plot"


def plot_summary(
    case_path: str,
    keys: list[str],
    output_path: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    x_axis: str = "date",
    max_points: int = 1000,
    twin_axis: bool = True,
) -> dict[str, Any]:
    """Plot one or more summary vectors to a PNG and return its path.

    Parameters
    ----------
    case_path : str
        Path to the case (any of CASE / .SMSPEC / .UNSMRY / .DATA).
    keys : list[str]
        Summary keys to plot, e.g. ``["FOPR", "FPR", "WBHP:PROD"]``.
    output_path : str | None
        Destination PNG, or a folder. Defaults to ``<workdir>/artifacts/plots``.
    title : str | None
        Plot title. Defaults to the case name + key list.
    start, end : str | None
        ISO date bounds (``YYYY-MM-DD``).
    x_axis : str
        ``"date"`` (calendar) or ``"days"`` (elapsed simulation days).
    max_points : int
        Downsample cap per series.
    twin_axis : bool
        When the keys span exactly two engineering units, put the second unit
        group on a secondary y-axis (e.g. rate vs pressure). With one unit all
        series share an axis; with 3+ units everything shares the primary axis.

    Returns
    -------
    dict
        ``ok``, ``png_path`` (Windows path), ``keys_plotted``, ``units``.
    """
    if not keys:
        return {"ok": False, "error": "at least one key is required"}

    data = _results.read_summary(case_path, keys, start=start, end=end, max_points=max_points)
    if not data.get("ok"):
        return data

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime

    vectors: dict[str, list[float]] = data["vectors"]
    units: dict[str, str | None] = data.get("units", {})
    plotted = list(vectors.keys())
    if not plotted:
        return {"ok": False, "error": "no plottable vectors", "missing": data.get("missing")}

    if x_axis == "days" and data.get("days") is not None:
        x = data["days"]
        x_label = "Elapsed time (days)"
        x_is_date = False
    else:
        x = [datetime.fromisoformat(t) for t in data["time"]]
        x_label = "Date"
        x_is_date = True

    # Group keys by unit so we can decide on a twin axis.
    unit_groups: dict[str, list[str]] = {}
    for k in plotted:
        unit_groups.setdefault(units.get(k) or "", []).append(k)

    fig, ax1 = plt.subplots(figsize=(10, 6))
    case_name = Path(data["case_path"]).name
    fig.suptitle(title or f"{case_name}: {', '.join(plotted)}", fontsize=12)

    cmap = plt.get_cmap("tab10")
    color_i = 0
    use_twin = twin_axis and len(unit_groups) == 2
    primary_unit = next(iter(unit_groups))

    ax2 = ax1.twinx() if use_twin else None
    ax1_vals: list[float] = []
    ax2_vals: list[float] = []
    for unit, ks in unit_groups.items():
        target = ax1 if (not use_twin or unit == primary_unit) else ax2
        bucket = ax1_vals if target is ax1 else ax2_vals
        for k in ks:
            target.plot(x, vectors[k], label=f"{k} [{unit}]" if unit else k,
                        color=cmap(color_i % 10), marker="o", ms=3, lw=1.4)
            bucket.extend(v for v in vectors[k] if v is not None)
            color_i += 1

    ax1.set_xlabel(x_label)
    ax1.set_ylabel(_axis_label(primary_unit, unit_groups[primary_unit]))
    ax1.grid(True, alpha=0.3)
    _tidy_yaxis(ax1, ax1_vals)
    if use_twin and ax2 is not None:
        second_unit = [u for u in unit_groups if u != primary_unit][0]
        ax2.set_ylabel(_axis_label(second_unit, unit_groups[second_unit]))
        _tidy_yaxis(ax2, ax2_vals)

    if x_is_date:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        fig.autofmt_xdate()

    # Combined legend across both axes.
    handles, labels = ax1.get_legend_handles_labels()
    if ax2 is not None:
        h2, l2 = ax2.get_legend_handles_labels()
        handles += h2
        labels += l2
    ax1.legend(handles, labels, loc="best", fontsize=9)

    if output_path and str(output_path).lower().endswith(".png"):
        from .. import wsl

        raw = output_path if not output_path.startswith("/") else wsl.to_windows_path(output_path)
        png = Path(raw)
        png.parent.mkdir(parents=True, exist_ok=True)
    else:
        png = _artifact_dir(output_path) / f"{case_name}_{_safe_name(plotted)}.png"
    fig.tight_layout()
    fig.savefig(png, dpi=130, bbox_inches="tight")
    plt.close(fig)

    return {
        "ok": True,
        "png_path": str(png),
        "keys_plotted": plotted,
        "units": {k: units.get(k) for k in plotted},
        "n_points": data.get("n_points"),
        "x_axis": "days" if not x_is_date else "date",
        "missing": data.get("missing"),
    }


def _axis_label(unit: str, keys: list[str]) -> str:
    base = ", ".join(keys[:3]) + ("..." if len(keys) > 3 else "")
    return f"{base} [{unit}]" if unit else base


def _tidy_yaxis(ax, values: list[float]) -> None:
    """Make rate-controlled (near-constant) series read as flat, not as noise.

    Suppresses matplotlib's scientific offset (the misleading ``+5e2`` header)
    and, when a series barely varies, pads the limits so floating-point jitter
    isn't amplified to fill the axis.
    """
    try:
        ax.ticklabel_format(useOffset=False, style="plain", axis="y")
    except Exception:
        pass
    if not values:
        return
    vmin, vmax = min(values), max(values)
    span = vmax - vmin
    mag = max(abs(vmin), abs(vmax), 1e-12)
    if span < 0.01 * mag:  # effectively constant
        pad = max(0.05 * mag, 1e-9)
        ax.set_ylim(vmin - pad, vmax + pad)
