"""Workspace discovery — find simulation cases on disk.

The natural opening move of a session: "what cases do I have under
``C:\\models``?" Scans a directory tree for ECLIPSE decks (``.DATA``) and
result sets (``.SMSPEC``) and pairs them up, so Claude can offer concrete next
steps (validate, run, read results) instead of asking for paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import wsl


def list_cases(directory: str, max_depth: int = 4, limit: int = 100) -> dict[str, Any]:
    """Scan a directory tree for simulation cases (decks and/or results).

    Parameters
    ----------
    directory : str
        Root to scan (Windows or WSL path).
    max_depth : int
        How many levels below the root to descend (keeps Norne-sized trees fast).
    limit : int
        Maximum cases returned (most recently modified first).
    """
    root = Path(directory) if not directory.startswith("/") else Path(wsl.to_windows_path(directory))
    root = root.expanduser()
    if not root.is_dir():
        return {"ok": False, "error": f"not a directory: {root}"}

    cases: dict[str, dict[str, Any]] = {}
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            upper = name.upper()
            if not (upper.endswith(".DATA") or upper.endswith(".SMSPEC")):
                continue
            stem = Path(dirpath) / Path(name).stem
            key = str(stem).upper()
            entry = cases.setdefault(
                key,
                {"case": Path(name).stem, "dir": dirpath, "deck": None, "has_results": False, "mtime": 0.0},
            )
            full = Path(dirpath) / name
            try:
                mtime = full.stat().st_mtime
            except OSError:
                mtime = 0.0
            entry["mtime"] = max(entry["mtime"], mtime)
            if upper.endswith(".DATA"):
                entry["deck"] = str(full)
            else:
                entry["has_results"] = True
                entry["case_prefix"] = str(stem)

    items = sorted(cases.values(), key=lambda e: e["mtime"], reverse=True)
    truncated = len(items) > limit
    items = items[:limit]
    for e in items:
        e["modified"] = datetime.fromtimestamp(e.pop("mtime"), tz=timezone.utc).isoformat(timespec="seconds")
    return {
        "ok": True,
        "root": str(root),
        "n_cases": len(items),
        "truncated": truncated,
        "cases": items,
    }
