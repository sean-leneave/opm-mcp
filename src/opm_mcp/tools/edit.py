"""Deck editing — clone a deck with safe, verifiable overrides for sensitivities.

This is what turns the server from a *viewer* into a *copilot*: Claude can take
a base deck, produce a variant (lower rate, later end date, different BHP limit,
extra report steps), run it, and compare — i.e. drive sensitivity studies and
history-matching loops.

Editing is **text-based and conservative** on purpose. Rather than try to
round-trip arbitrary keyword blocks (fragile across ECLIPSE keyword grammars),
it offers a few well-defined operations and always:

* writes to a **new** file — the original deck is never mutated;
* reports how many substitutions each op made, so a no-op (pattern miss) is
  visible rather than silent;
* optionally re-validates the result via the WSL parser.

Operations (each a dict in ``overrides``)::

    {"op": "append_before_end", "text": "WCONPROD\\n 'PROD' ... /\\n/"}
    {"op": "insert_after_keyword", "keyword": "SCHEDULE", "text": "..."}
    {"op": "replace_regex", "pattern": "ORAT 500", "replacement": "ORAT 300", "count": 1}
    {"op": "set_title", "text": "Sensitivity: PROD ORAT 300"}

For well-control changes the idiomatic move is ``append_before_end`` with a new
``WCONPROD``/``WCONINJE`` — in SCHEDULE the latest control wins.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import wsl
from ..config import SETTINGS
from . import deck as _deck


def _to_local(path: str) -> Path:
    if path.startswith("/"):
        path = wsl.to_windows_path(path)
    return Path(path).expanduser()


def clone_deck(
    deck_path: str,
    output_path: str | None = None,
    overrides: list[dict] | None = None,
    flatten_includes: bool = False,
    validate: bool = True,
) -> dict[str, Any]:
    """Clone a deck, apply text overrides, and (optionally) validate the result.

    Parameters
    ----------
    deck_path : str
        Source ``.DATA`` deck (Windows or WSL path).
    output_path : str | None
        Destination ``.DATA``. Defaults to ``<src>_<n>.DATA`` next to the source,
        or under the work dir if the source folder is read-only.
    overrides : list[dict] | None
        Ordered list of edit operations (see module docstring).
    flatten_includes : bool
        If True, run ``opmpack`` first so INCLUDE files are inlined before
        editing — needed when the keyword you target lives in an include.
    validate : bool
        Re-parse the cloned deck via the WSL parser and return the result.

    Returns
    -------
    dict
        ``ok``, ``output_path``, per-op ``applied`` counts, and (if requested)
        the ``validation`` payload from :func:`opm_mcp.tools.deck.validate_deck`.
    """
    src = _to_local(deck_path)
    if not src.exists():
        return {"ok": False, "error": f"deck not found: {src}"}

    # Source text — flattened or raw.
    if flatten_includes:
        text, err = _flatten(deck_path)
        if err:
            return {"ok": False, "error": f"opmpack flatten failed: {err}"}
    else:
        text = src.read_text(encoding="utf-8", errors="replace")

    # Destination path.
    if output_path:
        dst = _to_local(output_path)
    else:
        dst = _unique_sibling(src)
    dst.parent.mkdir(parents=True, exist_ok=True)

    applied: list[dict] = []
    for i, op in enumerate(overrides or []):
        text, info = _apply_op(text, op)
        info["index"] = i
        applied.append(info)
        if not info.get("ok", True):
            return {"ok": False, "error": info.get("error"), "applied": applied}

    dst.write_text(text, encoding="utf-8")

    result: dict[str, Any] = {
        "ok": True,
        "source": str(src),
        "output_path": str(dst),
        "output_path_wsl": wsl.to_wsl_path(str(dst)),
        "applied": applied,
        "n_operations": len(applied),
    }
    if validate:
        result["validation"] = _deck.validate_deck(str(dst))
        if not result["validation"].get("ok"):
            result["ok"] = False
            result["error"] = "cloned deck failed validation — see 'validation'"
    return result


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _apply_op(text: str, op: dict) -> tuple[str, dict]:
    kind = (op.get("op") or "").lower()
    if kind == "append_before_end":
        return _append_before_end(text, op.get("text", ""), anchor=op.get("anchor", "END"))
    if kind == "insert_after_keyword":
        return _insert_after_keyword(text, op.get("keyword", ""), op.get("text", ""))
    if kind == "replace_regex":
        return _replace_regex(
            text,
            op.get("pattern", ""),
            op.get("replacement", ""),
            count=int(op.get("count", 0)),
            flags=op.get("flags", ""),
        )
    if kind == "set_title":
        return _set_title(text, op.get("text", ""))
    return text, {"op": kind, "ok": False, "error": f"unknown op '{kind}'"}


def _append_before_end(text: str, block: str, anchor: str = "END") -> tuple[str, dict]:
    if not block:
        return text, {"op": "append_before_end", "ok": False, "error": "empty text"}
    block = block.rstrip("\n") + "\n"
    # Match the anchor keyword on its own line (END is a bare keyword).
    pat = re.compile(rf"(?im)^[ \t]*{re.escape(anchor)}\b.*$")
    m = pat.search(text)
    if m:
        new = text[: m.start()] + block + "\n" + text[m.start():]
        return new, {"op": "append_before_end", "ok": True, "anchor": anchor, "inserted_chars": len(block)}
    # No anchor — just append.
    return text.rstrip("\n") + "\n\n" + block, {
        "op": "append_before_end",
        "ok": True,
        "anchor": None,
        "note": f"'{anchor}' not found; appended at end of file",
    }


def _insert_after_keyword(text: str, keyword: str, block: str) -> tuple[str, dict]:
    if not keyword or not block:
        return text, {"op": "insert_after_keyword", "ok": False, "error": "keyword and text required"}
    block = block.rstrip("\n") + "\n"
    pat = re.compile(rf"(?im)^[ \t]*{re.escape(keyword)}\b.*$")
    m = pat.search(text)
    if not m:
        return text, {"op": "insert_after_keyword", "ok": False, "error": f"keyword '{keyword}' not found"}
    insert_at = text.find("\n", m.end())
    insert_at = insert_at + 1 if insert_at != -1 else len(text)
    new = text[:insert_at] + block + text[insert_at:]
    return new, {"op": "insert_after_keyword", "ok": True, "keyword": keyword}


def _replace_regex(text: str, pattern: str, replacement: str, count: int = 0, flags: str = "") -> tuple[str, dict]:
    if not pattern:
        return text, {"op": "replace_regex", "ok": False, "error": "empty pattern"}
    f = 0
    if "i" in flags.lower():
        f |= re.IGNORECASE
    if "m" in flags.lower():
        f |= re.MULTILINE
    try:
        rx = re.compile(pattern, f)
    except re.error as exc:
        return text, {"op": "replace_regex", "ok": False, "error": f"bad regex: {exc}"}
    new, n = rx.subn(replacement, text, count=count)
    info = {"op": "replace_regex", "ok": n > 0, "pattern": pattern, "n_replacements": n}
    if n == 0:
        info["error"] = "pattern matched nothing (no substitution made)"
    return new, info


def _set_title(text: str, title: str) -> tuple[str, dict]:
    if not title:
        return text, {"op": "set_title", "ok": False, "error": "empty title"}
    pat = re.compile(r"(?im)^[ \t]*TITLE[ \t]*$\n(.*)$")
    m = pat.search(text)
    if m:
        new = text[: m.start(1)] + f"  {title}" + text[m.end(1):]
        return new, {"op": "set_title", "ok": True}
    return text, {"op": "set_title", "ok": False, "error": "TITLE keyword (on its own line) not found"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_sibling(src: Path) -> Path:
    stem, suffix = src.stem, src.suffix or ".DATA"
    parent = src.parent
    for n in range(1, 1000):
        cand = parent / f"{stem}_v{n}{suffix}"
        if not cand.exists():
            return cand
    return SETTINGS.work_dir / "decks" / f"{stem}_clone{suffix}"


def _flatten(deck_path: str) -> tuple[str, str | None]:
    """Inline INCLUDEs via opmpack; returns (text, error)."""
    deck_wsl = wsl.to_wsl_path(deck_path)
    res = wsl.run(["opmpack", deck_wsl], check=False, timeout=120)
    if res.returncode != 0:
        return "", res.stderr[-500:] or f"exit {res.returncode}"
    return res.stdout, None
