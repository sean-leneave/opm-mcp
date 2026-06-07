# Contributing to opm-mcp

Thanks for your interest! This server is small enough that contributions can
land quickly. Pull requests, bug reports, and ideas for new tools are all
welcome.

## Where to start

- **Bug?** Open an issue with the output of `opm_environment` (it tells us
  which OPM binaries / Python bindings the server can reach) plus the exact
  tool call that failed.
- **New tool?** Open a short design issue first describing what binary or
  workflow you want exposed and what the tool's inputs/outputs look like. We
  prefer **one tool per OPM concept** so the model has a clear surface.
- **Doc fixes?** Send a PR directly.

## Dev setup

```powershell
git clone https://github.com/ojaogezi/opm-mcp.git
cd opm-mcp
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

You'll need a WSL2 distro with OPM Flow installed to exercise the full server
end-to-end. The pure-Python unit tests under `tests/` don't need WSL and run
in CI.

## Project layout

See [README.md](README.md#project-layout) for the source tree.

The split is deliberate:

- `wsl.py` is the only module that touches `subprocess` / `wsl.exe`.
- `tools/*.py` are pure orchestration — they call `wsl.run`, `wsl.popen`, or
  the bundled helper scripts and return JSON-friendly dicts.
- `helpers/*.py` are scripts that run *inside WSL* against the OPM Python
  bindings. They must remain stdlib-only on the Windows side (the server
  loads them as resources).

## Style

- Python 3.11+ syntax. We use `X | None` annotations rather than `Optional[X]`.
- No third-party formatting tool is required, but contributions are expected
  to match the existing style: 4-space indents, type hints on public APIs,
  module-level docstrings that explain *why*, not just *what*.
- All MCP tool functions should:
  1. Accept caller-supplied paths in either Windows or WSL form.
  2. Return a JSON-serialisable dict (no numpy arrays, no enums).
  3. Have a one-line docstring — that string becomes the tool description Claude sees.

## Tests

The unit-test suite covers path translation, the WSL command builder, and
basic config parsing. Integration tests that actually invoke `flow` are
optional and gated behind `OPM_MCP_RUN_INTEGRATION=1` so CI doesn't need WSL.

## Releasing

Until v1.0 we bump the patch version in `pyproject.toml` per change and tag
releases manually. For now, prefer a CHANGELOG entry per PR.
