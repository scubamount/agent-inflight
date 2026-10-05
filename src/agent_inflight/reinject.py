"""When a Hermes session gets the brief (brief.py).

Compaction replaces the conversation with a summary, and an entry's exact
wording (SHAs, "NOT pushed", the next step) is what summaries lose first, so
the brief is delivered again after it.

When (decide()):
  session start  the conversation's first turn
  compaction     the set of compaction markers in the history changed since
                 the last turn this process saw (in-place compaction, e.g.
                 hermes-lcm, which keeps the session id)
  resume         first turn this process sees for the session and it is not
                 the conversation's first turn (resume, a rotated session, or
                 the same session after a Hermes restart)

A restart forgets which sessions this process has seen, so on that first turn
the history is checked: if it already holds a brief after its last compaction
marker, the session still has it and gets nothing new. The label is `resume`
either way; only a compaction this process saw happen is called `compaction`.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

COMPACTION_RE = re.compile(
    r"\[(?:Recent|Session Arc|Durable|Depth-\d+) Summary \(d\d+, node \d+\)\]"  # hermes-lcm
    r"|\[CONTEXT COMPACTION"                                                     # built-in compressor
    r"|\[CONTEXT SUMMARY\]:")                                                    # legacy prefix
BRIEF_RE = re.compile(r"\[inflight brief: ")


def _texts(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                yield part["text"]


def compaction_signature(history: Any) -> str:
    """Hash of every compaction marker (with its first 120 chars) in the
    history. Tool results are skipped: a grep or log read that prints a marker
    is not a compaction."""
    h = hashlib.sha1()
    for m in history or []:
        if not isinstance(m, dict) or m.get("role") == "tool":
            continue
        for text in _texts(m.get("content")):
            for hit in COMPACTION_RE.finditer(text):
                h.update(text[hit.start():hit.start() + 120].encode("utf-8", "replace"))
    return h.hexdigest()


def has_brief(history: Any) -> bool:
    """True when a brief appears after the last compaction marker. Only user
    turns deliver a brief; they carry it in `content` or, for Hermes' replayed
    turns, in the `api_content` sidecar (the bytes actually sent). Tool
    results are skipped entirely: a test run or grep that prints a brief or a
    marker is neither a delivery nor a compaction."""
    found = False
    for m in history or []:
        if not isinstance(m, dict) or m.get("role") == "tool":
            continue
        user = m.get("role") == "user"
        for key in ("content", "api_content"):
            for text in _texts(m.get(key)):
                last_c = max((h.start() for h in COMPACTION_RE.finditer(text)), default=-1)
                last_b = max((h.start() for h in BRIEF_RE.finditer(text)), default=-1) if user else -1
                if last_c > last_b:
                    found = False
                elif last_b >= 0:
                    found = True
    return found


def decide(prev_sig: Optional[str], sig: str, is_first_turn: bool, history: Any = None) -> Optional[str]:
    """'session start' | 'compaction' | 'resume' | None."""
    if prev_sig is None:
        if is_first_turn:
            return "session start"
        return None if has_brief(history) else "resume"
    return "compaction" if sig != prev_sig else None
