"""WSL bridge — translate paths and run Linux binaries from Windows.

OPM Flow, ResInsight, and the OPM utility binaries are Linux-only on this host;
they live in an Ubuntu-24.04 distro under WSL2. The MCP server itself runs on
the Windows side so Claude Code can launch it natively. Every call into OPM
land therefore needs:

1. **Path translation.** Caller hands us Windows-style paths
   (``C:\\Users\\me\\case.DATA``) which Linux binaries cannot read directly.
   We rewrite them to ``/mnt/c/Users/me/case.DATA`` before execution. Already-
   Linux paths (``/home/me/case.DATA``) are passed through untouched.

2. **Command marshalling.** We shell out via ``wsl.exe -d <distro> --`` and
   capture stdout/stderr. Long-running jobs (e.g. ``flow``) need ``Popen`` so we
   can return immediately and let :mod:`opm_mcp.jobs` track them.

3. **Reverse translation.** Result-file paths reported by Linux tools come back
   as ``/mnt/c/...`` and need to be folded back into ``C:\\...`` so downstream
   Windows code (``resdata`` parsers) can open them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Mapping, Sequence

from .config import SETTINGS

_DRIVE_LETTER_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_MNT_RE = re.compile(r"^/mnt/([a-z])/(.*)$")
_WSL_UNC_RE = re.compile(r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/]([^\\/]+)[\\/](.*)$", re.IGNORECASE)


class WSLError(RuntimeError):
    """Raised when wsl.exe is unavailable or returns a non-zero exit code."""


@dataclass
class WSLResult:
    returncode: int
    stdout: str
    stderr: str

    def check(self, cmd: Sequence[str]) -> "WSLResult":
        if self.returncode != 0:
            raise WSLError(
                f"WSL command failed (exit {self.returncode}): "
                f"{' '.join(cmd)}\nstderr:\n{self.stderr.strip()}"
            )
        return self


def _wsl_exe() -> str:
    exe = shutil.which("wsl.exe") or shutil.which("wsl")
    if not exe:
        raise WSLError(
            "wsl.exe not found on PATH. Install WSL2 and the Ubuntu-24.04 "
            "distro, or set OPM_MCP_WSL_DISTRO to a distro that exists."
        )
    return exe


def to_wsl_path(path: str | Path) -> str:
    """Convert a Windows path to its equivalent inside the WSL distro.

    - ``C:\\Users\\me`` → ``/mnt/c/Users/me``
    - ``\\\\wsl$\\Ubuntu-24.04\\home\\me`` → ``/home/me``
    - Already-Linux paths (``/foo/bar``) pass through unchanged.
    """
    raw = str(path).strip()
    if not raw:
        return raw
    # UNC form pointing at a WSL share -> strip the host segment.
    unc = _WSL_UNC_RE.match(raw)
    if unc:
        return "/" + unc.group(2).replace("\\", "/")
    drive = _DRIVE_LETTER_RE.match(raw)
    if drive:
        letter, rest = drive.group(1).lower(), drive.group(2).replace("\\", "/")
        return f"/mnt/{letter}/{rest}"
    # Looks already-POSIX.
    if raw.startswith("/"):
        return raw
    # Bare relative path (e.g. "deck.DATA") — resolve against CWD on Windows
    # and recurse, so the WSL side sees an absolute path.
    abs_path = str(PureWindowsPath(Path(raw).resolve()))
    return to_wsl_path(abs_path)


def to_windows_path(path: str) -> str:
    """Convert a WSL path back to its Windows equivalent (best effort)."""
    raw = str(path).strip()
    mnt = _WSL_MNT_RE.match(raw)
    if mnt:
        letter, rest = mnt.group(1).upper(), mnt.group(2).replace("/", "\\")
        return f"{letter}:\\{rest}"
    if raw.startswith("/"):
        # Lives inside the distro filesystem — Windows can see it via UNC.
        return f"\\\\wsl.localhost\\{SETTINGS.wsl_distro}{raw.replace('/', chr(92))}"
    return raw


def wsl_dir_for(path: str | Path) -> str:
    """Return the WSL-side directory containing ``path``."""
    wsl = to_wsl_path(path)
    # Use a POSIX-style dirname so we don't accidentally drop the leading /.
    if "/" not in wsl.rstrip("/"):
        return "/"
    return wsl.rsplit("/", 1)[0] or "/"


def run(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> WSLResult:
    """Run a command synchronously inside the configured WSL distro."""
    cmd = _build_command(argv, cwd=cwd, env=env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise WSLError(str(exc)) from exc
    result = WSLResult(proc.returncode, proc.stdout, proc.stderr)
    return result.check(cmd) if check else result


def popen(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdout_path: str | Path | None = None,
    stderr_path: str | Path | None = None,
) -> subprocess.Popen:
    """Spawn a background WSL process, optionally redirecting output to files."""
    cmd = _build_command(argv, cwd=cwd, env=env)
    stdout = open(stdout_path, "wb") if stdout_path else subprocess.PIPE
    stderr = open(stderr_path, "wb") if stderr_path else subprocess.PIPE
    creationflags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _build_command(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
) -> list[str]:
    if not argv:
        raise ValueError("argv must contain at least the program name")
    exe = _wsl_exe()
    cmd = [exe, "-d", SETTINGS.wsl_distro, "--"]
    if env:
        # Inject env vars via a tiny `env` prefix so the child binary picks them up.
        env_parts = ["env"] + [f"{k}={_sh_escape(v)}" for k, v in env.items()]
        cmd.extend(env_parts)
    if cwd:
        wsl_cwd = to_wsl_path(cwd)
        # Use `bash -lc` to honor cd + the rest of argv as a single shell line.
        shell_line = f"cd {_sh_escape(wsl_cwd)} && exec {_shell_join(argv)}"
        cmd.extend(["bash", "-lc", shell_line])
    else:
        cmd.extend(list(argv))
    return cmd


def _sh_escape(value: str) -> str:
    if not value:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _shell_join(parts: Iterable[str]) -> str:
    return " ".join(_sh_escape(p) for p in parts)


def check_environment() -> dict[str, str]:
    """Quick health check used by the ``opm_environment`` MCP tool."""
    info: dict[str, str] = {"wsl_distro": SETTINGS.wsl_distro}
    try:
        info["wsl_exe"] = _wsl_exe()
    except WSLError as exc:
        info["wsl_exe"] = f"ERROR: {exc}"
        return info
    # `flow --version` is cheap; ResInsight's --version spins up Qt/GL and
    # can hang for >20s in headless WSL, so probe it via dpkg + `command -v`
    # instead. Snapshots/logs still work when actually invoked (optionally
    # with `force_software_gl=True`).
    probes: tuple[tuple[str, list[str]], ...] = (
        ("flow", [SETTINGS.flow_bin, "--version"]),
        (
            "ResInsight",
            [
                "bash",
                "-lc",
                f"command -v {SETTINGS.resinsight_bin} >/dev/null "
                f'&& (dpkg -s resinsight 2>/dev/null | sed -n "s/^Version: //p" '
                f'| head -1 | xargs -I{{}} echo "resinsight {{}} (installed)") '
                f'|| echo "ResInsight not on PATH"',
            ],
        ),
        ("python3-opm-common", ["python3", "-c", "import opm.io.parser; print('ok')"]),
    )
    for label, argv in probes:
        try:
            res = run(argv, check=False, timeout=15)
            line = (res.stdout or res.stderr).strip().splitlines()
            info[label] = line[0] if line else f"exit {res.returncode}"
        except (WSLError, subprocess.TimeoutExpired) as exc:
            info[label] = f"ERROR: {exc}"
    return info
