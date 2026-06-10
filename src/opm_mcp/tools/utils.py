"""OPM utility binaries — convertECL, compareECL, opmpack, opmhash, summary.

These are quick wrappers around the binaries shipped by ``libopm-common-bin``
and ``libopm-grid-bin``. They run in WSL and return captured output for Claude
to interpret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import wsl


def convert_ecl(input_path: str, output_path: str | None = None, mode: str = "auto") -> dict[str, Any]:
    """Toggle between formatted and unformatted ECL output files.

    Parameters
    ----------
    input_path : str
        Input file (e.g. ``CASE.UNRST`` or ``CASE.FUNRST``).
    output_path : str | None
        Destination. Defaults to swapping extension F<->U.
    mode : str
        ``auto`` (detect from extension), ``to-formatted``, or ``to-binary``.
    """
    src_wsl = wsl.to_wsl_path(input_path)
    argv = ["convertECL"]
    src_lower = input_path.lower()
    if mode == "to-formatted" or (mode == "auto" and not src_lower.endswith(("formatted", "f")) and ".f" not in src_lower[-7:]):
        # convertECL chooses direction automatically; we just pass src + dst.
        pass
    if output_path:
        dst_wsl = wsl.to_wsl_path(output_path)
        argv += [src_wsl, dst_wsl]
    else:
        argv += [src_wsl]
    res = wsl.run(argv, check=False, timeout=120)
    return {
        "ok": res.returncode == 0,
        "returncode": res.returncode,
        "argv": argv,
        "stdout": res.stdout[-2000:],
        "stderr": res.stderr[-2000:],
    }


def compare_ecl(
    case_a: str,
    case_b: str,
    keywords: list[str] | None = None,
    abs_tol: float = 1e-3,
    rel_tol: float = 1e-3,
) -> dict[str, Any]:
    """Diff two ECL output files (UNRST or UNSMRY) using ``compareECL``.

    The OPM ``compareECL`` binary compares two binary result files within
    user-supplied tolerances and exits non-zero if differences exceed them.
    """
    argv = ["compareECL"]
    if keywords:
        argv += ["-k", ",".join(keywords)]
    argv += [wsl.to_wsl_path(case_a), wsl.to_wsl_path(case_b), str(abs_tol), str(rel_tol)]
    res = wsl.run(argv, check=False, timeout=120)
    return {
        "matches_within_tolerance": res.returncode == 0,
        "returncode": res.returncode,
        "argv": argv,
        "stdout": res.stdout[-4000:],
        "stderr": res.stderr[-2000:],
    }


def pack_deck(deck_path: str, output_dir: str | None = None) -> dict[str, Any]:
    """Run ``opmpack`` to flatten a deck + all its INCLUDEs into one file.

    The output is a single self-contained DATA file written next to the source
    (or into ``output_dir`` if supplied).
    """
    deck_wsl = wsl.to_wsl_path(deck_path)
    argv = ["opmpack", deck_wsl]
    if output_dir:
        argv += ["-o", wsl.to_wsl_path(output_dir)]
    res = wsl.run(argv, check=False, timeout=120)
    return {
        "ok": res.returncode == 0,
        "returncode": res.returncode,
        "argv": argv,
        "stdout": res.stdout[-2000:],
        "stderr": res.stderr[-2000:],
    }


def hash_deck(deck_path: str) -> dict[str, Any]:
    """Run ``opmhash`` to produce a deterministic hash of a deck's contents."""
    deck_wsl = wsl.to_wsl_path(deck_path)
    res = wsl.run(["opmhash", deck_wsl], check=False, timeout=60)
    return {
        "ok": res.returncode == 0,
        "hash_line": res.stdout.strip(),
        "stderr": res.stderr[-1000:],
    }


def summary_tool(case_path: str, keys: list[str] | None = None) -> dict[str, Any]:
    """Run the ``summary`` CLI from libopm-common-bin — alternative to read_summary.

    Useful as a cross-check against the :mod:`resdata` reader, or when summary
    files use a format ``resdata`` cannot open. Returns the raw textual table.
    """
    argv = ["summary", wsl.to_wsl_path(case_path)]
    if keys:
        argv += list(keys)
    res = wsl.run(argv, check=False, timeout=120)
    return {
        "ok": res.returncode == 0,
        "stdout": res.stdout[-5000:],
        "stderr": res.stderr[-1000:],
    }


def make_restart_deck(
    base_deck: str,
    restart_case: str,
    report_step: int,
    output_deck: str | None = None,
    mode: str = "inline",
    skiprest: bool = True,
) -> dict[str, Any]:
    """Generate a restart deck from a history run using OPM's ``rst_deck``.

    The classic history→prediction workflow: run history, then restart from
    report step N with changed controls. ``rst_deck`` clears the SOLUTION
    section, inserts ``RESTART``, and either strips pre-restart SCHEDULE
    keywords or (``skiprest=True``, default) inserts ``SKIPREST``.

    Parameters
    ----------
    base_deck : str
        The original ``.DATA`` deck of the history run.
    restart_case : str
        Case prefix of the run holding the restart file (e.g. the
        ``case_prefix_win`` from ``flow_status``); ``:report_step`` is appended.
    report_step : int
        SEQNUM report step to restart from (see ``restart_info``).
    output_deck : str | None
        Destination ``.DATA``. Defaults to ``<workdir>/decks/<CASE>_RST<N>.DATA``.
    mode : str
        ``inline`` (default — one self-contained file, safest across the
        Windows/WSL path boundary), ``share`` or ``copy``.
    """
    from ..config import SETTINGS

    base_wsl = wsl.to_wsl_path(base_deck)
    case_wsl = wsl.to_wsl_path(restart_case)
    # rst_deck wants the restart source as a case prefix, not a file path.
    for ext in (".DATA", ".UNRST", ".SMSPEC"):
        if case_wsl.upper().endswith(ext):
            case_wsl = case_wsl[: -len(ext)]
            break
    if output_deck:
        out_win = Path(output_deck) if not output_deck.startswith("/") else Path(wsl.to_windows_path(output_deck))
    else:
        case_name = Path(case_wsl).stem
        out_win = SETTINGS.work_dir / "decks" / f"{case_name}_RST{report_step}.DATA"
    out_win.parent.mkdir(parents=True, exist_ok=True)
    out_wsl = wsl.to_wsl_path(str(out_win))

    argv = ["rst_deck", base_wsl, f"{case_wsl}:{int(report_step)}", out_wsl, "-m", mode]
    if skiprest:
        argv.append("-s")
    res = wsl.run(argv, check=False, timeout=300)
    produced = out_win.exists()
    unifin_added = False
    if produced:
        unifin_added = _ensure_unifin(out_win, case_wsl)
    return {
        "ok": res.returncode == 0 and produced,
        "returncode": res.returncode,
        "output_deck": str(out_win) if produced else None,
        "output_deck_wsl": out_wsl if produced else None,
        "argv": argv,
        "stdout_tail": res.stdout[-1500:],
        "stderr_tail": res.stderr[-1500:],
        "unifin_added": unifin_added,
        "note": "Run validate_deck on the result, then run_flow it for the prediction.",
    }


def _ensure_unifin(deck_win: Path, restart_case_wsl: str) -> bool:
    """Insert UNIFIN when the restart source is a unified UNRST.

    ``rst_deck`` copies RUNSPEC verbatim, so a base deck with only UNIFOUT
    yields a restart deck that expects separate ``.X000N`` files while the
    history run actually wrote a unified ``.UNRST`` — flow then aborts with
    "restart file ... .X000N does not exist". Detect that mismatch and patch.
    """
    import re

    unrst_win = Path(wsl.to_windows_path(restart_case_wsl + ".UNRST"))
    if not unrst_win.exists():
        return False
    try:
        text = deck_win.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if re.search(r"(?im)^[ \t]*UNIFIN[ \t]*$", text):
        return False
    m = re.search(r"(?im)^[ \t]*RUNSPEC[ \t]*$", text)
    if not m:
        return False
    insert_at = text.find("\n", m.end())
    insert_at = insert_at + 1 if insert_at != -1 else len(text)
    text = text[:insert_at] + "\nUNIFIN\n" + text[insert_at:]
    deck_win.write_text(text, encoding="utf-8")
    return True


def upscaling_status() -> dict[str, Any]:
    """Detect whether OPM upscaling tools are installed in WSL.

    The ``opm-upscaling`` Debian package (binaries ``upscale_perm``,
    ``upscale_relperm``, ``cpchop``, ``cpregularize``, ``steadystate_upscale``)
    is *not* pulled in by ``flow``, so users typically need to install it
    separately. This tool tells Claude whether the binaries are available and
    where to find them.
    """
    candidates = [
        "upscale_perm",
        "upscale_relperm",
        "upscale_steadystate_implicit",
        "upscale_steadystate_explicit",
        "cpchop",
        "cpregularize",
    ]
    found = {}
    for c in candidates:
        res = wsl.run(["bash", "-lc", f"command -v {c} || true"], check=False, timeout=10)
        path = res.stdout.strip()
        if path:
            found[c] = path
    return {
        "installed": bool(found),
        "binaries": found,
        "missing": [c for c in candidates if c not in found],
        "install_hint": "In WSL: sudo apt-get update && sudo apt-get install opm-upscaling",
    }


def run_upscaling(tool: str, args: list[str], cwd: str | None = None) -> dict[str, Any]:
    """Generic passthrough to an OPM upscaling binary.

    Refuses to run if the binary isn't on PATH inside WSL.
    """
    status = upscaling_status()
    if tool not in status["binaries"]:
        return {
            "ok": False,
            "error": f"upscaling binary '{tool}' is not installed in WSL.",
            "hint": status["install_hint"],
        }
    argv = [tool] + [wsl.to_wsl_path(a) if Path(a).drive or a.startswith(("/mnt/", "\\")) else a for a in args]
    res = wsl.run(argv, cwd=cwd, check=False, timeout=3600)
    return {
        "ok": res.returncode == 0,
        "argv": argv,
        "stdout": res.stdout[-8000:],
        "stderr": res.stderr[-4000:],
    }
