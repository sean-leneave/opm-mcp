"""Plain-text deck reading — runs natively on the host, no WSL round-trip.

ECLIPSE decks are ASCII, so keyword-level inspection doesn't need the
``python3-opm-common`` bindings. These tools exist because the agent otherwise
falls back to host-shell ``grep``/``sed`` (fragile, path-dialect dependent,
and some clients refuse to open ``.DATA`` files at all, misdetecting them as
binary).

``deck_grep`` searches the deck *and its INCLUDE tree*, resolving the
``PATHS`` keyword's ``$ALIAS`` substitutions, so "show me VFPPROD wherever it
lives" is one call. Resolution is deliberately textual — no full parse — which
keeps it working on decks the parser rejects.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import guard, wsl

# Skip pattern-searching huge data blobs (GRDECL arrays can run to 100s of MB);
# they're noise for keyword greps and slow to scan.
_MAX_GREP_BYTES = 8_000_000

_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_KEYWORD_LINE = re.compile(r"^[A-Z][A-Z0-9_-]{0,7}\s*$")


def _to_local(path: str) -> Path:
    """Normalise a caller-supplied path (Windows or WSL form) to a host Path."""
    raw = str(path).strip()
    if raw.startswith("/"):
        raw = wsl.to_windows_path(raw)
    return Path(raw).expanduser()


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _strip_comment(line: str) -> str:
    """Drop an ECLIPSE ``--`` comment, respecting quoted strings."""
    out = []
    in_quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            out.append(ch)
        elif ch in "'\"":
            in_quote = ch
            out.append(ch)
        elif ch == "-" and line[i : i + 2] == "--":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _record_token(text: str) -> str | None:
    """First filename-ish token of a record: quoted string or bare word before ``/``."""
    m = _QUOTED.search(text)
    if m:
        return m.group(1) if m.group(1) is not None else m.group(2)
    bare = text.split("/", 1)[0].strip()
    return bare.split()[0] if bare.split() else None


def _parse_paths_aliases(lines: list[str]) -> dict[str, str]:
    """Extract ``PATHS`` alias → path mappings (records end at a lone ``/``)."""
    aliases: dict[str, str] = {}
    it = iter(enumerate(lines))
    for _, line in it:
        if _strip_comment(line).strip().upper() != "PATHS":
            continue
        for _, rec_line in it:
            rec = _strip_comment(rec_line).strip()
            if not rec:
                continue
            if rec == "/":  # end of keyword
                break
            tokens = [t for pair in _QUOTED.findall(rec) for t in pair if t]
            if len(tokens) >= 2:
                aliases[tokens[0]] = tokens[1]
        break
    return aliases


def _expand_aliases(token: str, aliases: dict[str, str]) -> str:
    for name in sorted(aliases, key=len, reverse=True):
        token = token.replace(f"${name}", aliases[name])
    return token


def _includes_of(path: Path, aliases: dict[str, str]) -> list[Path]:
    """Resolved INCLUDE targets of one file (relative to that file's folder)."""
    out: list[Path] = []
    lines = _read_lines(path)
    i = 0
    while i < len(lines):
        if _strip_comment(lines[i]).strip().upper() == "INCLUDE":
            j = i + 1
            while j < len(lines):
                rec = _strip_comment(lines[j]).strip()
                if rec:
                    token = _record_token(rec)
                    if token:
                        expanded = _expand_aliases(token, aliases)
                        target = Path(expanded)
                        if not target.is_absolute():
                            target = path.parent / target
                        out.append(target)
                    i = j
                    break
                j += 1
        i += 1
    return out


def collect_files(deck_path: str, max_files: int = 64) -> tuple[list[Path], list[str]]:
    """The deck plus its transitive INCLUDE tree (cycle-safe, breadth-first).

    Returns ``(existing_files, missing_paths)``.
    """
    root = _to_local(deck_path)
    if not root.exists():
        raise FileNotFoundError(f"deck not found: {root}")
    aliases = _parse_paths_aliases(_read_lines(root))
    files: list[Path] = []
    missing: list[str] = []
    seen: set[str] = set()
    queue = [root]
    while queue and len(files) < max_files:
        current = queue.pop(0)
        key = str(current.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if not guard.is_allowed(current):
            # An INCLUDE that points outside the allowed roots is reported, never read.
            missing.append(f"{current} (blocked: outside allowed roots)")
            continue
        if not current.exists():
            missing.append(str(current))
            continue
        files.append(current)
        try:
            queue.extend(_includes_of(current, aliases))
        except OSError:
            continue
    return files, missing


def _resolve_member(deck_path: str, file: str | None) -> Path | dict[str, Any]:
    """Resolve ``file`` against the deck's INCLUDE tree (or return the deck).

    Every candidate must lie inside the allowed roots (``guard``); a ``file``
    argument cannot be used to read arbitrary files.
    """
    if not file:
        return _to_local(deck_path)
    cand = _to_local(file)
    if cand.exists():
        return Path(guard.confine(cand, what="file"))
    if not cand.is_absolute():
        rel = _to_local(deck_path).parent / str(file)
        if rel.exists():
            return Path(guard.confine(rel, what="file"))
    # Last resort: match by bare filename anywhere in the include tree.
    files, _ = collect_files(deck_path)
    wanted = Path(str(file)).name.lower()
    found = next((f for f in files if f.name.lower() == wanted), None)
    if found is None:
        return {
            "ok": False,
            "error": f"include file not found in deck tree: {file}",
            "files_in_tree": [str(f) for f in files],
        }
    return found


def deck_text(
    deck_path: str,
    start_line: int = 1,
    n_lines: int = 200,
    file: str | None = None,
) -> dict[str, Any]:
    """Numbered text slice of the deck (or one of its include files)."""
    target = _resolve_member(deck_path, file)
    if isinstance(target, dict):
        return target
    if not target.exists():
        return {"ok": False, "error": f"file not found: {target}"}
    lines = _read_lines(target)
    start = max(1, int(start_line))
    end = min(len(lines), start + max(1, int(n_lines)) - 1)
    numbered = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
    return {
        "ok": True,
        "file_win": str(target),
        "file_wsl": wsl.to_wsl_path(str(target)),
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "text": numbered,
    }


def deck_grep(
    deck_path: str,
    pattern: str,
    context: int = 2,
    search_includes: bool = True,
    max_matches: int = 40,
    ignore_case: bool = True,
) -> dict[str, Any]:
    """Regex search across the deck and (optionally) its resolved INCLUDE tree."""
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return {"ok": False, "error": f"bad regex: {exc}"}
    files, missing = collect_files(deck_path)
    if not search_includes:
        files = files[:1]
    matches: list[dict[str, Any]] = []
    skipped: list[str] = []
    truncated = False
    for f in files:
        if f.stat().st_size > _MAX_GREP_BYTES:
            skipped.append(f"{f} ({f.stat().st_size} bytes > {_MAX_GREP_BYTES})")
            continue
        lines = _read_lines(f)
        for idx, line in enumerate(lines, 1):
            if not rx.search(line):
                continue
            if len(matches) >= max_matches:
                truncated = True
                break
            lo, hi = max(0, idx - 1 - context), min(len(lines), idx + context)
            matches.append(
                {
                    "file": str(f),
                    "line": idx,
                    "text": line.rstrip(),
                    "before": [ln.rstrip() for ln in lines[lo : idx - 1]],
                    "after": [ln.rstrip() for ln in lines[idx:hi]],
                }
            )
        if truncated:
            break
    return {
        "ok": True,
        "pattern": pattern,
        "n_matches": len(matches),
        "truncated": truncated,
        "files_searched": [str(f) for f in files],
        "files_missing": missing,
        "files_skipped_too_large": skipped,
        "matches": matches,
    }
