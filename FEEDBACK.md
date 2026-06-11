# Field feedback — GASLIFT-12 validation session (2026-06-11)

Source: a real Claude Code session driving this server end-to-end on
`examples/Gaslift/GASLIFT-12.DATA` (validate → run → 2 sensitivity runs →
7 diagnostic plots → convergence/root-cause analysis). Of ~25 `mcp__opm__*`
calls, **one** failed outright; the rest of the friction was adjacent to the
tools — path dialects, schema typing, and missing deck-text access. This file
records each issue, its root cause, and the fix (patched in this change-set
where marked ✅).

## Worked well — keep

- Background job model (`run_flow` → `flow_status` → `flow_log_tail`): the
  aborted `--enable-tuning=true` run surfaced `state: "failed"` plus the
  simulator's own error string with zero extra digging.
- `convergence_info="steps,iterations"` by default: `flow_convergence_report`
  found the day-139.8 timestep collapse without a rerun.
- Inline PNG plots, wildcard key expansion, `read_summary` downsampling,
  `compare_summaries` for A/B run deltas.
- Lenient **input** path handling — every tool accepted Windows paths and
  converted internally.

## Issue 1 — `extra_args` rejected a plain string ✅ fixed

```
run_flow(deck_path=..., extra_args="--enable-tuning=true")
→ 1 validation error for run_flowArguments: extra_args
  Input should be a valid list [type=list_type]
```

The published JSON schema for `extra_args` carried no `type`/`items` (only
`"default": null`) in the client's rendering, so the model had no way to know
a list was required. Cost: one failed tool call + retry.

**Fix (✅):** the MCP boundary now accepts `list[str] | str` for every
list-of-strings parameter and coerces strings sensibly — `shlex.split` for
CLI-flag params (`extra_args`, `run_upscaling.args`), comma/whitespace split
for vector-key params (`keys`, `keywords`). See `src/opm_mcp/coerce.py`.
Docstrings now state the expected shape explicitly so it survives in clients
that strip param-level schema unions.

## Issue 2 — WSL path dialect leaks into responses ✅ fixed

`validate_deck`/`summarize_deck` echoed back only the converted form
(`"deck_path": "/mnt/c/Users/..."`). The agent then pasted that path into the
host shell (Git Bash, **not** WSL) and got
`No such file or directory`. `run_flow` already did this right
(`output_dir_wsl` *and* `output_dir_win`); deck tools didn't.

**Fix (✅):**
- Deck tools now return `deck_path` (exactly as the caller passed it) plus
  explicit `deck_path_win` and `deck_path_wsl`.
- Server `instructions` now warn: `/mnt/c/...` forms are valid only inside the
  WSL distro; host shells (PowerShell / Git Bash) need the `_win` forms;
  `to_windows_path` / `to_wsl_path` convert.

**Convention going forward:** any tool that reports a path must either use the
Windows form or carry an explicit `_wsl` / `_win` suffix. Never a bare
ambiguous `path` field holding a WSL form.

## Issue 3 — no way to read deck text through the server ✅ fixed

Claude Code's `Read` tool misclassifies `.DATA` as binary ("cannot read binary
files"), and the session needed **eight** Git Bash `grep/sed/head/ls` calls to
inspect LIFTOPT, WLIFTOPT, WCONPROD, GCONPROD, GCONINJE, TUNING, the INCLUDE
tree, and the VFP table axes. That whole deck-QC loop ran outside the server,
host-shell-dependent and fragile.

**Fix (✅):** two new pure-Windows tools (no WSL round-trip — decks are plain
text):
- `deck_text(deck_path, start_line, n_lines, file)` — numbered slice of the
  deck or one of its include files.
- `deck_grep(deck_path, pattern, context, search_includes, max_matches)` —
  regex search across the deck **and its resolved INCLUDEs**, honoring the
  `PATHS` keyword (`$MODEL05`-style aliases), returning file + line numbers +
  context.

## Issue 4 — `summarize_deck` returned wrong title/groups ✅ fixed

For GASLIFT-12 it returned `"title": null` (deck has
`TITLE` → "GASLIFT-12: GAS LIFT OPTIMISATION RUN") and
`"groups": {"count": 0}` (deck has FIELD/PLAT-A/M5S/M5N/B1/G1/C1/F1 via
`GRUPTREE`). For a gas-lift / group-control case the group tree is exactly
what a summary must surface.

Root causes (verified against the opm 2025.10 bindings inside the distro):
- `_title` indexed the `DeckItem` (`item[i]`) — it has no `__getitem__`; the
  title words live in `item.get_data_list()`.
- `_group_names` probed `group_names`/`groups` attributes that don't exist in
  these bindings; groups come from `Schedule._groups(report_step) → [Group]`
  with `.name`.

**Fix (✅):** `helpers/deck_inspect.py` uses `get_data_list()` with a
`get_str(0)` fallback, and `_groups(0)` first with the legacy probes kept as
fallback.

## Issue 5 — `field_summary.n_report_steps` mislabeled ✅ fixed

It returned 80 for a run with 13 report steps — the count is summary
*ministeps* (output timesteps), not report steps. Renamed to
`n_summary_points`.

## Issue 6 — log analysis needed host-side grep ✅ fixed

Finding the well-shut cascade meant grepping `flow.log` by its Windows path
(`grep "will be shut" ...`). `flow_log_tail` only returned the last N lines.

**Fix (✅):** `flow_log_tail(job_id, lines, grep="will be shut|Problem:")`
filters the **whole** log with a case-insensitive regex and returns the last
`lines` matching lines, each prefixed with its line number.

## Not server bugs (for the record)

- `Read` refusing ASCII `.DATA` files is a Claude Code harness issue
  (extension-based misdetection). Issue 3's tools route around it.
- The schema union (`list[str] | None`) being flattened to `{"default": null}`
  appears to happen in the client's deferred-tool rendering, not in FastMCP.
  The Issue-1 coercion makes the server robust to it regardless.
- The GASLIFT-12 case itself collapsing numerically (all 9 wells
  convergence-shut, ±19% FOPT depending on stepping flags) is an OPM Flow
  2025.10 robustness finding, not an MCP issue — kept as a stress case for
  `flow_convergence_report` and log parsing.
