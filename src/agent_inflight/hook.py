"""`inflight hook <event>`: the harness-neutral hook protocol (version 1).

A harness runs `inflight hook <event>` with one JSON object on stdin. Unknown
fields are ignored. Stdout is plain text meant for the model, or nothing.
Exit status is ALWAYS 0: in Claude Code, exit 2 from a PreToolUse hook blocks
the tool, so any error is logged to hooks.log and swallowed (fail open).

  session-start {session_id, cwd, source: new|startup|resume|compact|clear}
      records the session; on new/startup runs the audit catch-up for ENDED
      sessions (recorded repos only, ~3 s budget, at most every 10 min);
      then prints the brief (brief.py; same text the Hermes plugin injects)
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

`inflight hook <event> --harness <name>` records the harness name and adapts
field names and output: with `claude-code`, `tool_name`/`new_cwd` are read and
model-facing text goes out as `hookSpecificOutput.additionalContext` JSON
(never a permission decision).
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


def normalize(event: str, payload: Dict[str, Any], harness: Optional[str]) -> Dict[str, Any]:
    """Map a harness's own field names onto protocol v1. Claude Code sends
    `tool_name` (not `tool`) and, on CwdChanged, `new_cwd`."""
    p = dict(payload)
    if "tool" not in p and isinstance(p.get("tool_name"), str):
        p["tool"] = p["tool_name"]
    if event == "cwd-changed" and isinstance(p.get("new_cwd"), str):
        p["cwd"] = p["new_cwd"]
    if harness and not p.get("harness"):
        p["harness"] = harness
    return p


# Events whose stdout a harness can deliver to the model, and how.
_CC_EVENT = {"session-start": "SessionStart", "pre-tool": "PreToolUse", "post-tool": "PostToolUse"}


def render_for(harness: Optional[str], event: str, text: str) -> str:
    """Claude Code shows plain stdout to the model only on SessionStart; on
    PreToolUse it must be `hookSpecificOutput.additionalContext` JSON. No
    permissionDecision is ever emitted: the hook never allows or denies."""
    if harness != "claude-code" or event not in _CC_EVENT:
        return text
    return json.dumps({"hookSpecificOutput": {"hookEventName": _CC_EVENT[event],
                                              "additionalContext": text[:9000]}})


def _collisions(sid: str, repo: str) -> List[str]:
    """Other sessions that touched `repo` in the last ACTIVE_WINDOW_S and have
    not ended. A Hermes session's end lives in state.db, not in its hook state
    file, so a state file with no `ended_at` is checked against the built-in
    backends (no plugins: this runs inside a hook or the Hermes process)."""
    from . import backends
    out: List[str] = []
    be: Optional[backends.Chain] = None
    looked = False
    for other, d in state.recent(ACTIVE_WINDOW_S):
        if other == sid or d.get("ended_at"):
            continue
        if repo not in (d.get("repos") or {}):
            continue
        if d.get("harness") == "hermes":
            if not looked:
                be, looked = backends.chain(plugins=False), True
            info = be.lookup(other) if be is not None else None
            if info and info.get("ended_at"):
                continue
        out.append(other)
    return out


def collision_warning(sid: str, repo: str, event: str = "pre-tool") -> str:
    """The once-per-(session, repo) warning that another live session touched
    `repo` recently, or ''. Shared by the hook and the Hermes plugin. It only
    informs: nothing here allows, denies or delays a tool call."""
    others = _collisions(sid, repo)
    if not others or repo in (state.load(sid).get("warned") or []):
        return ""
    state.update(sid, warned=repo)
    state.log("collision-warning", event=event, session=sid)
    return (f"[inflight] another active session ({', '.join(others[:3])}) touched {repo} in the "
            f"last {ACTIVE_WINDOW_S // 60} min. Coordinate before editing the same files "
            "(`inflight sessions`).")


def handle(event: str, payload: Dict[str, Any]) -> str:
    """Run one event; returns the text for stdout ('' for none). May raise:
    run() turns any exception into a logged fail-open."""
    sid = _sid(payload)
    if event not in EVENTS or not state.valid_sid(sid):
        if sid and not state.valid_sid(sid):
            state.log("hook-refused", event=event, error="invalid session id")
        return ""
    cwd = payload.get("cwd")
    # session-start records nothing: opening in a repo is not touching it. If
    # it counted, every new session would become the latest recorder of the
    # repo it opened in and take over (or hide) the owed work a dead session
    # left there, so catch-up would tag the wrong session.
    repo = state.repo_root(cwd) if event in ("pre-tool", "post-tool", "cwd-changed") else None

    if event == "session-end":
        reason = payload.get("reason")
        state.update(sid, ended_at=time.time(), end_reason=str(reason)[:40] if reason else "end")
        state.log("session-end", event=event, session=sid)
        return ""

    if event == "pre-tool":
        state.update(sid)
        if not repo:
            return ""
        return collision_warning(sid, repo, event)

    if event == "session-start":
        source = str(payload.get("source") or "new")
        harness = payload.get("harness")
        fields: Dict[str, Any] = {"ended_at": None, "end_reason": None}
        if isinstance(harness, str) and harness:
            fields["harness"] = harness[:40]
        state.update(sid, **fields)
        if source in ("new", "startup", "clear"):
            from . import audit
            audit.catch_up()  # bounded, rate-limited, never raises; writes only for DEAD sessions
        from . import backends, brief
        try:
            text = paths.inflight_file().expanduser().read_text(encoding="utf-8")
        except OSError:
            return ""
        reason = {"compact": "compaction", "resume": "resume"}.get(source, "session start")
        return brief.build(text, sid, [sid], reason, backends.chain(plugins=False))

    state.update(sid, repo=repo)  # post-tool, cwd-changed, heartbeat
    return ""


def run(argv: Optional[List[str]] = None, stdin: Any = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    event = argv[0] if argv else ""
    harness = None
    if "--harness" in argv[1:]:
        i = argv.index("--harness", 1)
        harness = argv[i + 1][:40] if i + 1 < len(argv) else None
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = handle(event, normalize(event, payload, harness))
        if out:
            sys.stdout.write(render_for(harness, event, out.rstrip()) + "\n")
    except Exception as e:  # fail open: never block, never exit nonzero
        state.log("hook-error", event=event, error=type(e).__name__)
    return 0
