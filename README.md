# opm-mcp

[![CI](https://github.com/ojaogezi/opm-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ojaogezi/opm-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-server-blueviolet)](https://modelcontextprotocol.io/)

A Model Context Protocol (MCP) server that gives Claude direct access to the
[Open Porous Media](https://opm-project.org/) reservoir-simulation stack:

- **OPM Flow** — black-oil and compositional reservoir simulator (ECLIPSE-compatible).
- **ResInsight** — 3D viewer and post-processor.
- **OPM utility binaries** — `opmpack`, `opmhash`, `summary`, `convertECL`, `compareECL`.
- **OPM upscaling** (optional `opm-upscaling` package).

The server runs **on Windows** so Claude Code / Claude Desktop can spawn it
natively, but it delegates every Linux-only binary to an Ubuntu distro under
WSL2. Result files (UNSMRY / UNRST / EGRID / INIT) are parsed natively on
Windows via [`resdata`](https://github.com/equinor/resdata).

## Architecture

```
   +-------------------+              +----------------------+
   |     Windows       |              |   WSL2 Ubuntu-24.04  |
   |                   |              |                      |
   |  Claude  <----->  |   wsl.exe    |   flow               |
   |  opm-mcp (Py)     | -----------> |   ResInsight         |
   |     resdata       |              |   python3-opm-common |
   |     (UNSMRY etc.) |              |   convertECL/etc.    |
   +-------------------+              +----------------------+
            ^                                       |
            |     reads result files via UNC /      |
            +---  /mnt/c/... path translation  -----+
```

## Prerequisites

On Windows:
- Python 3.11+ (tested with 3.13).
- WSL2 enabled, with an Ubuntu-24.04 distro available (`wsl -l -v`).

In the WSL distro, install OPM:

```bash
sudo apt-get update
sudo apt-get install flow resinsight python3-opm-common python3-opm-simulators
# Optional, for the upscaling tools:
sudo apt-get install opm-upscaling
# Optional, for fully headless ResInsight rendering without WSLg:
sudo apt-get install xvfb mesa-utils libgl1-mesa-dri
```

## Install

```powershell
git clone https://github.com/ojaogezi/opm-mcp.git
cd opm-mcp
py -m pip install -e .
```

This installs the `opm-mcp` console script and pulls in `mcp[cli]`, `resdata`,
`pydantic` and `numpy`.

For development:

```powershell
py -m pip install -e ".[dev]"
pytest
```

## Register with Claude Code

```powershell
claude mcp add opm --scope user -- py -m opm_mcp.server
```

Or, if you prefer a JSON config (Claude Desktop / cursor / etc.), add this to
your `mcpServers` block:

```json
{
  "mcpServers": {
    "opm": {
      "command": "py",
      "args": ["-m", "opm_mcp.server"],
      "env": {
        "OPM_MCP_WSL_DISTRO": "Ubuntu-24.04"
      }
    }
  }
}
```

## Configuration (env vars)

| Variable                  | Default              | Purpose                                        |
| ------------------------- | -------------------- | ---------------------------------------------- |
| `OPM_MCP_WSL_DISTRO`      | `Ubuntu-24.04`       | Name of the WSL distro to run OPM in.          |
| `OPM_MCP_FLOW_BIN`        | `flow`               | Binary name / absolute WSL path for Flow.      |
| `OPM_MCP_RESINSIGHT_BIN`  | `ResInsight`         | Binary name / absolute WSL path for ResInsight.|
| `OPM_MCP_WORK_DIR`        | `~/.opm-mcp` (Win)   | Where job logs and artefacts go.               |
| `OPM_MCP_FLOW_THREADS`    | `0` (auto)           | `--threads-per-process` default for runs.      |
| `OPM_MCP_JOB_HISTORY`     | `50`                 | How many recent jobs to remember in-memory.    |
| `OPM_MCP_LOG_TAIL`        | `200`                | Default line count for `flow_log_tail`.        |

## Tool reference

### Environment & paths

| Tool              | Purpose                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `opm_environment` | Probe `wsl.exe`, `flow`, `ResInsight`, OPM Python bindings.        |
| `to_wsl_path`     | Translate `C:\foo\bar` to `/mnt/c/foo/bar`.                        |
| `to_windows_path` | Reverse translation.                                               |

### Deck inspection (uses `python3-opm-common` in WSL)

| Tool             | Purpose                                                         |
| ---------------- | --------------------------------------------------------------- |
| `validate_deck`  | Parse a DATA deck, report keyword counts.                       |
| `summarize_deck` | Phases, units, grid dims, well/group counts, schedule length.   |
| `list_wells`     | Per-well info at schedule step 0.                               |

### OPM Flow (background jobs)

| Tool             | Purpose                                                              |
| ---------------- | -------------------------------------------------------------------- |
| `run_flow`       | Start a Flow job; returns a `job_id` immediately.                    |
| `flow_status`    | Current state + last progress line (`Report step N/M ...`).          |
| `flow_log_tail`  | Tail of combined stdout/stderr.                                      |
| `flow_cancel`    | Cancel a running job.                                                |
| `flow_jobs`      | List recent jobs (most recent first).                                |

### Result readers (native Windows via `resdata`)

| Tool                     | Purpose                                                |
| ------------------------ | ------------------------------------------------------ |
| `list_summary_vectors`   | All summary keys (FOPR, WBHP:*, GOPR:*, ...).          |
| `read_summary`           | Time series for one or more keys, with per-vector units + elapsed days, downsampled. |
| `field_summary`          | Cumulative production/injection, FPR, final rates (with units). |
| `well_summary`           | Per-well WOPR/WGPR/WWPR/WBHP/WOPT/etc. min/max/last.   |
| `grid_info`              | EGRID dims, active count, INIT property stats.         |
| `restart_info`           | UNRST report step list + keywords.                     |
| `compare_summaries`      | Diff field totals between two cases.                   |

### Native plotting (matplotlib — no ResInsight/OpenGL)

| Tool            | Purpose                                                                |
| --------------- | --------------------------------------------------------------------- |
| `plot_summary`  | Render summary vectors to a PNG, auto twin-axis by unit, date or days x-axis. The fast path for rate/pressure/cumulative plots. |

### Reservoir-engineering analytics

| Tool                       | Purpose                                                              |
| -------------------------- | ------------------------------------------------------------------- |
| `material_balance_check`   | OOIP, recovery factor, **exact** oil mass-balance drift, surface VRR. |
| `flow_convergence_report`  | Parse INFOSTEP/INFOITER: wall time by stage, wasted (cut) steps, worst timesteps, residuals. |
| `restart_property`         | Per-layer stats (PV-weighted when INIT present) for PRESSURE/SWAT/SGAS/SOIL at a report step, plus histogram. |

> For PVT, SCAL/relative-permeability fitting and deck-physics validation, this
> server defers to the **[pyrestoolbox MCP](https://github.com/whitsonAS)** if
> you have it connected, rather than duplicating those calculators.

### Deck editing (sensitivities / history matching)

| Tool         | Purpose                                                                       |
| ------------ | ---------------------------------------------------------------------------- |
| `clone_deck` | Write a NEW deck with safe overrides (`append_before_end`, `replace_regex`, `set_title`, `insert_after_keyword`), reporting per-op substitution counts and re-validating. Enables clone → run → compare loops. |

### ResInsight automation (CLI command files)

| Tool                  | Purpose                                                          |
| --------------------- | ---------------------------------------------------------------- |
| `resinsight_snapshot` | Open a case, render the default 3D view, save PNG.               |
| `resinsight_well_log` | Export an LAS well log for one well.                             |
| `resinsight_open`     | Launch interactive ResInsight (uses WSLg on Windows 11).         |

If ResInsight fails with `MESA: error: ZINK ...`, pass
`force_software_gl: true` or install `xvfb` and `libgl1-mesa-dri` in WSL.

### OPM utility binaries

| Tool                 | Purpose                                                       |
| -------------------- | ------------------------------------------------------------- |
| `convert_ecl`        | Toggle formatted/unformatted ECL output files.                |
| `compare_ecl`        | Diff two result files within tolerances.                      |
| `pack_deck`          | Flatten a deck + INCLUDEs into one file (`opmpack`).          |
| `hash_deck`          | Keyword-level deterministic hash (`opmhash`).                 |
| `summary_tool`       | Text summary table via the OPM `summary` CLI.                 |
| `upscaling_status`   | Detect whether `opm-upscaling` is installed in WSL.           |
| `run_upscaling`      | Generic passthrough to `upscale_perm`, `upscale_relperm`, ... |

## Bigger test cases

This repo only ships the tiny `examples/SPE1_MINI.DATA`. For real benchmark
decks (Norne, SPE9, SPE10, Equinor gas-lift demos, etc.) use the upstream
[OPM/opm-tests](https://github.com/OPM/opm-tests) repository — they're
distributed under the Open Database License (ODbL 1.0) and shouldn't be
re-vendored here. Clone alongside:

```powershell
git clone https://github.com/OPM/opm-tests.git C:\models\opm-tests
```

Then point the MCP tools at any deck under that tree, e.g.
`C:\models\opm-tests\norne\NORNE_ATW2013.DATA`.

## Smoke test

```powershell
# 1. Validate a small example deck
py -c "from opm_mcp.tools import deck; import json; print(json.dumps(deck.summarize_deck(r'C:\Users\ogezi\RCI\opm-mcp\examples\SPE1_MINI.DATA'), indent=2))"

# 2. Run Flow on it (completes in a couple of seconds)
py -c "from opm_mcp.tools import flow; import time, json; j = flow.run_flow(r'C:\Users\ogezi\RCI\opm-mcp\examples\SPE1_MINI.DATA'); print(j['job_id']);
import time
time.sleep(5)
print(json.dumps(flow.flow_status(j['job_id']), indent=2))"

# 3. Read summary vectors
py -c "from opm_mcp.tools import results as r; import json; print(json.dumps(r.field_summary(r'C:\Users\ogezi\.opm-mcp\jobs\<JOB_ID>\out\SPE1_MINI'), indent=2))"
```

## Project layout

```
opm-mcp/
  pyproject.toml
  README.md
  examples/
    SPE1_MINI.DATA
  src/opm_mcp/
    __init__.py
    config.py            # env-var driven settings
    wsl.py               # WSL bridge + path translation (wslpath-aware)
    jobs.py              # background flow-job tracker (restart-safe)
    server.py            # FastMCP entry point (`opm-mcp` script)
    tools/
      deck.py            # validate / summarise / wells
      flow.py            # run, status, log_tail, cancel, jobs
      results.py         # resdata-backed summary/grid/restart readers (units + cache)
      plots.py           # native matplotlib plot_summary
      analysis.py        # material balance, convergence report, restart property
      edit.py            # clone_deck with safe overrides
      resinsight.py      # snapshot, well log, interactive open
      utils.py           # opmpack, opmhash, summary, compareECL, upscaling
    helpers/
      deck_inspect.py    # runs inside WSL — uses python3-opm-common
```

## Notes

- **Path handling.** Every path argument can be Windows-style (`C:\\foo`) or
  WSL-style (`/mnt/c/foo`, `/home/...`); the server normalises both directions.
  Result-file tools accept either the bare case name (`SPE1_MINI`) or any of
  its known extensions (`.DATA`, `.SMSPEC`, `.EGRID`, `.UNRST`).
- **Background simulations.** `run_flow` returns immediately and writes its
  Linux PID + exit code under `OPM_MCP_WORK_DIR/jobs/<id>/`. Jobs are
  **rehydrated on server restart**, so you can still poll (and cancel, via
  `wsl kill`) a run that was started before Claude Desktop restarted. Runs also
  emit INFOSTEP/INFOITER by default for `flow_convergence_report`.
- **Optional upscaling.** The `opm-upscaling` Debian package is *not* a
  dependency of Flow; `upscaling_status` tells Claude whether it's available
  and prints the install command if not.

## License

[MIT](LICENSE) — see [LICENSE](LICENSE) for the full text.

## Acknowledgements

- [Open Porous Media](https://opm-project.org/) — the underlying reservoir-simulation stack.
- [Equinor `resdata`](https://github.com/equinor/resdata) — open-source ECL result-file reader.
- [Model Context Protocol](https://modelcontextprotocol.io/) and the
  [official Python SDK](https://github.com/modelcontextprotocol/python-sdk).
