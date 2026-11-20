"""Runtime configuration for the OPM MCP server.

Defaults are tuned for a Windows host running OPM in a WSL2 Ubuntu distro.
Each setting can be overridden by an environment variable so the server can be
re-pointed at a different distro or different binary paths without code changes.

Settings may also come from an env file (``KEY=VALUE`` lines), read once at
import: ``$OPM_MCP_ENV_FILE`` if set, else ``~/.config/opm-mcp/env``. Values
already present in the process environment win over the file, so a launch
command can still override any single setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_ENV_FILE = Path.home() / ".config" / "opm-mcp" / "env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. ``#`` starts a comment (whole line, or after whitespace).

    Only ``OPM_MCP_*`` keys are accepted, so the file cannot alter unrelated
    process settings (PATH, LD_PRELOAD, ...).
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip an inline comment: a '#' preceded by whitespace.
        for i, ch in enumerate(value):
            if ch == "#" and (i == 0 or value[i - 1].isspace()):
                value = value[:i]
                break
        value = value.strip().strip('"').strip("'")
        if key.startswith("OPM_MCP_") and key.isidentifier():
            out[key] = value
    return out


def load_env_file() -> Path | None:
    """Merge the env file into ``os.environ`` without overriding existing values."""
    path = Path(os.environ.get("OPM_MCP_ENV_FILE") or _DEFAULT_ENV_FILE).expanduser()
    values = _parse_env_file(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return path if values else None


ENV_FILE_LOADED = load_env_file()


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _split_paths(value: str) -> tuple[Path, ...]:
    """Split a root list on ``os.pathsep`` (``:`` on Linux, ``;`` on Windows)."""
    return tuple(Path(p).expanduser() for p in value.split(os.pathsep) if p.strip())


@dataclass(frozen=True)
class Settings:
    wsl_distro: str
    flow_bin: str
    resinsight_bin: str
    work_dir: Path
    flow_default_threads: int
    job_history_size: int
    log_tail_default: int
    max_parallel: int
    # --- hardening (sean-leneave fork) ---
    roots: tuple[Path, ...]  # every path a tool touches must resolve inside one of these
    publish_dir: Path | None  # finished case files are copied here (None = off)
    max_cores: int  # refuse runs where mpi_processes x threads exceeds this (0 = no limit)
    flow_extra_allow: tuple[str, ...]  # additional allowed flow options (exact names)
    enable_upscaling: bool  # run_upscaling passes arbitrary args: off unless enabled
    allow_resinsight_commands: bool  # raw ResInsight command-file lines: off unless enabled

    @classmethod
    def from_env(cls) -> "Settings":
        work = Path(_env("OPM_MCP_WORK_DIR", str(Path.home() / ".opm-mcp")))
        work.mkdir(parents=True, exist_ok=True)
        (work / "jobs").mkdir(exist_ok=True)
        (work / "artifacts").mkdir(exist_ok=True)
        publish_raw = os.environ.get("OPM_MCP_PUBLISH", "").strip()
        publish = Path(publish_raw).expanduser() if publish_raw else None
        roots = _split_paths(os.environ.get("OPM_MCP_ROOTS", ""))
        # Secure default: with no roots configured, only the work dir is reachable.
        roots = roots or (work,)
        # The work dir and the publish dir are always inside the sandbox.
        extra = [work] + ([publish] if publish else [])
        roots = tuple(dict.fromkeys([*roots, *extra]))
        allow = tuple(
            a.strip() for a in os.environ.get("OPM_MCP_FLOW_EXTRA_ALLOW", "").split(",") if a.strip()
        )
        return cls(
            wsl_distro=_env("OPM_MCP_WSL_DISTRO", "Ubuntu-24.04"),
            flow_bin=_env("OPM_MCP_FLOW_BIN", "flow"),
            resinsight_bin=_env("OPM_MCP_RESINSIGHT_BIN", "ResInsight"),
            work_dir=work,
            flow_default_threads=int(_env("OPM_MCP_FLOW_THREADS", "0")),
            job_history_size=int(_env("OPM_MCP_JOB_HISTORY", "50")),
            log_tail_default=int(_env("OPM_MCP_LOG_TAIL", "200")),
            max_parallel=max(1, int(_env("OPM_MCP_MAX_PARALLEL", "2"))),
            roots=roots,
            publish_dir=publish,
            max_cores=max(0, int(_env("OPM_MCP_MAX_CORES", "0"))),
            flow_extra_allow=allow,
            enable_upscaling=_bool("OPM_MCP_ENABLE_UPSCALING"),
            allow_resinsight_commands=_bool("OPM_MCP_ALLOW_RESINSIGHT_COMMANDS"),
        )


SETTINGS = Settings.from_env()
