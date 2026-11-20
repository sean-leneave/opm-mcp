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

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence

from .config import SETTINGS

_ON_WINDOWS = os.name == "nt"

_DRIVE_LETTER_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/]([^\\/]+)[\\/](.*)$", re.IGNORECASE)

# WSL's DrvFs mount root for Windows drives. Default is ``/mnt/`` but users can
# set ``automount.root=/`` in /etc/wsl.conf (giving ``/c/...``). Detected once
# via ``wslpath`` and cached so we don't hardcode the wrong prefix.
_mount_prefix_cache: str | None = None


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


def _mount_prefix() -> str:
    """Return the WSL mount root for Windows drives, e.g. ``/mnt/`` or ``/``.

    Detected via ``wslpath -u 'C:/'`` and cached **only on success** — if the
    probe fails (distro still booting, WSL unavailable) we fall back to
    ``/mnt/`` for this call but leave the cache empty so a later call can still
    pick up a non-default ``automount.root``.
    """
    global _mount_prefix_cache
    if _mount_prefix_cache is not None:
        return _mount_prefix_cache
    if not _ON_WINDOWS:
        # Drive-letter paths don't occur on a POSIX host; value is moot.
        _mount_prefix_cache = "/mnt/"
        return _mount_prefix_cache
    try:
        res = run(["wslpath", "-u", "C:/"], check=False, timeout=10)
        out = res.stdout.strip()
        m = re.match(r"^(/.*?/)c/?$", out) or re.match(r"^(/)c/?$", out)
        if m:
            _mount_prefix_cache = m.group(1)
            return _mount_prefix_cache
    except Exception:
        pass
    return "/mnt/"


def _wsl_mnt_re() -> re.Pattern[str]:
    root = re.escape(_mount_prefix().rstrip("/"))
    return re.compile(rf"^{root}/([a-z])/(.*)$")


def to_wsl_path(path: str | Path) -> str:
    """Convert a Windows path to its equivalent inside the WSL distro.

    - ``C:\\Users\\me`` → ``/mnt/c/Users/me`` (or the distro's mount root)
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
        return f"{_mount_prefix()}{letter}/{rest}"
    # Looks already-POSIX.
    if raw.startswith("/"):
        return raw
    # Bare relative path. On Windows resolve against CWD then re-translate the
    # drive path; on a POSIX host it is already a native path.
    if _ON_WINDOWS:
        return to_wsl_path(str(PureWindowsPath(Path(raw).resolve())))
    return str(Path(raw).resolve())


def to_windows_path(path: str) -> str:
    """Convert a WSL path back to its Windows equivalent (best effort).

    On a non-Windows host (e.g. CI, or a future native-Linux backend) there is
    nothing to translate — a ``/`` path is already local — so we pass it through.
    """
    raw = str(path).strip()
    if not _ON_WINDOWS:
        return raw
    mnt = _wsl_mnt_re().match(raw)
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
    """Run a command synchronously on the Linux side (WSL on Windows, direct on POSIX)."""
    cmd, sub_env, sub_cwd = _build_command(argv, cwd=cwd, env=env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=sub_env,
            cwd=sub_cwd,
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
    merge_stderr: bool = False,
) -> subprocess.Popen:
    """Spawn a background WSL process, optionally redirecting output to files.

    ``merge_stderr=True`` folds stderr into the stdout stream (the moral
    equivalent of ``2>&1``), so a single capture file holds both — no second
    file handle fighting over the same offset.
    """
    cmd, sub_env, sub_cwd = _build_command(argv, cwd=cwd, env=env)
    stdout = open(stdout_path, "wb") if stdout_path else subprocess.PIPE
    if merge_stderr:
        stderr: Any = subprocess.STDOUT
    else:
        stderr = open(stderr_path, "wb") if stderr_path else subprocess.PIPE
    creationflags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        return subprocess.Popen(
            cmd,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            # POSIX: run in its own session so a simulation is not killed when
            # the MCP client hangs up and the server's process group receives
            # SIGHUP/SIGTERM. (Windows uses CREATE_NEW_PROCESS_GROUP above.)
            start_new_session=not _ON_WINDOWS,
            env=sub_env,
            cwd=sub_cwd,
        )
    finally:
        # The child holds its own copies of the log file descriptors; close the
        # parent's so long sessions do not leak one handle per job.
        for fh in (stdout, stderr):
            if hasattr(fh, "close"):
                fh.close()


def wsl_pid_alive(pid: int) -> bool:
    """Return True if a Linux PID is still alive (distro on Windows, local on POSIX)."""
    if not _ON_WINDOWS:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    res = run(
        ["bash", "-lc", f"kill -0 {int(pid)} 2>/dev/null && echo 1 || echo 0"],
        check=False,
        timeout=10,
    )
    return res.stdout.strip().endswith("1")


def wsl_pids_alive(pids: Sequence[int]) -> dict[int, bool]:
    """Liveness for many PIDs in ONE round-trip.

    Each ``wsl.exe`` invocation costs ~200 ms of VM hand-off, so checking five
    inherited jobs one-by-one makes ``flow_jobs`` sluggish — batch them.
    """
    pids = [int(p) for p in pids]
    if not pids:
        return {}
    if not _ON_WINDOWS:
        return {p: wsl_pid_alive(p) for p in pids}
    loop = "; ".join(f"kill -0 {p} 2>/dev/null && echo {p}:1 || echo {p}:0" for p in pids)
    res = run(["bash", "-lc", loop], check=False, timeout=15)
    out: dict[int, bool] = {p: False for p in pids}
    for line in res.stdout.splitlines():
        if ":" in line:
            pid_s, _, flag = line.strip().partition(":")
            try:
                out[int(pid_s)] = flag == "1"
            except ValueError:
                continue
    return out


_BOOT_ID = "/proc/sys/kernel/random/boot_id"


def _local_identity(pid: int) -> str | None:
    """``<boot_id>:<start_time>`` for a local Linux PID, or None if it does not exist."""
    try:
        boot = Path(_BOOT_ID).read_text().strip()
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces; everything after the last ")" starts at field 3,
    # so field 22 (starttime, clock ticks since boot) is index 19.
    fields = stat.rsplit(")", 1)[1].split()
    return f"{boot}:{fields[19]}" if len(fields) > 19 else None


def pid_identities_supported() -> bool:
    """True when process identities can be read (Linux /proc, locally or in WSL)."""
    return _ON_WINDOWS or Path(_BOOT_ID).exists()


def wsl_pids_identity(pids: Sequence[int]) -> dict[int, str | None]:
    """Identity (``<boot_id>:<start_time>``) of each Linux PID; None for PIDs that do not exist.

    A PID alone is not a process: after a reboot, or once a PID wraps, the same number
    belongs to an unrelated process. Boot id plus start time is unique, so a recorded
    identity tells a still-running Flow from a stranger that inherited its PID.
    Returns ``{}`` where identities are unsupported (non-Linux POSIX hosts).
    """
    pids = [int(p) for p in pids]
    if not pids or not pid_identities_supported():
        return {}
    if not _ON_WINDOWS:
        return {p: _local_identity(p) for p in pids}
    script = (
        f"b=$(cat {_BOOT_ID}); for p in {' '.join(map(str, pids))}; do "
        "if [ -r /proc/$p/stat ]; then "
        "s=$(sed -E 's/^.*\\) //' /proc/$p/stat | cut -d' ' -f20); echo \"$p:$b:$s\"; "
        "else echo \"$p:\"; fi; done"
    )
    res = run(["bash", "-c", script], check=False, timeout=15)
    out: dict[int, str | None] = {p: None for p in pids}
    for line in res.stdout.splitlines():
        pid_s, _, ident = line.strip().partition(":")
        try:
            out[int(pid_s)] = ident or None
        except ValueError:
            continue
    return out


def wsl_kill(pid: int, *, force: bool = False) -> bool:
    """Send SIGTERM (or SIGKILL) to a Linux PID (distro on Windows, local on POSIX)."""
    if not _ON_WINDOWS:
        import signal as _signal

        try:
            os.kill(int(pid), _signal.SIGKILL if force else _signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    sig = "-9" if force else "-15"
    res = run(
        ["bash", "-lc", f"kill {sig} {int(pid)} 2>/dev/null && echo 1 || echo 0"],
        check=False,
        timeout=10,
    )
    return res.stdout.strip().endswith("1")


def _build_command(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str] | None,
) -> tuple[list[str], dict[str, str] | None, str | None]:
    """Build ``(cmd, subprocess_env, subprocess_cwd)`` for this host.

    On Windows the command is wrapped through ``wsl.exe`` with env/cwd baked
    into the command line (subprocess env/cwd would apply to wsl.exe itself,
    not the Linux child). On a POSIX host the binaries are local, so we run
    them directly and let ``subprocess`` handle env/cwd — the same server
    codebase then works on native Linux with no WSL involved.
    """
    if not argv:
        raise ValueError("argv must contain at least the program name")
    if not _ON_WINDOWS:
        sub_env: dict[str, str] | None = None
        if env:
            sub_env = dict(os.environ)
            sub_env.update(env)
        return list(argv), sub_env, (str(cwd) if cwd else None)
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
    return cmd, None, None


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
    info: dict[str, str] = {}
    if _ON_WINDOWS:
        info["mode"] = "windows + WSL"
        info["wsl_distro"] = SETTINGS.wsl_distro
        try:
            info["wsl_exe"] = _wsl_exe()
        except WSLError as exc:
            info["wsl_exe"] = f"ERROR: {exc}"
            return info
    else:
        info["mode"] = "native linux (no WSL)"
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
