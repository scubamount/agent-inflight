"""Per-session state written by `inflight hook`, and the hooks audit log.

Layout (next to the tracker, never inside a harness's own folders):

  <tracker dir>/inflight-state/            0700
      sessions/<session-id>.json           0600  heartbeat, ended flag, repos
      config.json                          0600  plugin allowlist
      venv/                                     pinned interpreter (install.sh)
      interpreter                               path install.sh pinned
  <tracker dir>/inflight-archive/hooks.log 0600  one JSON line per action

`<tracker dir>/sessions/` is NOT used: under Hermes that is the harness's
own transcript folder.

Session ids are untrusted input (they arrive on stdin from a harness). They
are checked against the tag charset before they become a file name, so an id
can't contain `/` or point outside sessions/.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import paths, safety

SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # same charset as core.TAG_RE ids


def valid_sid(sid: Any) -> bool:
    return isinstance(sid, str) and bool(SID_RE.match(sid)) and ".." not in sid


def state_dir() -> Path:
    return paths.inflight_file().expanduser().parent / "inflight-state"


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def session_path(sid: str) -> Path:
    if not valid_sid(sid):
        raise ValueError("invalid session id")
    return sessions_dir() / f"{sid}.json"


def load(sid: str) -> Dict[str, Any]:
    try:
        data = json.loads(session_path(sid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(sid: str, **changes: Any) -> Dict[str, Any]:
    """Read-modify-write one session's state under its lock. `repo=` adds to
    the touched-repo set; `warned=` adds to the warned-collision set; any other
    key is set as-is."""
    p = session_path(sid)
    safety.ensure_private_dir(state_dir())
    safety.ensure_private_dir(p.parent)
    with safety.locked(p, timeout=1.0):
        data = load(sid)
        now = time.time()
        data.setdefault("session", sid)
        data.setdefault("started_at", now)
        data["heartbeat_at"] = now
        repo = changes.pop("repo", None)
        if repo:
            repos = data.setdefault("repos", {})
            repos[repo] = now
        warned = changes.pop("warned", None)
        if warned:
            w = data.setdefault("warned", [])
            if warned not in w:
                w.append(warned)
        data.update(changes)
        safety.write_private(p, json.dumps(data, indent=1, sort_keys=True) + "\n")
    return data


def recent(within_s: float) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Sessions whose state file changed within `within_s` (stat only, then load)."""
    d = sessions_dir()
    cutoff = time.time() - within_s
    try:
        it = list(os.scandir(d))
    except OSError:
        return
    for ent in it:
        if not ent.name.endswith(".json"):
            continue
        try:
            if ent.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        sid = ent.name[:-5]
        if valid_sid(sid):
            yield sid, load(sid)


def repo_root(cwd: Any) -> Optional[str]:
    """Nearest ancestor of `cwd` holding `.git` (dir or file). Filesystem only:
    no git process runs here, so a hostile repo config can't execute."""
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        p = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return str(d)
    return None


def hooks_log() -> Path:
    return paths.archive_dir(paths.inflight_file().expanduser()) / "hooks.log"


_LOG_KEYS = ("event", "session", "action", "entry_id", "kinds", "plugin", "error")


def log(action: str, **fields: Any) -> None:
    """Append one JSON line. Only whitelisted keys are written, so tool
    arguments, file contents or secret values can't reach the log by accident.
    Never raises."""
    try:
        rec: Dict[str, Any] = {"ts": round(time.time(), 3), "action": action}
        for k in _LOG_KEYS:
            v = fields.get(k)
            if v not in (None, "", []):
                rec[k] = v if isinstance(v, (int, float, list)) else str(v)[:200]
        if "session" in rec and not valid_sid(rec["session"]):
            rec["session"] = "<invalid>"
        p = hooks_log()
        safety.ensure_private_dir(p.parent)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, safety.FILE_MODE)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # the log must never break a hook or a command
        pass


def read_log() -> List[Dict[str, Any]]:
    try:
        lines = hooks_log().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
