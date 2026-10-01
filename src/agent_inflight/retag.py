"""Give hand-written entries the session tag the runtime knows.

`inflight add` writes `[session <id>]` from the environment. An agent that
edits the file by hand often types the tag literally, `[session $HERMES_SESSION_ID]`,
which nobody can trace afterwards. A harness hook that sees the edit happen
knows the real session id (the runtime passes it; the model never types it)
and can fix the tag in place.

Rules, all conservative:

- Only entries ABSENT from the pre-edit snapshot are candidates; an older
  literal tag might belong to any session and is reported, never claimed.
- A candidate is claimed only when the edit's own text contains it
  (`provenance`): two sessions editing at once can't claim each other's entry.
- Snapshot keys are the entry id (`#a1b2c3`) when it has one, else the head
  line, so an edited head on an existing entry is not mistaken for a new
  entry, and two sessions' new entries are told apart by id. The id is kept
  when the tag is rewritten.

Pure functions; the caller does the I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Set

from . import core

_WS = re.compile(r"\s+")


def entry_key(e: "core.Entry") -> str:
    return e.id or e.head


def snapshot(text: str) -> Set[str]:
    rn = core.right_now(core.parse(text))
    return {entry_key(e) for e in rn.entries} if rn else set()


def _norm(s: str) -> str:
    s = core.LITERAL_TAG_RE.sub("", s)
    s = core.TAG_RE.sub("", s)
    s = core.ID_ONLY_RE.sub("", s)
    return _WS.sub(" ", s.replace("**", "").replace("\\", "")).strip()


def written_by(e: "core.Entry", provenance: str) -> bool:
    """True when the edit text contains this entry's head. Tags and ids are
    removed from both sides first: they are the part a writer gets wrong."""
    head = _norm(e.head)
    # The bolded headline (date + text) is what the writer typed; the body may
    # be wrapped or edited separately. 60 chars is enough to be unique.
    bold = re.match(r"\*\*(.*?)\*\*", e.head)
    needle = (_norm(bold.group(1)) if bold else head)[:60]
    return bool(needle) and needle in _norm(provenance)


@dataclass
class Result:
    text: str
    retagged: int = 0
    unproven: int = 0   # new literal entries this edit can't be shown to have written
    foreign: int = 0    # literal entries that were already there before the edit


def retag(text: str, before: Set[str], sid: str, provenance: str,
          tag_for: Optional[Callable[[str], str]] = None) -> Result:
    """Rewrite literal tags on entries this edit wrote. `tag_for(sid)` returns
    the replacement tag text (defaults to `[session <sid>]`)."""
    sections = core.parse(text)
    rn = core.right_now(sections)
    res = Result(text)
    if rn is None or not sid:
        return res
    make = tag_for or (lambda s: f"[session {s}]")
    for e in rn.entries:
        if not core.LITERAL_TAG_RE.search(e.head):
            continue
        if entry_key(e) in before:
            res.foreign += 1
            continue
        if not written_by(e, provenance):
            res.unproven += 1
            continue
        head, nl, rest = e.text.partition("\n")
        eid = e.id
        e.text = core.LITERAL_TAG_RE.sub(lambda _m: make(sid) if not eid else make(sid)[:-1] + f" #{eid}]",
                                         head, count=1) + nl + rest
        res.retagged += 1
    if res.retagged:
        res.text = core.render(sections)
    return res
