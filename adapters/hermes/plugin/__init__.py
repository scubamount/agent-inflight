"""agent-inflight Hermes plugin: runtime session tags for hand-edited entries.

pre_tool_call         when a tool call may touch inflight.md, record the entry
                      keys, the file stat and the lint findings. Never blocks.
transform_tool_result if the file changed, rewrite literal `[session $VAR]`
                      tags on entries THIS call wrote (proven by the call's own
                      text) to the real session id the runtime passed in, then
                      report anything new. Only this hook can reach the model:
                      Hermes discards post_tool_call's return value.

The plugin makes no tool calls and no subprocesses, so it cannot re-enter the
tool loop. Any error falls back to "change nothing" (fail open).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The plugin directory is a symlink into the agent-inflight checkout
# (adapters/hermes/plugin); the package lives at <checkout>/src.
_SRC = Path(os.path.realpath(__file__)).parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_inflight import core, entries, paths, retag, trim  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_S = 600  # a call whose result never arrives (tool raised) is forgotten after this
_EDIT_TOOLS = {"write_file": ("content",), "patch": ("new_string", "patch")}
_SHELL_TOOLS = {"terminal": "command", "execute_code": "code"}


class _Snap:
    __slots__ = ("at", "stat", "keys", "findings", "provenance")

    def __init__(self, stat: Optional[Tuple[int, int]], keys, findings, provenance: str):
        self.at = time.monotonic()
        self.stat, self.keys, self.findings, self.provenance = stat, keys, findings, provenance


_snapshots: Dict[str, _Snap] = {}
_lock = threading.Lock()


def _target() -> Path:
    return paths.inflight_file().expanduser().resolve()


def _stat(p: Path) -> Optional[Tuple[int, int]]:
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def touch_provenance(tool_name: str, args: Any, target: Path) -> Optional[str]:
    """The call's own text when it may write `target`, else None."""
    if not isinstance(args, dict):
        return None
    if tool_name in _EDIT_TOOLS:
        text = "\n".join(str(args.get(k) or "") for k in _EDIT_TOOLS[tool_name])
        p = str(args.get("path") or "")
        try:
            hit = bool(p) and Path(os.path.expanduser(p)).resolve() == target
        except OSError:
            hit = False
        # patch mode=patch (V4A) names files inside the patch body, not in `path`
        return text if hit or target.name in str(args.get("patch") or "") else None
    key = _SHELL_TOOLS.get(tool_name)
    if key:
        text = str(args.get(key) or "")
        return text if target.name in text else None
    return None


def _prune(now: float) -> None:
    for k in [k for k, s in _snapshots.items() if now - s.at > SNAPSHOT_TTL_S]:
        del _snapshots[k]


def on_pre_tool_call(tool_name: str = "", args: Any = None, tool_call_id: str = "", **_: Any) -> None:
    try:
        target = _target()
        prov = touch_provenance(tool_name, args, target)
        if prov is None or not tool_call_id:
            return None
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            text = ""
        snap = _Snap(_stat(target), retag.snapshot(text), set(entries.lint(text)[1]) if text else set(), prov)
        with _lock:
            _prune(snap.at)
            _snapshots[tool_call_id] = snap
    except Exception:
        logger.debug("agent-inflight pre_tool_call failed", exc_info=True)
    return None  # observe only


def on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None,
                             session_id: str = "", tool_call_id: str = "", **_: Any) -> Optional[str]:
    with _lock:
        snap = _snapshots.pop(tool_call_id, None) if tool_call_id else None
    if snap is None or not isinstance(result, str):
        return None
    try:
        target = _target()
        st = _stat(target)
        if st is None or st == snap.stat:
            return None  # read-only call, or the file is gone: nothing to say
        text = target.read_text(encoding="utf-8")
        notes: List[str] = []
        res = retag.retag(text, snap.keys, session_id, snap.provenance)
        if res.retagged:
            if _stat(target) == st:
                trim.atomic_write(target, res.text)
                text = res.text
                notes.append(f"retagged {res.retagged} new entr{'y' if res.retagged == 1 else 'ies'} "
                             f"with [session {session_id}]")
            else:
                notes.append("retag skipped: the file changed again underneath (another session); "
                             "fix the tag with `inflight add` or re-run the edit")
        if res.unproven:
            notes.append(f"{res.unproven} new entr{'y' if res.unproven == 1 else 'ies'} with a literal "
                         "session variable that this call can't be shown to have written; left as is")
        notes += [f for f in entries.lint(text)[1] if f not in snap.findings]
        if not notes:
            return None
        return result + "\n\n[inflight] " + "; ".join(notes) + ". Prefer `inflight add` for new entries."
    except Exception:
        logger.debug("agent-inflight transform_tool_result failed", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
