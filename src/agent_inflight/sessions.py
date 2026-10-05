"""Who owns each `## Right now` entry, and is that session still working?

Several agent sessions (desktop tabs, profiles, background workers) write one
inflight file. An entry that says "committed, NOT pushed" is only actionable
if the reader knows who wrote it and whether that session is still at work:
a live sibling means coordinate (don't touch the same files); an idle or
ended one means its work can be picked up.

Writers tag the entry head:  **2026-09-30 14:05 [session <id>] — ...**
`inflight me` prints the tag for the current session.

Status comes from a session backend, never guessed:
  ACTIVE    last activity < --active-min minutes ago (default state.ACTIVE_MIN, 15)
  IDLE      no end recorded, quiet longer than that
  ENDED     ended (end reason shown)
  UNKNOWN   backend has no such id (typo, pruned, or another machine)
  NO-BACKEND  no backend available on this machine; tags still listed

Backends (backends.py; first answer wins):
  plugins   allowlisted entry points (`inflight plugin enable <name>`)
  hermes    read-only lookup in $HERMES_HOME/state.db and profiles/*/state.db;
            follows compression children so a tag on a compacted session
            resolves to the session that continues the work; --children
            lists delegated subagent sessions per entry (derived, not stored)
  heartbeat state files written by `inflight hook` (Claude Code, others)
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import backends, core, paths, progress, state


def backend(root: Optional[Path] = None, plugins: bool = True):
    """Every available backend as one `Chain` (first answer wins), or None."""
    return backends.chain(root, plugins=plugins)


def status(info: Optional[dict], active_min: int, have_backend: bool) -> str:
    if not have_backend:
        return "NO-BACKEND"
    if info is None:
        return "UNKNOWN"
    if info.get("status") in backends.STATUSES:
        return info["status"]
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
        rows.append({"session": sid, "id": e.id, "status": status(info, active_min, be is not None),
                     "this_session": sid == me, "entry": e.label[:110],
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
    ap.add_argument("--active-min", type=int, default=state.ACTIVE_MIN)
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
            cmds = be.drill(target, r.get("profile", "default"))
            if len(cmds) >= 1:
                print(f"           drill : {cmds[0]}")
            if len(cmds) >= 2:
                print(f"           resume: {cmds[1]}"
                      + ("   <- ACTIVE: do not resume; coordinate first" if r["status"] == "ACTIVE" else ""))
    n = len(data["entries"])
    print(f"\n{n} tagged entr{'y' if n == 1 else 'ies'}, {data['untagged']} untagged")
    return 0
