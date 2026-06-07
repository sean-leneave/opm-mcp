"""OPM MCP server entry point.

Registers every callable in :mod:`opm_mcp.tools` as an MCP tool so Claude can
invoke them via the standard MCP transport. Run as::

    python -m opm_mcp.server

or, after ``pip install -e .``::

    opm-mcp
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import wsl
from .config import SETTINGS
from .tools import analysis as analysis_tools
from .tools import deck as deck_tools
from .tools import edit as edit_tools
from .tools import flow as flow_tools
from .tools import plots as plot_tools
from .tools import resinsight as ri_tools
from .tools import results as result_tools
from .tools import utils as util_tools

mcp = FastMCP(
    name="opm",
    instructions=(
        "Reservoir engineering toolbox built on Open Porous Media (OPM). "
        "Tools cover ECLIPSE-format deck validation, running OPM Flow simulations, "
        "reading UNSMRY/UNRST/EGRID/INIT result files, driving ResInsight for "
        "snapshots and well logs, and OPM utility binaries (convertECL, "
        "compareECL, opmpack, opmhash, summary, optional opm-upscaling). "
        "Also: native matplotlib plots (no GL), material-balance and solver-"
        "convergence diagnostics, per-layer restart-property stats, and a "
        "deck editor for sensitivities. For PVT/SCAL/relperm calculations, "
        "defer to the pyrestoolbox MCP if present. "
        "Linux binaries run inside a WSL2 distro; the server itself runs on Windows."
    ),
)


# ---------------------------------------------------------------------------
# Environment / introspection
# ---------------------------------------------------------------------------


@mcp.tool()
def opm_environment() -> dict[str, str]:
    """Report which OPM binaries and Python bindings the server can reach.

    Use this first when troubleshooting — it tells you whether ``wsl.exe`` is
    reachable, whether ``flow`` and ``ResInsight`` respond, and whether the
    ``python3-opm-common`` bindings import successfully inside the distro.
    """
    info = wsl.check_environment()
    info["work_dir"] = str(SETTINGS.work_dir)
    return info


@mcp.tool()
def to_wsl_path(windows_path: str) -> dict[str, str]:
    """Translate a Windows path to its WSL equivalent (``C:\\x`` → ``/mnt/c/x``)."""
    return {"input": windows_path, "wsl_path": wsl.to_wsl_path(windows_path)}


@mcp.tool()
def to_windows_path(wsl_path: str) -> dict[str, str]:
    """Translate a WSL path back to its Windows form."""
    return {"input": wsl_path, "windows_path": wsl.to_windows_path(wsl_path)}


# ---------------------------------------------------------------------------
# Deck inspection (WSL python3-opm-common)
# ---------------------------------------------------------------------------


@mcp.tool()
def validate_deck(deck_path: str) -> dict[str, Any]:
    """Parse an ECLIPSE/Flow DATA deck and report keyword counts + parser issues."""
    return deck_tools.validate_deck(deck_path)


@mcp.tool()
def summarize_deck(deck_path: str) -> dict[str, Any]:
    """High-level deck summary: phases, units, grid dimensions, wells, schedule length."""
    return deck_tools.summarize_deck(deck_path)


@mcp.tool()
def list_wells(deck_path: str) -> dict[str, Any]:
    """List wells declared in the deck at the first schedule step."""
    return deck_tools.list_wells(deck_path)


# ---------------------------------------------------------------------------
# Flow simulation jobs (background)
# ---------------------------------------------------------------------------


@mcp.tool()
def run_flow(
    deck_path: str,
    extra_args: list[str] | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
) -> dict[str, Any]:
    """Start an OPM Flow simulation in the background. Returns a job id immediately.

    By default writes INFOSTEP/INFOITER diagnostics so ``flow_convergence_report``
    works; pass ``convergence_info="none"`` to skip them.
    """
    return flow_tools.run_flow(
        deck_path,
        extra_args=extra_args,
        output_dir=output_dir,
        threads=threads,
        convergence_info=convergence_info,
    )


@mcp.tool()
def flow_status(job_id: str) -> dict[str, Any]:
    """Check whether a Flow job is still running, with the latest progress line."""
    return flow_tools.flow_status(job_id)


@mcp.tool()
def flow_log_tail(job_id: str, lines: int = 200) -> dict[str, Any]:
    """Return the tail of a Flow job's combined stdout/stderr log."""
    return flow_tools.flow_log_tail(job_id, lines=lines)


@mcp.tool()
def flow_cancel(job_id: str) -> dict[str, Any]:
    """Cancel a running Flow job."""
    return flow_tools.flow_cancel(job_id)


@mcp.tool()
def flow_jobs() -> dict[str, Any]:
    """List recent Flow jobs known to this server."""
    return flow_tools.flow_jobs()


# ---------------------------------------------------------------------------
# Result-file readers (Windows-native via resdata)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_summary_vectors(case_path: str, pattern: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List summary vector keys in a case. Optional glob pattern (case-insensitive)."""
    return result_tools.list_summary_vectors(case_path, pattern=pattern, limit=limit)


@mcp.tool()
def read_summary(
    case_path: str,
    keys: list[str],
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
) -> dict[str, Any]:
    """Read one or more summary vectors as time series, downsampled to ``max_points``."""
    return result_tools.read_summary(case_path, keys, start=start, end=end, max_points=max_points)


@mcp.tool()
def field_summary(case_path: str) -> dict[str, Any]:
    """Field-level KPIs: cumulative production/injection, pressure, final rates."""
    return result_tools.field_summary(case_path)


@mcp.tool()
def well_summary(case_path: str, well: str) -> dict[str, Any]:
    """Per-well KPIs: oil/gas/water rates, BHP, cumulative production."""
    return result_tools.well_summary(case_path, well)


@mcp.tool()
def grid_info(case_path: str) -> dict[str, Any]:
    """Static grid info: dimensions, active count, porosity/permeability stats."""
    return result_tools.grid_info(case_path)


@mcp.tool()
def restart_info(case_path: str) -> dict[str, Any]:
    """List report steps available in a UNRST file plus the keywords it contains."""
    return result_tools.restart_info(case_path)


@mcp.tool()
def compare_summaries(case_a: str, case_b: str, keys: list[str] | None = None) -> dict[str, Any]:
    """Compare two cases on field totals (or supplied keys). Default = FOPT/FGPT/FWPT/FPR."""
    return result_tools.compare_summaries(case_a, case_b, keys=keys)


# ---------------------------------------------------------------------------
# Native plotting (matplotlib — no ResInsight/OpenGL)
# ---------------------------------------------------------------------------


@mcp.tool()
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
    """Plot summary vectors to a PNG (twin-axis by unit). Returns the image path.

    No ResInsight/OpenGL needed — the fast path for rate/pressure/cumulative
    line plots. ``x_axis`` is ``"date"`` or ``"days"``.
    """
    return plot_tools.plot_summary(
        case_path,
        keys,
        output_path=output_path,
        title=title,
        start=start,
        end=end,
        x_axis=x_axis,
        max_points=max_points,
        twin_axis=twin_axis,
    )


# ---------------------------------------------------------------------------
# Reservoir-engineering analytics
# ---------------------------------------------------------------------------


@mcp.tool()
def material_balance_check(case_path: str) -> dict[str, Any]:
    """Field MB QC: OOIP, recovery factor, exact oil mass-balance drift, surface VRR."""
    return analysis_tools.material_balance_check(case_path)


@mcp.tool()
def flow_convergence_report(case_path: str, worst_n: int = 5) -> dict[str, Any]:
    """Solver performance from INFOSTEP/INFOITER: time by stage, wasted steps, worst steps."""
    return analysis_tools.flow_convergence_report(case_path, worst_n=worst_n)


@mcp.tool()
def restart_property(
    case_path: str,
    keyword: str = "PRESSURE",
    report_step: int | None = None,
    n_histogram_bins: int = 10,
) -> dict[str, Any]:
    """Per-layer stats for a dynamic property (PRESSURE/SWAT/SGAS/SOIL) at a report step."""
    return analysis_tools.restart_property(
        case_path, keyword=keyword, report_step=report_step, n_histogram_bins=n_histogram_bins
    )


# ---------------------------------------------------------------------------
# Deck editing (sensitivities / history matching)
# ---------------------------------------------------------------------------


@mcp.tool()
def clone_deck(
    deck_path: str,
    output_path: str | None = None,
    overrides: list[dict] | None = None,
    flatten_includes: bool = False,
    validate: bool = True,
) -> dict[str, Any]:
    """Clone a deck with safe text overrides (append_before_end / replace_regex / set_title / insert_after_keyword).

    Writes a NEW deck, reports per-op substitution counts, and re-validates.
    Idiomatic rate/BHP change: append a new WCONPROD/WCONINJE via
    ``append_before_end`` (last control in SCHEDULE wins).
    """
    return edit_tools.clone_deck(
        deck_path,
        output_path=output_path,
        overrides=overrides,
        flatten_includes=flatten_includes,
        validate=validate,
    )


# ---------------------------------------------------------------------------
# ResInsight automation
# ---------------------------------------------------------------------------


@mcp.tool()
def resinsight_snapshot(
    case_path: str,
    output_dir: str | None = None,
    view_size: tuple[int, int] = (1280, 800),
    force_software_gl: bool = False,
    extra_commands: list[str] | None = None,
) -> dict[str, Any]:
    """Render the default 3D view of a case in ResInsight and save a PNG snapshot."""
    return ri_tools.export_snapshot(
        case_path,
        output_dir=output_dir,
        view_size=view_size,
        force_software_gl=force_software_gl,
        extra_commands=extra_commands,
    )


@mcp.tool()
def resinsight_well_log(
    case_path: str,
    well: str,
    properties: list[str] | None = None,
    output_dir: str | None = None,
    force_software_gl: bool = False,
) -> dict[str, Any]:
    """Export an LAS well log for one well using ResInsight."""
    return ri_tools.export_well_log(
        case_path,
        well,
        properties=properties,
        output_dir=output_dir,
        force_software_gl=force_software_gl,
    )


@mcp.tool()
def resinsight_open(case_path: str | None = None) -> dict[str, Any]:
    """Open ResInsight as an interactive GUI (relies on WSLg). Returns immediately."""
    return ri_tools.launch_resinsight_interactive(case_path=case_path)


# ---------------------------------------------------------------------------
# OPM utility binaries
# ---------------------------------------------------------------------------


@mcp.tool()
def convert_ecl(input_path: str, output_path: str | None = None, mode: str = "auto") -> dict[str, Any]:
    """Convert between formatted (F*) and unformatted ECL result files."""
    return util_tools.convert_ecl(input_path, output_path=output_path, mode=mode)


@mcp.tool()
def compare_ecl(
    case_a: str,
    case_b: str,
    keywords: list[str] | None = None,
    abs_tol: float = 1e-3,
    rel_tol: float = 1e-3,
) -> dict[str, Any]:
    """Compare two ECL result files within tolerances using ``compareECL``."""
    return util_tools.compare_ecl(case_a, case_b, keywords=keywords, abs_tol=abs_tol, rel_tol=rel_tol)


@mcp.tool()
def pack_deck(deck_path: str, output_dir: str | None = None) -> dict[str, Any]:
    """Flatten a deck and all its INCLUDEs into a single file using ``opmpack``."""
    return util_tools.pack_deck(deck_path, output_dir=output_dir)


@mcp.tool()
def hash_deck(deck_path: str) -> dict[str, Any]:
    """Compute a deterministic hash of a deck using ``opmhash``."""
    return util_tools.hash_deck(deck_path)


@mcp.tool()
def summary_tool(case_path: str, keys: list[str] | None = None) -> dict[str, Any]:
    """Run the OPM ``summary`` CLI on a case (alternative summary reader)."""
    return util_tools.summary_tool(case_path, keys=keys)


@mcp.tool()
def upscaling_status() -> dict[str, Any]:
    """Detect whether the optional ``opm-upscaling`` binaries are available."""
    return util_tools.upscaling_status()


@mcp.tool()
def run_upscaling(tool: str, args: list[str], cwd: str | None = None) -> dict[str, Any]:
    """Run an opm-upscaling binary (``upscale_perm``, ``upscale_relperm``, etc.) passthrough."""
    return util_tools.run_upscaling(tool, args, cwd=cwd)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
