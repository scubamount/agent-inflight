"""`inflight hook <event>`: the harness-neutral hook protocol (version 1).

A harness runs `inflight hook <event>` with one JSON object on stdin. Unknown
fields are ignored. Stdout is plain text meant for the model, or nothing.
Exit status is ALWAYS 0: in Claude Code, exit 2 from a PreToolUse hook blocks
the tool, so any error is logged to hooks.log and swallowed (fail open).

  session-start {session_id, cwd, source: new|startup|resume|compact|clear}
      records the session; on resume/compact prints the session's own open
      entries (same block as the Hermes re-inject)
  pre-tool      {session_id, cwd, tool, call_id}
      heartbeat; prints a collision warning, once per (session, repo), when
      another session with a heartbeat in the last ACTIVE window has touched
      the same repo
  post-tool     {session_id, cwd, tool, call_id}
      heartbeat; records cwd's repo as touched
  cwd-changed   {session_id, cwd}
      heartbeat; records the new repo
  session-end   {session_id, cwd, reason}
      records ended_at + reason. Nothing else: harnesses give this hook ~1.5 s
      and crashes skip it, so the owed-work audit runs later as catch-up.
  heartbeat     {session_id}

`args` (tool arguments) is accepted and never read, stored or logged.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

from . import paths, state

HOOK_PROTOCOL_VERSION = 1
EVENTS = ("session-start", "pre-tool", "post-tool", "cwd-changed", "session-end", "heartbeat")
ACTIVE_WINDOW_S = 15 * 60


def _sid(payload: Dict[str, Any]) -> str:
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        sid = paths.session_id()
    return sid


def _collisions(sid: str, repo: str) -> List[str]:
    out = []
    for other, d in state.recent(ACTIVE_WINDOW_S):
        if other == sid or d.get("ended_at"):
            continue
        if repo in (d.get("repos") or {}):
            out.append(other)
    return out


def handle(event: str, payload: Dict[str, Any]) -> str:
    """Run one event; returns the text for stdout ('' for none). May raise:
    run() turns any exception into a logged fail-open."""
    sid = _sid(payload)
    if event not in EVENTS or not state.valid_sid(sid):
        if sid and not state.valid_sid(sid):
            state.log("hook-refused", event=event, error="invalid session id")
        return ""
    cwd = payload.get("cwd")
    repo = state.repo_root(cwd) if event in ("pre-tool", "post-tool", "cwd-changed", "session-start") else None

    if event == "session-end":
        reason = payload.get("reason")
        state.update(sid, ended_at=time.time(), end_reason=str(reason)[:40] if reason else "end")
        state.log("session-end", event=event, session=sid)
        return ""

    if event == "pre-tool":
        state.update(sid)
        if not repo:
            return ""
        others = _collisions(sid, repo)
        if not others or repo in (state.load(sid).get("warned") or []):
            return ""
        state.update(sid, warned=repo)
        state.log("collision-warning", event=event, session=sid)
        return (f"[inflight] another active session ({', '.join(others[:3])}) touched {repo} in the "
                f"last {ACTIVE_WINDOW_S // 60} min. Coordinate before editing the same files "
                "(`inflight sessions`).")

    if event == "session-start":
        source = str(payload.get("source") or "new")
        harness = payload.get("harness")
        fields: Dict[str, Any] = {"ended_at": None, "end_reason": None}
        if isinstance(harness, str) and harness:
            fields["harness"] = harness[:40]
        state.update(sid, repo=repo, **fields)
        if source not in ("resume", "compact"):
            return ""
        from . import reinject
        try:
            text = paths.inflight_file().expanduser().read_text(encoding="utf-8")
        except OSError:
            return ""
        mine = reinject.owned(text, [sid])
        if not mine:
            return ""
        return reinject.render(mine, sid, [sid], "compaction" if source == "compact" else "resume")

    state.update(sid, repo=repo)  # post-tool, cwd-changed, heartbeat
    return ""


def run(argv: Optional[List[str]] = None, stdin: Any = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    event = argv[0] if argv else ""
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle(event, payload)
        if out:
            sys.stdout.write(out.rstrip() + "\n")
    except Exception as e:  # fail open: never block, never exit nonzero
        state.log("hook-error", event=event, error=type(e).__name__)
    return 0
