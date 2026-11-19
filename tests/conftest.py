"""Shared test setup (sean-leneave fork).

* Never read a developer's real ``~/.config/opm-mcp/env`` during tests.
* Path confinement (``opm_mcp.guard``) is on for every test. Upstream tests
  build decks under pytest's ``tmp_path``, so each test's ``tmp_path`` is added
  to the allowed roots for that test only. Tests of the guard itself use
  ``strict_roots`` to opt out of this.
"""

from __future__ import annotations

import os

os.environ["OPM_MCP_ENV_FILE"] = os.devnull

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _allow_tmp_path(request, tmp_path):
    if "strict_roots" in request.keywords:
        yield
        return
    import opm_mcp.config as cfg

    original = cfg.SETTINGS.roots
    object.__setattr__(cfg.SETTINGS, "roots", (*original, tmp_path))
    try:
        yield
    finally:
        object.__setattr__(cfg.SETTINGS, "roots", original)


def pytest_configure(config):
    config.addinivalue_line("markers", "strict_roots: run with only the configured roots allowed")
