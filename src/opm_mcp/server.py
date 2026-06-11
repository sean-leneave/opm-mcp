"""OPM MCP server entry point.

Registers every callable in :mod:`opm_mcp.tools` as an MCP tool so Claude can
invoke them via the standard MCP transport. Run as::

    python -m opm_mcp.server

or, after ``pip install -e .``::

    opm-mcp
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import wsl
from .coerce import arg_list, key_list
from .config import SETTINGS
from .tools import analysis as analysis_tools
from .tools import deck as deck_tools
from .tools import decktext as decktext_tools
from .tools import discovery as discovery_tools
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
        "Linux binaries run inside a WSL2 distro; the server itself runs on Windows. "
        "Paths: tools accept Windows or WSL forms as input. In responses, "
        "fields suffixed _wsl (/mnt/c/...) are valid ONLY inside the WSL "
        "distro — the host shell (PowerShell/Git Bash) needs the _win forms. "
        "Use to_windows_path / to_wsl_path to convert."
    ),
)


def _safe(fn):
    """Uniform error contract: tools never surface raw tracebacks.

    Every tool failure becomes ``{"ok": false, "error", "error_type"}`` so the
    model gets something it can react to (retry, fix a path, pick another tool)
    instead of an opaque MCP error.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — this is the tool boundary
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    return wrapper


def safe_tool(**kwargs):
    """``@safe_tool()`` with the :func:`_safe` error contract applied."""

    def decorator(fn):
        return mcp.tool(**kwargs)(_safe(fn))

    return decorator


def _with_inline_image(result: dict[str, Any], max_bytes: int = 1_500_000) -> Any:
    """Attach the rendered PNG as inline MCP image content when small enough.

    Claude Desktop renders the chart directly in the chat instead of the user
    having to open the path. The dict payload still rides along as a second
    content item, and is returned alone on failure or for oversized images.
    """
    png_path = result.get("png_path") if isinstance(result, dict) else None
    if not (isinstance(result, dict) and result.get("ok") and png_path):
        return result
    try:
        data = Path(png_path).read_bytes()
    except OSError:
        return result
    if len(data) > max_bytes:
        result["inline_image"] = f"skipped ({len(data)} bytes > {max_bytes})"
        return result
    return [Image(data=data, format="png"), result]


# ---------------------------------------------------------------------------
# Environment / introspection
# ---------------------------------------------------------------------------


@safe_tool()
def opm_environment() -> dict[str, str]:
    """Report which OPM binaries and Python bindings the server can reach.

    Use this first when troubleshooting — it tells you whether ``wsl.exe`` is
    reachable, whether ``flow`` and ``ResInsight`` respond, and whether the
    ``python3-opm-common`` bindings import successfully inside the distro.
    """
    info = wsl.check_environment()
    info["work_dir"] = str(SETTINGS.work_dir)
    return info


@safe_tool()
def list_cases(directory: str, max_depth: int = 4, limit: int = 100) -> dict[str, Any]:
    """Scan a directory tree for simulation cases (.DATA decks and .SMSPEC results).

    The session opener: returns case names, deck paths, whether results exist,
    and modification times (newest first).
    """
    return discovery_tools.list_cases(directory, max_depth=max_depth, limit=limit)


@safe_tool()
def to_wsl_path(windows_path: str) -> dict[str, str]:
    """Translate a Windows path to its WSL equivalent (``C:\\x`` → ``/mnt/c/x``)."""
    return {"input": windows_path, "wsl_path": wsl.to_wsl_path(windows_path)}


@safe_tool()
def to_windows_path(wsl_path: str) -> dict[str, str]:
    """Translate a WSL path back to its Windows form."""
    return {"input": wsl_path, "windows_path": wsl.to_windows_path(wsl_path)}


# ---------------------------------------------------------------------------
# Deck inspection (WSL python3-opm-common)
# ---------------------------------------------------------------------------


@safe_tool()
def validate_deck(deck_path: str) -> dict[str, Any]:
    """Parse an ECLIPSE/Flow DATA deck and report keyword counts + parser issues."""
    return deck_tools.validate_deck(deck_path)


@safe_tool()
def summarize_deck(deck_path: str) -> dict[str, Any]:
    """High-level deck summary: phases, units, grid dimensions, wells, schedule length."""
    return deck_tools.summarize_deck(deck_path)


@safe_tool()
def list_wells(deck_path: str) -> dict[str, Any]:
    """List wells declared in the deck at the first schedule step."""
    return deck_tools.list_wells(deck_path)


@safe_tool()
def deck_text(
    deck_path: str,
    start_line: int = 1,
    n_lines: int = 200,
    file: str | None = None,
) -> dict[str, Any]:
    """Numbered text slice of a deck (or one of its include files) — no WSL needed.

    Decks are plain ASCII; use this instead of host-shell head/sed when a
    client misdetects .DATA as binary. ``file`` selects an include file by
    path or bare name (matched against the resolved INCLUDE tree).
    """
    return decktext_tools.deck_text(deck_path, start_line=start_line, n_lines=n_lines, file=file)


@safe_tool()
def deck_grep(
    deck_path: str,
    pattern: str,
    context: int = 2,
    search_includes: bool = True,
    max_matches: int = 40,
    ignore_case: bool = True,
) -> dict[str, Any]:
    """Regex search across a deck AND its resolved INCLUDE tree (PATHS-aware).

    The deck-QC workhorse: ``deck_grep(deck, "^WCONPROD|^LIFTOPT", context=8)``
    returns file, line number, and context for every keyword hit — including
    keywords that live in include files referenced via $ALIAS paths. Files
    larger than ~8 MB (bulk GRDECL arrays) are skipped and reported.
    """
    return decktext_tools.deck_grep(
        deck_path,
        pattern,
        context=context,
        search_includes=search_includes,
        max_matches=max_matches,
        ignore_case=ignore_case,
    )


# ---------------------------------------------------------------------------
# Flow simulation jobs (background)
# ---------------------------------------------------------------------------


@safe_tool()
def run_flow(
    deck_path: str,
    extra_args: list[str] | str | None = None,
    output_dir: str | None = None,
    threads: int | None = None,
    convergence_info: str | None = "steps,iterations",
    mpi_processes: int | None = None,
) -> dict[str, Any]:
    """Start an OPM Flow simulation in the background; returns a job id immediately.

    ``extra_args`` is a JSON array of flow flags, e.g.
    ``["--enable-tuning=true"]`` — a plain string is also accepted and split
    shell-style. At most OPM_MCP_MAX_PARALLEL jobs run at once — extra
    submissions are state="queued" and launch automatically as slots free. By
    default writes INFOSTEP/INFOITER diagnostics so ``flow_convergence_report``
    works (``convergence_info="none"`` to skip). ``mpi_processes=N`` runs
    ``mpirun -np N`` for distributed solves.
    """
    return flow_tools.run_flow(
        deck_path,
        extra_args=arg_list(extra_args),
        output_dir=output_dir,
        threads=threads,
        convergence_info=convergence_info,
        mpi_processes=mpi_processes,
    )


@safe_tool()
def flow_status(job_id: str) -> dict[str, Any]:
    """Check whether a Flow job is still running, with the latest progress line."""
    return flow_tools.flow_status(job_id)


@safe_tool()
def flow_log_tail(job_id: str, lines: int = 200, grep: str | None = None) -> dict[str, Any]:
    """Return the tail of a Flow job's combined stdout/stderr log.

    ``grep`` (case-insensitive regex) filters the WHOLE log first and returns
    the last ``lines`` matching lines with line numbers — e.g.
    ``grep="will be shut|Problem:|Error:"`` extracts well-shut and solver
    events from a long run.
    """
    return flow_tools.flow_log_tail(job_id, lines=lines, grep=grep)


@safe_tool()
def flow_cancel(job_id: str) -> dict[str, Any]:
    """Cancel a running Flow job."""
    return flow_tools.flow_cancel(job_id)


@safe_tool()
def flow_jobs() -> dict[str, Any]:
    """List recent Flow jobs known to this server."""
    return flow_tools.flow_jobs()


# ---------------------------------------------------------------------------
# Result-file readers (Windows-native via resdata)
# ---------------------------------------------------------------------------


@safe_tool()
def list_summary_vectors(case_path: str, pattern: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List summary vector keys in a case. Optional glob pattern (case-insensitive)."""
    return result_tools.list_summary_vectors(case_path, pattern=pattern, limit=limit)


@safe_tool()
def read_summary(
    case_path: str,
    keys: list[str] | str,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
) -> dict[str, Any]:
    """Read one or more summary vectors as time series, downsampled to ``max_points``.

    ``keys`` is a JSON array (``["FOPR", "WBHP:B-1H"]``); a comma/space
    separated string is also accepted.
    """
    return result_tools.read_summary(
        case_path, key_list(keys), start=start, end=end, max_points=max_points
    )


@safe_tool()
def field_summary(case_path: str) -> dict[str, Any]:
    """Field-level KPIs: cumulative production/injection, pressure, final rates."""
    return result_tools.field_summary(case_path)


@safe_tool()
def well_summary(case_path: str, well: str) -> dict[str, Any]:
    """Per-well KPIs: oil/gas/water rates, BHP, cumulative production."""
    return result_tools.well_summary(case_path, well)


@safe_tool()
def grid_info(case_path: str) -> dict[str, Any]:
    """Static grid info: dimensions, active count, porosity/permeability stats."""
    return result_tools.grid_info(case_path)


@safe_tool()
def restart_info(case_path: str) -> dict[str, Any]:
    """List report steps available in a UNRST file plus the keywords it contains."""
    return result_tools.restart_info(case_path)


@safe_tool()
def compare_summaries(case_a: str, case_b: str, keys: list[str] | str | None = None) -> dict[str, Any]:
    """Compare two cases on field totals (or supplied keys). Default = FOPT/FGPT/FWPT/FPR."""
    return result_tools.compare_summaries(case_a, case_b, keys=key_list(keys))


# ---------------------------------------------------------------------------
# Native plotting (matplotlib — no ResInsight/OpenGL)
# ---------------------------------------------------------------------------


@safe_tool()
def plot_summary(
    case_path: str,
    keys: list[str] | str,
    output_path: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    x_axis: str = "date",
    max_points: int = 1000,
    twin_axis: bool = True,
) -> Any:
    """Plot summary vectors to a PNG, rendered inline in the chat.

    Keys accept wildcards (``WWCT:*``, ``GOPR:*``) as a JSON array; a
    comma/space separated string also works. Twin y-axis groups by
    engineering unit; ``x_axis`` is ``"date"`` or ``"days"``. No
    ResInsight/OpenGL needed — the fast path for rate/pressure/cumulative plots.
    """
    return _with_inline_image(
        plot_tools.plot_summary(
            case_path,
            key_list(keys),
            output_path=output_path,
            title=title,
            start=start,
            end=end,
            x_axis=x_axis,
            max_points=max_points,
            twin_axis=twin_axis,
        )
    )


@safe_tool()
def plot_property_map(
    case_path: str,
    keyword: str = "SWAT",
    k_layer: int = 1,
    report_step: int | None = None,
    output_path: str | None = None,
    vmin: float | None = None,
    vmax: float | None = None,
) -> Any:
    """I–J areal map of a grid property for one K layer, rendered inline.

    Dynamic (PRESSURE/SWAT/SGAS/SOIL from UNRST at a report step, default last)
    or static (PORO/PERMX/NTG/PORV from INIT). Inactive cells blank; axes are
    1-based I/J. The no-GL alternative to a ResInsight view for sweep/depletion QC.
    """
    return _with_inline_image(
        plot_tools.plot_property_map(
            case_path,
            keyword=keyword,
            k_layer=k_layer,
            report_step=report_step,
            output_path=output_path,
            vmin=vmin,
            vmax=vmax,
        )
    )


# ---------------------------------------------------------------------------
# Reservoir-engineering analytics
# ---------------------------------------------------------------------------


@safe_tool()
def material_balance_check(case_path: str) -> dict[str, Any]:
    """Field MB QC: OOIP, recovery factor, exact oil mass-balance drift, surface VRR."""
    return analysis_tools.material_balance_check(case_path)


@safe_tool()
def flow_convergence_report(case_path: str, worst_n: int = 5) -> dict[str, Any]:
    """Solver performance from INFOSTEP/INFOITER: time by stage, wasted steps, worst steps."""
    return analysis_tools.flow_convergence_report(case_path, worst_n=worst_n)


@safe_tool()
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


@safe_tool()
def compare_to_observed(
    case_path: str,
    observed_csv: str,
    keys: list[str] | None = None,
    plot: bool = True,
    output_path: str | None = None,
) -> Any:
    """History-match QC: misfit (RMSE/bias/MAE/NRMSE) of simulated vs observed data.

    CSV: first column date (ISO), remaining headers are summary keys
    (WBHP:PROD, FOPR, ...); blank cells = no observation. Returns per-key
    stats and an inline sim-vs-obs overlay plot.
    """
    return _with_inline_image(
        analysis_tools.compare_to_observed(
            case_path, observed_csv, keys=keys, plot=plot, output_path=output_path
        )
    )


@safe_tool()
def rft_data(case_path: str, well: str | None = None, date: str | None = None) -> dict[str, Any]:
    """RFT surveys (pressure vs depth at well connections) from CASE.RFT.

    For perforation-pressure / gradient QC. Requires WRFTPLT in the deck's
    SCHEDULE section. Cell i/j/k are 1-based.
    """
    return result_tools.rft_data(case_path, well=well, date=date)


# ---------------------------------------------------------------------------
# Deck editing (sensitivities / history matching)
# ---------------------------------------------------------------------------


@safe_tool()
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


@safe_tool()
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


@safe_tool()
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


@safe_tool()
def resinsight_open(case_path: str | None = None) -> dict[str, Any]:
    """Open ResInsight as an interactive GUI (relies on WSLg). Returns immediately."""
    return ri_tools.launch_resinsight_interactive(case_path=case_path)


# ---------------------------------------------------------------------------
# OPM utility binaries
# ---------------------------------------------------------------------------


@safe_tool()
def convert_ecl(input_path: str, output_path: str | None = None, mode: str = "auto") -> dict[str, Any]:
    """Convert between formatted (F*) and unformatted ECL result files."""
    return util_tools.convert_ecl(input_path, output_path=output_path, mode=mode)


@safe_tool()
def compare_ecl(
    case_a: str,
    case_b: str,
    keywords: list[str] | str | None = None,
    abs_tol: float = 1e-3,
    rel_tol: float = 1e-3,
) -> dict[str, Any]:
    """Compare two ECL result files within tolerances using ``compareECL``."""
    return util_tools.compare_ecl(
        case_a, case_b, keywords=key_list(keywords), abs_tol=abs_tol, rel_tol=rel_tol
    )


@safe_tool()
def make_restart_deck(
    base_deck: str,
    restart_case: str,
    report_step: int,
    output_deck: str | None = None,
    mode: str = "inline",
    skiprest: bool = True,
) -> dict[str, Any]:
    """Generate a restart (history -> prediction) deck via OPM's ``rst_deck``.

    Restarts ``base_deck`` from ``restart_case:report_step`` (see
    ``restart_info`` for available steps). Default mode "inline" produces one
    self-contained deck. Validate and run_flow the result for predictions.
    """
    return util_tools.make_restart_deck(
        base_deck,
        restart_case,
        report_step,
        output_deck=output_deck,
        mode=mode,
        skiprest=skiprest,
    )


@safe_tool()
def pack_deck(deck_path: str, output_dir: str | None = None) -> dict[str, Any]:
    """Flatten a deck and all its INCLUDEs into a single file using ``opmpack``."""
    return util_tools.pack_deck(deck_path, output_dir=output_dir)


@safe_tool()
def hash_deck(deck_path: str) -> dict[str, Any]:
    """Compute a deterministic hash of a deck using ``opmhash``."""
    return util_tools.hash_deck(deck_path)


@safe_tool()
def summary_tool(case_path: str, keys: list[str] | str | None = None) -> dict[str, Any]:
    """Run the OPM ``summary`` CLI on a case (alternative summary reader)."""
    return util_tools.summary_tool(case_path, keys=key_list(keys))


@safe_tool()
def upscaling_status() -> dict[str, Any]:
    """Detect whether the optional ``opm-upscaling`` binaries are available."""
    return util_tools.upscaling_status()


@safe_tool()
def run_upscaling(tool: str, args: list[str] | str, cwd: str | None = None) -> dict[str, Any]:
    """Run an opm-upscaling binary (``upscale_perm``, ``upscale_relperm``, etc.) passthrough.

    ``args`` is a JSON array; a plain string is also accepted and split
    shell-style.
    """
    return util_tools.run_upscaling(tool, arg_list(args) or [], cwd=cwd)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
