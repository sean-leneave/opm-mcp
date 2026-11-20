"""Security guardrails (sean-leneave fork).

Two kinds of check live here so that every tool applies them the same way:

* **Path confinement** — every path a tool reads or writes must resolve (after
  symlinks and ``..``) inside one of ``config.SETTINGS.roots``. A deck comment, a
  document or a prompt can steer an agent towards ``~/.ssh`` or ``/etc``; this
  makes that a refused call instead of a leak or an overwrite.
* **Flow option policy** — ``extra_args`` may only carry known, harmless Flow
  options in ``--name=value`` form, and a run may not ask for more cores than
  ``config.SETTINGS.max_cores``. This protects both the host and repeatability: an
  unvetted option can silently change the numerics.
"""

from __future__ import annotations

import inspect
import os
import re
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config, wsl

# Tool parameters that carry a filesystem path. Checked at the tool boundary.
PATH_ARGS: frozenset[str] = frozenset(
    {
        "directory",
        "deck_path",
        "case_path",
        "case_a",
        "case_b",
        "output_path",
        "output_dir",
        "observed_csv",
        "base_deck",
        "restart_case",
        "output_deck",
        "input_path",
        "cwd",
    }
)


class PathNotAllowed(PermissionError):
    """A path resolved outside the configured roots."""


def _local(path: str | Path) -> Path:
    """Normalise a caller path (Windows or WSL form) to a host path, like the tools do."""
    raw = str(path).strip()
    if raw.startswith("/"):
        raw = wsl.to_windows_path(raw)
    return Path(raw).expanduser()


def _real(path: Path) -> str:
    # strict=False: the path may not exist yet (an output file); existing
    # parents are still resolved, so a symlinked parent cannot escape.
    return os.path.normcase(os.path.realpath(str(path)))


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def roots() -> tuple[str, ...]:
    return tuple(_real(_local(r)) for r in config.SETTINGS.roots)


def is_allowed(path: str | Path) -> bool:
    real = _real(_local(path))
    return any(_within(real, r) for r in roots())


def confine(path: str | Path, *, what: str = "path") -> str:
    """Return ``path`` unchanged if it resolves inside a root, else raise."""
    if path is None or str(path).strip() == "":
        return path  # type: ignore[return-value]
    if not is_allowed(path):
        raise PathNotAllowed(
            f"{what} '{path}' is outside the allowed roots. "
            f"Allowed: {', '.join(str(r) for r in config.SETTINGS.roots)}. "
            "Set OPM_MCP_ROOTS to change this."
        )
    return str(path)


def confine_args(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: confine every argument named in :data:`PATH_ARGS`."""
    sig = inspect.signature(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        for name, value in bound.arguments.items():
            if name in PATH_ARGS and isinstance(value, (str, Path)) and str(value).strip():
                confine(value, what=name)
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Flow option policy
# ---------------------------------------------------------------------------

# Options that change solver behaviour or diagnostics but cannot point Flow at
# other files or change where it writes. Exact option names; values follow '='.
FLOW_ALLOWED_OPTIONS: frozenset[str] = frozenset(
    {
        "--threads-per-process",
        "--linear-solver",
        "--linear-solver-reduction",
        "--linear-solver-max-iter",
        "--linear-solver-restart",
        "--tolerance-mb",
        "--tolerance-cnv",
        "--tolerance-cnv-relaxed",
        "--newton-max-iterations",
        "--newton-min-iterations",
        "--solver-max-time-step-in-days",
        "--initial-time-step-in-days",
        "--time-step-control",
        "--min-time-step-before-shutting-problematic-wells-in-days",
        "--enable-tuning",
        "--output-extra-convergence-info",
        "--parsing-strictness",
        "--enable-dry-run",
        "--matrix-add-well-contributions",
        "--accelerator-mode",
    }
)

# Never allowed, whatever OPM_MCP_FLOW_EXTRA_ALLOW says: these choose files or
# output locations, which the server controls.
FLOW_FORBIDDEN_OPTIONS: frozenset[str] = frozenset(
    {"--output-dir", "--parameter-file", "--ecl-deck-file-name", "--ecl-output-dir"}
)

_OPTION_RE = re.compile(r"^(--[a-z0-9][a-z0-9-]*)(?:=(.*))?$")


class FlowOptionRejected(ValueError):
    """An extra Flow argument failed the allow-list."""


def validate_flow_extra_args(extra: Iterable[str] | None) -> list[str]:
    """Return the accepted extra args, or raise :class:`FlowOptionRejected`.

    Rules: each item is a single ``--name`` or ``--name=value`` token (no
    positional arguments, so no second deck or stray file); the name is on the
    allow-list (built-in plus ``OPM_MCP_FLOW_EXTRA_ALLOW``) and not forbidden;
    values contain no path separators.
    """
    accepted: list[str] = []
    allowed = FLOW_ALLOWED_OPTIONS | set(config.SETTINGS.flow_extra_allow)
    for item in extra or []:
        token = str(item).strip()
        m = _OPTION_RE.match(token)
        if not m:
            raise FlowOptionRejected(f"'{token}' is not a single --option or --option=value token")
        name, value = m.group(1), m.group(2)
        if name in FLOW_FORBIDDEN_OPTIONS:
            raise FlowOptionRejected(f"{name} is controlled by the server and cannot be passed")
        if name not in allowed:
            raise FlowOptionRejected(
                f"{name} is not on the Flow option allow-list. "
                "Add it to OPM_MCP_FLOW_EXTRA_ALLOW after review if it is needed."
            )
        if value is not None and ("/" in value or "\\" in value):
            raise FlowOptionRejected(f"{name}: values may not contain paths")
        accepted.append(token)
    return accepted


def check_cores(mpi_processes: int | None, threads: int | None) -> None:
    """Refuse runs that ask for more cores than ``config.SETTINGS.max_cores`` (0 = no limit)."""
    if not config.SETTINGS.max_cores:
        return
    procs = max(1, int(mpi_processes or 1))
    per = max(1, int(threads or 1))
    if procs * per > config.SETTINGS.max_cores:
        raise ValueError(
            f"run asks for {procs} process(es) x {per} thread(s) = {procs * per} cores, "
            f"above OPM_MCP_MAX_CORES={config.SETTINGS.max_cores}"
        )
