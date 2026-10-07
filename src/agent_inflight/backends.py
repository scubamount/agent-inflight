"""Session backends: who is a session, and is it still working?

A backend answers `lookup(id)` for the session ids it knows. Several can be
active at once; `Chain` asks them in priority order and the first answer wins:

  plugins (allowlisted entry points, priority order of the allowlist)
  hermes     read-only $HERMES_HOME/state.db (+ profiles/*/state.db)
  heartbeat  <tracker dir>/inflight-state/sessions/<id>.json, written by
             `inflight hook` (any harness that runs the hooks)

Backend contract (PLUGIN_API_VERSION 1), duck-typed:
  name: str
  available() -> bool
  lookup(id) -> SessionInfo | dict | None
  optional: lineage(id) -> [ids root..id], children(id) -> [dict], drill(id, profile) -> [str]

Status, when the backend doesn't set `status` itself:
  ENDED if ended_at, else ACTIVE if last activity < active_min, else IDLE.
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import state

PLUGIN_API_VERSION = 1
STATUSES = ("ACTIVE", "IDLE", "ENDED", "UNKNOWN")


@dataclass
class SessionInfo:
    """What a backend knows about one session. All fields optional."""
    status: Optional[str] = None          # one of STATUSES; None = derive from times
    last_activity_at: Optional[float] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    end_reason: Optional[str] = None
    profile: Optional[str] = None
    title: Optional[str] = None
    continued_as: Optional[str] = None    # id that continues this conversation
    resume_cmd: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


def _norm(info: Any) -> Optional[Dict[str, Any]]:
    if info is None:
        return None
    if isinstance(info, SessionInfo):
        return info.as_dict()
    if isinstance(info, dict):
        return dict(info)
    return None


class HermesBackend:
    name = "hermes"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or Path(os.environ.get("HERMES_ROOT") or os.environ.get("HERMES_HOME")
                                 or Path.home() / ".hermes")

    def available(self) -> bool:
        return bool(self.dbs())

    def dbs(self) -> List[Tuple[str, Path]]:
        dbs = [("default", self.root / "state.db")]
        dbs += [(p.parent.name, p) for p in sorted((self.root / "profiles").glob("*/state.db"))]
        return [(n, p) for n, p in dbs if p.is_file()]

    def _one(self, sid: str) -> Optional[dict]:
        for profile, db in self.dbs():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                row = con.execute(
                    "select title, started_at, ended_at, end_reason, last_activity_at "
                    "from sessions where id = ?", (sid,)).fetchone()
                con.close()
            except sqlite3.Error:
                continue
            if row:
                keys = ("title", "started_at", "ended_at", "end_reason", "last_activity_at")
                return {"profile": profile, "_db": db, **dict(zip(keys, row))}
        return None

    # A compression continuation is the row that picks the conversation up when
    # its parent ended by compression: parent.end_reason == 'compression' and
    # the child started at (or after) the parent's end. Subagents spawned
    # BEFORE that end are delegation children, even under a compression parent.
    _CONT_SLACK_S = 1.0

    @staticmethod
    def _rows(db: Path, sql: str, args: tuple) -> list:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                return con.execute(sql, args).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    @classmethod
    def _child(cls, db: Path, sid: str) -> Optional[str]:
        """The compression continuation of `sid`, if any."""
        row = cls._rows(db, "select c.id from sessions c join sessions p on c.parent_session_id = p.id "
                            "where p.id = ? and p.end_reason = 'compression' "
                            "and c.started_at >= coalesce(p.ended_at, 0) - ? "
                            "order by c.started_at desc limit 1", (sid, cls._CONT_SLACK_S))
        return row[0][0] if row else None

    def lineage(self, sid: str, max_hops: int = 50) -> List[str]:
        """Compression ancestors of `sid`, root first, ending with `sid`. A parent
        counts only if it ended by compression and `sid` started at/after that
        end (a continuation); delegation parents are not lineage."""
        info = self._one(sid)
        if not info:
            return [sid]
        db, chain = info["_db"], [sid]
        while len(chain) < max_hops:
            row = self._rows(db, "select p.id from sessions c join sessions p on c.parent_session_id = p.id "
                                 "where c.id = ? and p.end_reason = 'compression' "
                                 "and c.started_at >= coalesce(p.ended_at, 0) - ?", (chain[0], self._CONT_SLACK_S))
            if not row or row[0][0] in chain:
                break
            chain.insert(0, row[0][0])
        return chain

    def family_root(self, sid: str, max_hops: int = 50) -> str:
        """The top of `sid`'s parent chain, through delegation and compression
        parents alike. A subagent and the session that spawned it share a
        root, so one is never warned about the other."""
        info = self._one(sid)
        if not info:
            return sid
        seen = [sid]
        while len(seen) < max_hops:
            row = self._rows(info["_db"], "select parent_session_id from sessions where id = ?", (seen[-1],))
            if not row or not row[0][0] or row[0][0] in seen:
                break
            seen.append(row[0][0])
        return seen[-1]

    def children(self, sid: str, limit: int = 50) -> List[dict]:
        """Delegation children of `sid` and of every compression continuation
        after it (the same conversation), newest first. Read-only."""
        info = self._one(sid)
        if not info:
            return []
        db = info["_db"]
        chain, seen = [sid], {sid}
        while True:
            nxt = self._child(db, chain[-1])
            if not nxt or nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
        marks = ",".join("?" * len(chain))
        rows = self._rows(
            db,
            "select c.id, c.title, c.started_at, c.ended_at, c.end_reason, c.last_activity_at, c.parent_session_id "
            f"from sessions c join sessions p on c.parent_session_id = p.id where p.id in ({marks}) "
            "and not (coalesce(p.end_reason, '') = 'compression' and c.started_at >= coalesce(p.ended_at, 0) - ?) "
            "order by c.started_at desc limit ?",
            (*chain, self._CONT_SLACK_S, limit))
        keys = ("id", "title", "started_at", "ended_at", "end_reason", "last_activity_at", "parent")
        return [dict(zip(keys, r)) for r in rows]

    def lookup(self, sid: str) -> Optional[dict]:
        info = self._one(sid)
        seen = {sid}
        while info and info.get("end_reason") == "compression":
            child = self._child(info["_db"], sid)
            if not child or child in seen:
                break
            seen.add(child)
            nxt = self._one(child)
            if not nxt:
                break
            nxt["continued_as"] = child
            sid, info = child, nxt
        if info:
            info.pop("_db", None)
        return info

    def drill(self, sid: str, profile: str) -> List[str]:
        pflag = "" if profile in ("default", None) else f"-p {profile} "
        return [f"lcm_load_session(session_id='{sid}')  |  session_search(session_id='{sid}')",
                f"hermes {pflag}--resume {sid}"]


class HeartbeatBackend:
    """Sessions that run `inflight hook`: state files, no harness database."""
    name = "heartbeat"

    def available(self) -> bool:
        return state.sessions_dir().is_dir()

    def lookup(self, sid: str) -> Optional[SessionInfo]:
        if not state.valid_sid(sid):
            return None
        d = state.load(sid)
        if not d:
            return None
        return SessionInfo(last_activity_at=d.get("heartbeat_at"), started_at=d.get("started_at"),
                           ended_at=d.get("ended_at"), end_reason=d.get("end_reason"),
                           profile=d.get("harness"))


class Chain:
    """Ordered backends; first non-None lookup wins. Remembers which backend
    answered each id so lineage/children/drill go to the same one."""

    def __init__(self, backends: List[Any]):
        self.backends = backends
        self._owner: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "+".join(b.name for b in self.backends)

    def lookup(self, sid: str) -> Optional[Dict[str, Any]]:
        for b in self.backends:
            try:
                info = _norm(b.lookup(sid))
            except Exception:  # a broken plugin must not break `sessions`
                state.log("backend-error", plugin=getattr(b, "name", "?"))
                continue
            if info is not None and info.get("status") != "UNKNOWN":
                info["backend"] = b.name
                self._owner[sid] = b
                return info
        return None

    def _call(self, sid: str, meth: str, default: Any, *args: Any) -> Any:
        b = self._owner.get(sid)
        if b is None:
            self.lookup(sid)
            b = self._owner.get(sid)
        fn = getattr(b, meth, None) if b is not None else None
        if fn is None:
            return default
        try:
            return fn(sid, *args)
        except Exception:
            state.log("backend-error", plugin=getattr(b, "name", "?"))
            return default

    def lineage(self, sid: str) -> List[str]:
        return self._call(sid, "lineage", [sid]) or [sid]

    def children(self, sid: str) -> List[dict]:
        return self._call(sid, "children", [])

    def family_root(self, sid: str) -> str:
        return self._call(sid, "family_root", sid) or sid

    def drill(self, sid: str, profile: str) -> List[str]:
        return self._call(sid, "drill", [], profile)


def chain(root: Optional[Path] = None, plugins: bool = True) -> Optional[Chain]:
    """The available backends, or None if none is. `plugins=False` loads only
    the built-ins (the Hermes plugin uses this: it never imports third-party
    code into the Hermes process)."""
    found: List[Any] = []
    if plugins:
        from . import plugins as plugin_mod
        found += plugin_mod.load_backends()
    found += [HermesBackend(root), HeartbeatBackend()]
    live = []
    for b in found:
        try:
            if b.available():
                live.append(b)
        except Exception:
            state.log("backend-error", plugin=getattr(b, "name", "?"))
    return Chain(live) if live else None
