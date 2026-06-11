# Changelog

All notable changes to this project will be documented in this file. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added (field-feedback fixes — see FEEDBACK.md)

- **`deck_text` / `deck_grep`**: host-native deck text access — numbered
  slices and regex search across the deck *and its resolved INCLUDE tree*
  (`PATHS` `$ALIAS` substitution honoured, oversized GRDECL blobs skipped).
  No WSL round-trip; works even when the client misdetects `.DATA` as binary.
- **`flow_log_tail(grep=...)`**: case-insensitive regex filter over the whole
  job log with line numbers — extracts well-shut / solver-failure events
  without host-side grep.
- **Lenient list params** (`opm_mcp.coerce`): every `list[str]` tool parameter
  (`run_flow.extra_args`, `read_summary.keys`, `plot_summary.keys`,
  `compare_summaries.keys`, `summary_tool.keys`, `compare_ecl.keywords`,
  `run_upscaling.args`) now also accepts a plain string — shell-style split
  for CLI flags, comma/space split for summary keys. Fixes
  `extra_args="--enable-tuning=true"` bouncing with a pydantic `list_type`
  error in clients that flatten schema unions.

### Fixed

- **`summarize_deck`** returned `title: null` and `groups: count 0` with the
  opm 2025.10 bindings — title now read via `DeckItem.get_data_list()`, groups
  via `Schedule._groups(report_step)` (legacy accessors kept as fallback).
- **Dual-form paths in deck tools**: `validate_deck` / `summarize_deck` /
  `list_wells` echoed only the WSL form (`/mnt/c/...`), which does not resolve
  in the host shell. They now return `deck_path` (as passed), `deck_path_win`,
  and `deck_path_wsl`; server instructions document the convention.

### Changed

- **`field_summary`**: `n_report_steps` renamed to `n_summary_points` — the
  count is summary ministeps, not schedule report steps.

## [0.2.0] - 2026-06-08

### Added (analyst pack 2)

- **Wildcard summary keys**: `read_summary` / `plot_summary` expand patterns
  like `WWCT:*` and `GOPR:*` (capped, case-insensitive, unmatched patterns
  reported).
- **`plot_property_map`**: I–J areal heatmap of a layer property — dynamic
  (PRESSURE/SWAT/SGAS/SOIL from UNRST) or static (PORO/PERMX/NTG/PORV from
  INIT) — with no ResInsight/OpenGL dependency.
- **`compare_to_observed`**: observed-vs-simulated misfit (RMSE, bias, MAE,
  NRMSE per key) from a CSV of observations, with an overlay plot. The
  history-matching QC loop.
- **`make_restart_deck`**: history→prediction workflow via OPM's `rst_deck`,
  including an automatic `UNIFIN` patch when restarting from unified output.
- **`rft_data`**: RFT pressure-vs-depth surveys per well/date (1-based i/j/k).
- **`list_cases`**: scan a directory tree for decks/results to open a session.
- **MPI runs**: `run_flow(mpi_processes=N)` → `mpirun -np N flow`.
- **Inline images**: `plot_summary`, `plot_property_map` and
  `compare_to_observed` return the PNG as MCP image content, so charts render
  directly in the chat.

### Added (engineering)

- **Native-Linux backend**: on a POSIX host the server runs OPM binaries
  directly (no `wsl.exe`), so the same package serves Linux users; path
  translation degrades to pass-through.
- **Bounded concurrency**: at most `OPM_MCP_MAX_PARALLEL` (default 2) flow jobs
  run at once; extra submissions queue (`state="queued"`, with
  `queue_position`) and launch automatically, including after a server restart.
- **Job provenance**: `job.json` records the flow version and the SHA-256 of
  the submitted deck.
- **Uniform error contract**: every tool failure returns structured
  `{"ok": false, "error", "error_type"}` instead of a raw traceback.
- CI: ruff linting + POSIX job-manager integration tests against a stub flow
  binary; release workflow (PyPI trusted publishing) and MCP-registry
  `server.json`.

### Fixed

- **PV-weighted averages on real field models**: `PORV` is global-size in INIT
  files; it is now compressed to active cells via the grid index map instead of
  silently disabling weighting on any grid with inactive cells.
- Mount-root detection no longer caches the `/mnt/` fallback when the `wslpath`
  probe fails (e.g. distro still booting).
- `flow_jobs` batches PID-liveness checks into one WSL round-trip.
- Job rehydration is lazy (first use) instead of an import side effect.

### Added (analytics & plotting, 2026-06-07 batch)

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

### Fixed (2026-06-07 batch)

- Replaced deprecated `datetime.utcfromtimestamp` (warned/removed on 3.12+).
- Example deck now requests `INIT`, so `grid_info` static-property stats and
  PV-weighted `restart_property` averages work; it also requests RFT output
  (`WRFTPLT`) so `rft_data` can be exercised out of the box.

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
