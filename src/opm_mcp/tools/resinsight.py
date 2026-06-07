"""ResInsight automation — launch the 3D viewer to visualise simulation cases.

ResInsight is a Qt/OpenGL application and lives in WSL. We drive it via its
``--commandFile`` interface (a small DSL — ``openCase(...)``, ``exportSnapshots
(...)``, etc.) plus a few CLI flags. Results (snapshots, plots) land in a
caller-specified folder which can sit on the Windows side (``C:\\...``) for easy
viewing.

OpenGL note: WSLg on Windows 11 normally provides a display, but ResInsight
needs a working OpenGL driver. If snapshots fail with ``MESA: error: ZINK``
or similar, set ``LIBGL_ALWAYS_SOFTWARE=1`` in the env or have the user install
``apt install xvfb mesa-utils libgl1-mesa-dri`` in the distro.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .. import wsl
from ..config import SETTINGS


def _writable_dir(path: str | None) -> Path:
    if path:
        p = Path(path) if not path.startswith("/") else Path(wsl.to_windows_path(path))
    else:
        p = SETTINGS.work_dir / "artifacts" / "resinsight"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_command_file(commands: list[str], dest_dir: Path) -> Path:
    cf = dest_dir / "commands.rip"
    cf.write_text("\n".join(commands) + "\n", encoding="utf-8")
    return cf


def export_snapshot(
    case_path: str,
    output_dir: str | None = None,
    view_size: tuple[int, int] = (1280, 800),
    force_software_gl: bool = False,
    extra_commands: list[str] | None = None,
) -> dict[str, Any]:
    """Open a case in ResInsight, render the default 3D view, save a PNG.

    Parameters
    ----------
    case_path : str
        Path to ``CASE.EGRID`` (or ``CASE.DATA`` — we will resolve to EGRID).
    output_dir : str | None
        Where the PNG(s) should land. Defaults to ``<workdir>/artifacts/resinsight``.
    view_size : (int, int)
        Image size in pixels.
    force_software_gl : bool
        Set ``LIBGL_ALWAYS_SOFTWARE=1`` for the child process. Use when GPU
        passthrough to WSL is unavailable.
    extra_commands : list[str] | None
        Additional ResInsight command-file lines appended before the snapshot
        is taken (e.g. ``setExportFolder(type="SNAPSHOTS", path="...")``).
    """
    case_local = Path(case_path) if not case_path.startswith("/") else Path(wsl.to_windows_path(case_path))
    if case_local.suffix.upper() == ".DATA":
        case_local = case_local.with_suffix(".EGRID")
    if not case_local.exists():
        return {"ok": False, "error": f"case file not found on Windows side: {case_local}"}

    out_dir = _writable_dir(output_dir)
    case_wsl = wsl.to_wsl_path(str(case_local))
    out_wsl = wsl.to_wsl_path(str(out_dir))

    commands = [
        f'openCase(fileNames=["{case_wsl}"])',
        f'setExportFolder(type="SNAPSHOTS", path="{out_wsl}")',
    ]
    if extra_commands:
        commands.extend(extra_commands)
    commands.append('exportSnapshots(type="VIEWS")')
    commands.append("closeProject()")
    cf = _write_command_file(commands, out_dir)
    cf_wsl = wsl.to_wsl_path(str(cf))

    argv = [
        SETTINGS.resinsight_bin,
        "--console",
        "--size",
        str(view_size[0]),
        str(view_size[1]),
        "--commandFile",
        cf_wsl,
    ]
    env = {"LIBGL_ALWAYS_SOFTWARE": "1"} if force_software_gl else {}

    res = wsl.run(argv, env=env, check=False, timeout=600)

    snapshots = sorted(p for p in out_dir.glob("*.png"))
    return {
        "ok": bool(snapshots) and res.returncode == 0,
        "returncode": res.returncode,
        "command_file": str(cf),
        "output_dir": str(out_dir),
        "snapshots": [str(p) for p in snapshots],
        "stdout_tail": res.stdout[-1500:],
        "stderr_tail": res.stderr[-1500:],
    }


def export_well_log(
    case_path: str,
    well: str,
    properties: list[str] | None = None,
    output_dir: str | None = None,
    force_software_gl: bool = False,
) -> dict[str, Any]:
    """Export an LAS-format well log for one well from a case.

    Uses ResInsight's ``createWellLogPlotFromGridProperty`` / ``exportWellLogPlot``
    commands. Default properties are PRESSURE, SOIL, SWAT, SGAS.
    """
    properties = properties or ["PRESSURE", "SOIL", "SWAT", "SGAS"]
    case_local = Path(case_path) if not case_path.startswith("/") else Path(wsl.to_windows_path(case_path))
    if case_local.suffix.upper() == ".DATA":
        case_local = case_local.with_suffix(".EGRID")
    out_dir = _writable_dir(output_dir)
    case_wsl = wsl.to_wsl_path(str(case_local))
    out_wsl = wsl.to_wsl_path(str(out_dir))

    commands = [
        f'openCase(fileNames=["{case_wsl}"])',
        f'setExportFolder(type="WELLLOGS", path="{out_wsl}")',
    ]
    for prop in properties:
        commands.append(
            'createWellLogPlotFromGridProperty('
            f'wellPathNames=["{well}"], propertyType="DYNAMIC_NATIVE", propertyName="{prop}")'
        )
    commands.append('exportWellLogPlot(folder="' + out_wsl + '")')
    commands.append("closeProject()")
    cf = _write_command_file(commands, out_dir)
    cf_wsl = wsl.to_wsl_path(str(cf))

    argv = [SETTINGS.resinsight_bin, "--console", "--commandFile", cf_wsl]
    env = {"LIBGL_ALWAYS_SOFTWARE": "1"} if force_software_gl else {}
    res = wsl.run(argv, env=env, check=False, timeout=600)
    artefacts = sorted(p for p in out_dir.glob("*.las"))
    return {
        "ok": bool(artefacts) and res.returncode == 0,
        "returncode": res.returncode,
        "well": well,
        "properties": properties,
        "output_dir": str(out_dir),
        "log_files": [str(p) for p in artefacts],
        "stdout_tail": res.stdout[-1500:],
        "stderr_tail": res.stderr[-1500:],
    }


def launch_resinsight_interactive(case_path: str | None = None) -> dict[str, Any]:
    """Launch ResInsight as a GUI window (relies on WSLg).

    This call returns immediately; the GUI stays open until the user closes it.
    Useful when the user wants to inspect a case visually rather than have
    Claude export artefacts.
    """
    argv = [SETTINGS.resinsight_bin]
    if case_path:
        case_local = Path(case_path) if not case_path.startswith("/") else Path(wsl.to_windows_path(case_path))
        if case_local.suffix.upper() == ".DATA":
            case_local = case_local.with_suffix(".EGRID")
        argv += ["--case", wsl.to_wsl_path(str(case_local))]
    proc = wsl.popen(argv)
    return {"pid": proc.pid, "argv": argv, "note": "GUI launched; not tracked as a job."}
