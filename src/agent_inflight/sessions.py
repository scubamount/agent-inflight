"""Who owns each `## Right now` entry, and is that session still working?

Several agent sessions (desktop tabs, profiles, background workers) write one
inflight file. An entry that says "committed, NOT pushed" is only actionable
if the reader knows who wrote it and whether that session is still at work:
a live sibling means coordinate (don't touch the same files); an idle or
ended one means its work can be picked up.

Writers tag the entry head:  **2026-09-30 14:05 [session <id>] — ...**
`inflight me` prints the tag for the current session.

Status comes from a session backend, never guessed:
  ACTIVE    last activity < --active-min minutes ago (default 15)
  IDLE      no end recorded, quiet longer than that
  ENDED     ended (end reason shown)
  UNKNOWN   backend has no such id (typo, pruned, or another machine)
  NO-BACKEND  no backend available on this machine; tags still listed

Backends:
  hermes    read-only lookup in $HERMES_HOME/state.db and profiles/*/state.db;
            follows compression children so a tag on a compacted session
            resolves to the session that continues the work; --children
            lists delegated subagent sessions per entry (derived, not stored)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import core, paths, progress


class HermesBackend:
    name = "hermes"

    def __init__(self, root: Path):
        self.root = root

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
        pflag = "" if profile == "default" else f"-p {profile} "
        return [f"lcm_load_session(session_id='{sid}')  |  session_search(session_id='{sid}')",
                f"hermes {pflag}--resume {sid}"]


def backend(root: Optional[Path] = None):
    b = HermesBackend(root or Path(os.environ.get("HERMES_ROOT") or os.environ.get("HERMES_HOME")
                                   or Path.home() / ".hermes"))
    return b if b.available() else None


def status(info: Optional[dict], active_min: int, have_backend: bool) -> str:
    if not have_backend:
        return "NO-BACKEND"
    if info is None:
        return "UNKNOWN"
    if info.get("ended_at"):
        return "ENDED"
    last = info.get("last_activity_at") or info.get("started_at") or 0
    return "ACTIVE" if time.time() - last < active_min * 60 else "IDLE"


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    m = int((time.time() - ts) / 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago" if m < 2880 else f"{m // 1440}d ago"


def collect(text: str, active_min: int, be, with_children: bool = False) -> Dict:
    rn = core.right_now(core.parse(text))
    entries = rn.entries if rn else []
    me = paths.session_id()
    rows, untagged = [], 0
    for e in entries:
        sid = e.session
        if not sid:
            untagged += 1
            continue
        info = be.lookup(sid) if be else None
        p, lc = progress.progress(e.text), progress.lifecycle(e.text)
        rows.append({"session": sid, "status": status(info, active_min, be is not None),
                     "this_session": sid == me, "entry": " ".join(e.head.replace("**", "").split())[:110],
                     "progress": {"done": p.done, "open": p.open, "blocked": p.blocked, "total": p.total},
                     "state": lc.state, **(info or {})})
        if with_children and be is not None and info is not None:
            rows[-1]["children"] = be.children(sid)
    return {"backend": be.name if be else None, "entries": rows, "untagged": untagged}


def me_main() -> int:
    sid = paths.session_id()
    print(f"[session {sid}]" if sid else
          "no session id in env (set INFLIGHT_SESSION_ID, or run inside Hermes)")
    return 0 if sid else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight sessions", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--active-min", type=int, default=15)
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--me", action="store_true", help="same as `inflight me`")
    ap.add_argument("--children", action="store_true",
                    help="list each entry's delegated child sessions (read from the backend, never stored)")
    args = ap.parse_args(argv)
    if args.me:
        return me_main()

    path = (args.file or paths.inflight_file()).expanduser()
    if not path.is_file():
        print(f"{path} not found (run `inflight init`)")
        return 2
    be = backend()
    data = collect(path.read_text(encoding="utf-8"), args.active_min, be, args.children)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    if be is None:
        print("no session backend on this machine: tags listed, status unknown\n")
    for r in data["entries"]:
        who = " (this session)" if r["this_session"] else ""
        print(f"{r['status']:<10} {r['session']} @{r.get('profile', '-')}{who}  last {ago(r.get('last_activity_at'))}"
              + (f"  ended: {r.get('end_reason')}" if r["status"] == "ENDED" else ""))
        if r.get("continued_as"):
            print(f"           note  : ended by compression; continues as {r['continued_as']}")
        if r.get("title"):
            print(f"           title : {r['title'][:90]}")
        print(f"           entry : {r['entry']}")
        prog = r["progress"]
        if prog["total"] or r["state"] != "active":
            label = progress.Progress(prog["done"], prog["open"], prog["blocked"]).label()
            print(f"           state : {r['state']}" + (f"  progress {label}" if label else ""))
        kids = r.get("children")
        if kids is not None:
            print(f"           kids  : {len(kids)} delegated" + (" (newest 10)" if len(kids) > 10 else ""))
            for k in kids[:10]:
                st = status(k, args.active_min, True)
                print(f"             {st:<7} {k['id']}  {(k.get('title') or '')[:60]}")
        if be and not r["this_session"] and r["status"] not in ("UNKNOWN", "NO-BACKEND"):
            target = r.get("continued_as") or r["session"]
            drill, resume = be.drill(target, r.get("profile", "default"))
            print(f"           drill : {drill}")
            print(f"           resume: {resume}"
                  + ("   <- ACTIVE: do not resume; coordinate first" if r["status"] == "ACTIVE" else ""))
    n = len(data["entries"])
    print(f"\n{n} tagged entr{'y' if n == 1 else 'ies'}, {data['untagged']} untagged")
    return 0
