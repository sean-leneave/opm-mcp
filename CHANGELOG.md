# Changelog

All notable changes to this project will be documented in this file. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Native plotting**: `plot_summary` renders summary vectors to PNG with
  automatic twin-axis grouping by engineering unit and a date/days x-axis — no
  ResInsight/OpenGL dependency.
- **RE analytics**: `material_balance_check` (OOIP, recovery factor, exact oil
  mass-balance drift, surface VRR), `flow_convergence_report` (INFOSTEP/INFOITER
  parsing — wall time by stage, wasted cut steps, worst timesteps, residuals),
  and `restart_property` (per-layer, PV-weighted stats for PRESSURE/SWAT/SGAS/
  SOIL at a report step).
- **Deck editing**: `clone_deck` writes a new deck with safe, verifiable text
  overrides (`append_before_end`, `replace_regex`, `set_title`,
  `insert_after_keyword`), reports per-op substitution counts, and re-validates —
  enabling clone → run → compare sensitivity loops.
- `read_summary`/`field_summary`/`well_summary` now report per-vector
  engineering units; `read_summary` also returns an elapsed-`days` axis.
- `run_flow` now writes INFOSTEP/INFOITER by default (`convergence_info`).

### Changed

- **Job manager is now restart-safe.** Flow's Linux PID is captured via a
  `$$`+`exec` wrapper (reliable where `$!`/`$BASHPID` are not under `wsl.exe`),
  jobs are rehydrated from disk on startup, and reloaded jobs can be polled and
  cancelled (`wsl kill`). The old "spawn → kill → respawn" redirection hack is
  gone; `Popen.wait()` now yields flow's true exit code.
- Path translation detects the WSL mount root via `wslpath` instead of
  hardcoding `/mnt/`, so non-default `automount.root` configs work.
- Log tailing reads backward from EOF instead of slurping the whole file
  (status polling is no longer O(filesize)).
- `Summary` objects are cached by mtime to avoid re-parsing SMSPEC repeatedly.

### Fixed

- Replaced deprecated `datetime.utcfromtimestamp` (warned/removed on 3.12+).
- Example deck now requests `INIT`, so `grid_info` static-property stats and
  PV-weighted `restart_property` averages work.

## [0.1.0] - 2026-06-07

### Added

- Initial MCP server exposing 28 tools across the OPM stack.
- **Deck inspection** (`validate_deck`, `summarize_deck`, `list_wells`) via
  `python3-opm-common` inside WSL.
- **Flow simulation jobs** (`run_flow`, `flow_status`, `flow_log_tail`,
  `flow_cancel`, `flow_jobs`) with background-process tracking and PRT-log
  scanning.
- **Result readers** (`list_summary_vectors`, `read_summary`,
  `field_summary`, `well_summary`, `grid_info`, `restart_info`,
  `compare_summaries`) using `resdata` natively on Windows.
- **ResInsight automation** (`resinsight_snapshot`, `resinsight_well_log`,
  `resinsight_open`) via the `--commandFile` DSL.
- **OPM utility binaries** (`convert_ecl`, `compare_ecl`, `pack_deck`,
  `hash_deck`, `summary_tool`).
- **Optional upscaling** (`upscaling_status`, `run_upscaling`) — gated on
  `opm-upscaling` being installed in WSL.
- **Environment helpers** (`opm_environment`, `to_wsl_path`,
  `to_windows_path`).
- SPE1-style example deck under `examples/SPE1_MINI.DATA`.
- README with full tool reference + Claude Desktop / Claude Code config
  snippets.
