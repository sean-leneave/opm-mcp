# Hardening in this fork (sean-leneave/opm-mcp)

Upstream: [ojaogezi/opm-mcp](https://github.com/ojaogezi/opm-mcp), forked at `9ea44f0` after a static security review. This file lists what the fork changes and why. Priorities, in order: security, accuracy, repeatability, durability.

| # | Change | Where | Test |
| --- | --- | --- | --- |
| 1 | **Path confinement.** Every tool argument that names a file (`deck_path`, `case_path`, `output_dir`, ...) must resolve, after symlinks and `..`, inside `OPM_MCP_ROOTS`. Deck INCLUDEs outside the roots are reported, never read. Default roots: the work dir only. | `guard.py`, `server.safe_tool`, `tools/decktext.py` | `tests/test_guard.py` |
| 2 | **Flow option policy.** `extra_args` must be single `--name[=value]` tokens on an allow-list; `--output-dir`, `--parameter-file` and similar are never accepted; values may not contain paths. `OPM_MCP_MAX_CORES` caps processes x threads. | `guard.py`, `jobs.start_flow_job` | `tests/test_guard.py`, `tests/test_jobs_hardening.py` |
| 3 | **Detached runs.** On POSIX, Flow starts in its own session, so a client disconnect or server crash does not kill a simulation. | `wsl.popen` | `test_flow_runs_in_its_own_session` |
| 4 | **Fixed run environment.** Flow runs under `env -i` with an explicit `PATH`, `HOME`, `LANG`, `LC_ALL` (and `USER`) via a non-login bash. The environment is recorded per job as `run_env`. No `OMP_NUM_THREADS`: threads go in argv. | `jobs._launch_locked` | `test_flow_sees_only_the_recorded_environment` |
| 5 | **Locked dependencies.** `requirements.lock` and `requirements-dev.lock` pin every package with SHA-256 hashes (universal, Python 3.12). | repository root | install with `--require-hashes` |
| 6 | Parent process closes its copies of job log handles (no leak in long sessions). | `wsl.popen` | — |
| + | **Opt-in for passthrough tools.** `run_upscaling` (unchecked arguments) and `resinsight_snapshot(extra_commands=...)` (raw ResInsight commands) are off unless `OPM_MCP_ENABLE_UPSCALING=1` / `OPM_MCP_ALLOW_RESINSIGHT_COMMANDS=1`. | `server.py` | — |
| + | **Env file.** Settings load from `~/.config/opm-mcp/env` (or `$OPM_MCP_ENV_FILE`); only `OPM_MCP_*` keys; the process environment wins. | `config.py` | `tests/test_config_envfile.py` |
| + | **Publishing.** With `OPM_MCP_PUBLISH` set, a completed job's case files, `flow.log` and `job.json` are copied to `<publish>/<case>-<job_id>/`. | `jobs._publish` | `test_completed_job_is_published` |

Upstream tests are unchanged except `tests/test_jobs_stub.py`, whose fixture now sets `OPM_MCP_ROOTS` (its decks live in `tmp_path`). `tests/conftest.py` keeps a developer's real env file out of tests.

## Install (inside the VM or WSL2 only)

```
python3 -m venv ~/opm-mcp/.venv
~/opm-mcp/.venv/bin/pip install --require-hashes -r requirements.lock
~/opm-mcp/.venv/bin/pip install --no-deps -e .
~/opm-mcp/.venv/bin/pip install --require-hashes -r requirements-dev.lock
~/opm-mcp/.venv/bin/pytest -q
```

## Known limits

- **Flow and `opmpack` follow INCLUDE paths themselves.** A malicious deck can make the simulator read a file anywhere the VM user can read. Mitigation: run only in the VM / WSL2, whose user holds no secrets; review decks from outside sources.
- **`resdata` 7.0.0 is locked but new** (released 2026-11-19; the first version with Linux ARM wheels, which the Mac VM needs). It is accepted only after the reader tests and acceptance test T7 pass. If it fails them, pin `resdata<7` on x86 and add an OPM-reader backend for ARM.
- **MCP SDK is still `<2`**; the 2.0 port is a separate, test-gated change.
- **Untested on Windows.** The fixes keep upstream's Windows paths, but the Windows acceptance run (Phase W2) is still to come.

## Status

Written 2026-11-19. Not yet executed: the first test run happens in the Ubuntu VM. Do not merge to `main` until the suite passes there.
