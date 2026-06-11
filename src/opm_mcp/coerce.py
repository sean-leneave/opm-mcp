"""Lenient input coercion at the MCP tool boundary.

Some MCP clients flatten optional-parameter schema unions when rendering tool
definitions, so the model can't always see that a parameter wants
``list[str]`` — observed in the wild as ``extra_args="--enable-tuning=true"``
bouncing off pydantic with a ``list_type`` error. Rather than fail the call,
the server-side wrappers widen those parameters to ``list[str] | str`` and
normalise here.

Two flavours because the natural string syntax differs:

- CLI-flag params (``extra_args``, upscaling ``args``): shell-style splitting,
  so quoting still works — ``'--a "--b=x y"'`` → ``["--a", "--b=x y"]``.
- Summary-key params (``keys``, ``keywords``): comma/whitespace separated —
  ``"FOPR, FGPR FPR"`` → ``["FOPR", "FGPR", "FPR"]``.
"""

from __future__ import annotations

import re
import shlex

_KEY_SEP = re.compile(r"[\s,]+")


def arg_list(value: list[str] | str | None) -> list[str] | None:
    """Normalise a CLI-argument parameter to ``list[str]`` (shell-style split)."""
    if value is None or isinstance(value, list):
        return value
    return shlex.split(str(value))


def key_list(value: list[str] | str | None) -> list[str] | None:
    """Normalise a summary-key parameter to ``list[str]`` (comma/space split)."""
    if value is None or isinstance(value, list):
        return value
    return [k for k in _KEY_SEP.split(str(value).strip()) if k]
