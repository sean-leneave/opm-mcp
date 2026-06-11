#!/usr/bin/env python3
"""Helper run *inside WSL* to inspect an ECLIPSE/Flow deck.

Uses the ``python3-opm-common`` bindings (``opm.io.parser`` and
``opm.io.ecl_state``) which are not available on the Windows host. Designed to
be invoked from the MCP server as::

    python3 /mnt/c/.../deck_inspect.py <action> <deck_path>

Output is a single JSON document on stdout; errors are returned as
``{"ok": false, "error": "..."}`` rather than raised so the server can surface
them as tool results.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def _parse(deck_path: str):
    from opm.io.parser import Parser, ParseContext, action  # type: ignore

    parser = Parser()
    # The OPM 2025.10 bindings expose lowercase enum members:
    # action.ignore | action.warn | action.throw. Be permissive so we can parse
    # research-quality decks that may have non-fatal quirks.
    permissive = [
        ("PARSE_RANDOM_SLASH", action.ignore),
        ("PARSE_MISSING_INCLUDE", action.warn),
        ("PARSE_EXTRA_RECORDS", action.warn),
        ("PARSE_UNKNOWN_KEYWORD", action.warn),
        ("UNSUPPORTED_INITIAL_THPRES", action.warn),
    ]
    try:
        ctx = ParseContext(permissive)
    except Exception:
        ctx = ParseContext()
    return parser.parse(deck_path, ctx)


def _validate(deck_path: str) -> dict:
    deck = _parse(deck_path)
    keywords = [kw.name for kw in deck]
    return {
        "ok": True,
        "deck_path": deck_path,
        "n_keywords": len(keywords),
        "unique_keywords": sorted(set(keywords)),
        "issues": [],
    }


def _summarize(deck_path: str) -> dict:
    from opm.io.ecl_state import EclipseState  # type: ignore
    from opm.io.schedule import Schedule  # type: ignore

    deck = _parse(deck_path)
    state = EclipseState(deck)
    grid = state.grid()
    runspec_kw = {kw.name for kw in deck if kw.name in {"OIL", "GAS", "WATER", "DISGAS", "VAPOIL", "METRIC", "FIELD", "LAB", "PVT-M"}}
    sched = Schedule(deck, state)

    well_names = _well_names(sched)
    groups = _group_names(sched)
    n_steps = _n_report_steps(sched)
    start_date = _date(sched, "start")
    end_date = _date(sched, "end")

    return {
        "ok": True,
        "deck_path": deck_path,
        "title": _title(deck),
        "phases": sorted(p for p in runspec_kw if p in {"OIL", "GAS", "WATER", "DISGAS", "VAPOIL"}),
        "units": next((u for u in runspec_kw if u in {"METRIC", "FIELD", "LAB", "PVT-M"}), None),
        "grid": {
            "nx": grid.nx,
            "ny": grid.ny,
            "nz": grid.nz,
            "n_active": grid.nactive,
            "cartesian_size": grid.nx * grid.ny * grid.nz,
        },
        "wells": {"count": len(well_names), "names": well_names[:200]},
        "groups": {"count": len(groups), "names": groups[:50]},
        "schedule": {
            "report_steps": n_steps,
            "start": start_date,
            "end": end_date,
        },
    }


def _wells(deck_path: str) -> dict:
    from opm.io.ecl_state import EclipseState  # type: ignore
    from opm.io.schedule import Schedule  # type: ignore

    deck = _parse(deck_path)
    state = EclipseState(deck)
    sched = Schedule(deck, state)

    well_records: list[dict] = []
    for name in _well_names(sched):
        record: dict = {"name": name}
        try:
            w = sched.get_well(name, 0)
            # The Well binding's attribute names vary across OPM versions; we
            # probe a handful and keep whatever comes back as a string.
            for attr in ("preferred_phase", "status", "group_name", "well_type", "I", "J", "ref_depth"):
                if hasattr(w, attr):
                    try:
                        val = getattr(w, attr)
                        record[attr] = val() if callable(val) else val
                    except Exception:
                        pass
        except Exception as exc:
            record["note"] = f"introspection unavailable: {exc}"
        well_records.append(record)
    return {"ok": True, "deck_path": deck_path, "wells": well_records}


def _well_names(sched) -> list[str]:
    try:
        return list(sched.well_names("*"))
    except TypeError:
        return list(sched.well_names())


def _group_names(sched) -> list[str]:
    # opm 2025.10 exposes Schedule._groups(report_step) -> [Group] with .name;
    # older bindings had group_names()/groups. Probe newest-first.
    if hasattr(sched, "_groups"):
        try:
            return [g.name for g in sched._groups(0)]
        except Exception:
            pass
    for accessor in ("group_names", "groups"):
        if hasattr(sched, accessor):
            try:
                val = getattr(sched, accessor)
                return list(val() if callable(val) else val)
            except Exception:
                continue
    return []


def _n_report_steps(sched) -> int | None:
    for accessor in ("reportsteps", "timesteps"):
        if hasattr(sched, accessor):
            try:
                val = getattr(sched, accessor)
                seq = val() if callable(val) else val
                return len(list(seq))
            except Exception:
                continue
    return None


def _date(sched, which: str) -> str | None:
    if not hasattr(sched, which):
        return None
    try:
        val = getattr(sched, which)
        d = val() if callable(val) else val
        return str(d)
    except Exception:
        return None


def _title(deck) -> str | None:
    for kw in deck:
        if kw.name == "TITLE":
            # One DeckRecord with one string DeckItem holding the title words.
            # DeckItem has no __getitem__/__len__; the values come from
            # get_data_list() (all words) or get_str(i) (one word).
            try:
                item = kw[0][0]
                if hasattr(item, "get_data_list"):
                    return " ".join(str(s) for s in item.get_data_list()).strip()
                return str(item.get_str(0)).strip()
            except Exception:
                return None
    return None


_ACTIONS = {
    "validate": _validate,
    "summarize": _summarize,
    "wells": _wells,
}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in _ACTIONS:
        print(json.dumps({"ok": False, "error": "usage: deck_inspect.py {validate|summarize|wells} <deck>"}))
        return 2
    deck_path = argv[2]
    if not Path(deck_path).exists():
        print(json.dumps({"ok": False, "error": f"deck not found: {deck_path}"}))
        return 1
    try:
        result = _ACTIONS[argv[1]](deck_path)
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(limit=4),
        }
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
