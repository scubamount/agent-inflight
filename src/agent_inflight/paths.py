"""Where things live. One resolver so every command agrees on the file."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def home() -> Path:
    """Directory that holds inflight.md.

    INFLIGHT_HOME wins. Otherwise a Hermes install ($HERMES_HOME, then
    ~/.hermes) is used when present, so Hermes users need no config. Everyone
    else gets ~/.agent-inflight.
    """
    if os.environ.get("INFLIGHT_HOME"):
        return Path(os.environ["INFLIGHT_HOME"]).expanduser()
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser()
    hermes = Path.home() / ".hermes"
    if hermes.is_dir():
        return hermes
    return Path.home() / ".agent-inflight"


def inflight_file() -> Path:
    if os.environ.get("INFLIGHT_FILE"):
        return Path(os.environ["INFLIGHT_FILE"]).expanduser()
    return home() / "inflight.md"


def canonical(path: str) -> str:
    """`path` with symlinks resolved and, on macOS, each component in its
    on-disk case. APFS and HFS+ are case-insensitive by default, so
    `~/Code/x` and `~/code/x` are one repo; realpath keeps whatever case it
    was given, and the same repo was recorded (and audited) twice. Falls back
    to realpath when the path can't be opened."""
    real = os.path.realpath(os.path.expanduser(path))
    if sys.platform != "darwin":
        return real
    try:
        import fcntl
        fd = os.open(real, os.O_RDONLY)
        try:
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        finally:
            os.close(fd)
        return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape") or real
    except (OSError, AttributeError, ValueError):
        return real


def archive_dir(for_file: Path) -> Path:
    return for_file.parent / "inflight-archive"


def _env(var: str) -> str:
    return os.environ.get(var, "").strip()


def _ps_table() -> "dict[int, tuple[int, str]]":
    """pid -> (ppid, args) for every process, from one `ps` call. {} on failure."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True,
                             text=True, timeout=2, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


_SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "-sh", "-bash", "-zsh"}


def _is_hermes(args: str) -> bool:
    # A shell's -c string can mention ~/.hermes, so shells never count. Any
    # other process whose command line names hermes is the Hermes agent
    # (the `hermes` launcher or its python runtime).
    argv0 = os.path.basename(args.split(None, 1)[0]) if args.strip() else ""
    return argv0 not in _SHELLS and "hermes" in args.lower()


def _claude_is_innermost(claude_pid: str) -> bool:
    """Both harness ids are set: one harness runs inside the other and the
    outer one's id leaked in through the environment. The nearer ancestor
    owns this process. Walk up from our parent: reaching CLAUDE_PID before
    any Hermes process means Claude Code is innermost."""
    if not claude_pid.isdigit():
        return False
    table, pid = _ps_table(), os.getppid()
    for _ in range(64):
        if pid == int(claude_pid):
            return True
        row = table.get(pid)
        if row is None or pid <= 1:
            return False
        if _is_hermes(row[1]):
            return False
        pid = row[0]
    return False


def session_id() -> str:
    """This session's id, if the harness exports one.

    INFLIGHT_SESSION_ID is an explicit override. HERMES_SESSION_ID and
    CLAUDE_CODE_SESSION_ID are inherited by child processes, so a Claude Code
    launched from a Hermes terminal sees both. If both are set, the harness
    that is the nearest ancestor wins."""
    # CLAUDE_CODE_SESSION_ID / CLAUDE_PID: set by Claude Code in Bash-tool,
    # hook and MCP subprocesses (https://code.claude.com/docs/en/env-vars).
    override, hermes, claude = _env("INFLIGHT_SESSION_ID"), _env("HERMES_SESSION_ID"), _env("CLAUDE_CODE_SESSION_ID")
    if override:
        return override
    if hermes and claude:
        return claude if _claude_is_innermost(_env("CLAUDE_PID")) else hermes
    return hermes or claude


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
