# Changelog

All notable changes to this project will be documented in this file. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
