"""Deck inspection tools — parse and summarise ECLIPSE/Flow input decks.

All parsing happens inside WSL via :mod:`opm_mcp.helpers.deck_inspect`, which
uses ``python3-opm-common``. We return whatever the helper emits as JSON so the
model sees structured data, not free-form text.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .. import wsl

_HELPER_NAME = "deck_inspect.py"


def _helper_path_wsl() -> str:
    """Return the WSL-visible path to the bundled helper script."""
    with resources.as_file(resources.files("opm_mcp.helpers").joinpath(_HELPER_NAME)) as p:
        return wsl.to_wsl_path(str(p))


def _run(action: str, deck_path: str) -> dict:
    helper = _helper_path_wsl()
    deck_wsl = wsl.to_wsl_path(deck_path)
    res = wsl.run(
        ["python3", helper, action, deck_wsl],
        check=False,
        timeout=120,
    )
    raw = res.stdout.strip().splitlines()
    last = raw[-1] if raw else ""
    try:
        payload = json.loads(last)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "helper produced non-JSON output",
            "returncode": res.returncode,
            "stdout": res.stdout[-2000:],
            "stderr": res.stderr[-2000:],
        }
    payload.setdefault("returncode", res.returncode)
    return payload


def validate_deck(deck_path: str) -> dict:
    """Parse the deck and report keyword counts + any parser issues."""
    if not deck_path:
        return {"ok": False, "error": "deck_path is required"}
    return _run("validate", deck_path)


def summarize_deck(deck_path: str) -> dict:
    """Return a high-level summary: phases, units, grid dims, wells, schedule length."""
    return _run("summarize", deck_path)


def list_wells(deck_path: str) -> dict:
    """List the wells declared at the first schedule step with type/status/group."""
    return _run("wells", deck_path)
