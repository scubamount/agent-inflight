"""Where things live. One resolver so every command agrees on the file."""
from __future__ import annotations

import os
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


def archive_dir(for_file: Path) -> Path:
    return for_file.parent / "inflight-archive"


def session_id() -> str:
    """This session's id, if the harness exports one."""
    for var in ("INFLIGHT_SESSION_ID", "HERMES_SESSION_ID", "CLAUDE_SESSION_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
