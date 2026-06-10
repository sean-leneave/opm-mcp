"""Runtime configuration for the OPM MCP server.

Defaults are tuned for a Windows host running OPM in a WSL2 Ubuntu distro.
Each setting can be overridden by an environment variable so the server can be
re-pointed at a different distro or different binary paths without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


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

    @classmethod
    def from_env(cls) -> "Settings":
        work = Path(_env("OPM_MCP_WORK_DIR", str(Path.home() / ".opm-mcp")))
        work.mkdir(parents=True, exist_ok=True)
        (work / "jobs").mkdir(exist_ok=True)
        (work / "artifacts").mkdir(exist_ok=True)
        return cls(
            wsl_distro=_env("OPM_MCP_WSL_DISTRO", "Ubuntu-24.04"),
            flow_bin=_env("OPM_MCP_FLOW_BIN", "flow"),
            resinsight_bin=_env("OPM_MCP_RESINSIGHT_BIN", "ResInsight"),
            work_dir=work,
            flow_default_threads=int(_env("OPM_MCP_FLOW_THREADS", "0")),
            job_history_size=int(_env("OPM_MCP_JOB_HISTORY", "50")),
            log_tail_default=int(_env("OPM_MCP_LOG_TAIL", "200")),
            max_parallel=max(1, int(_env("OPM_MCP_MAX_PARALLEL", "2"))),
        )


SETTINGS = Settings.from_env()
