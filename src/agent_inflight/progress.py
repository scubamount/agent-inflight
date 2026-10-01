"""Derived progress and the explicit lifecycle of one entry. Pure functions.

Format (additive: entries without these lines parse as before):

    **2026-09-30 19:10 [session S #a1b2] — agent-inflight rollout.** prose...
    - [x] C1 plugin
    - [ ] C2 re-injection
    - [~] push (blocked: waiting on review)
    status: paused (stale since 2026-10-03)

- Checkbox lines (`- [ ]` open, `- [x]` done, `- [~]` blocked) are counted
  only in the entry BODY, never the head, and never inside a ``` / ~~~ fence.
- Progress is derived on every read, never stored.
- `status:` is the one optional lifecycle line: `active` (also when absent),
  `paused (stale since D)` or `done D`. Same fence rule. The CLI writes it;
  ticking every box does NOT close an entry (closing is an explicit act).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, List, Optional, Tuple

BOX_RE = re.compile(r"^\s*[-*]\s+\[( |x|X|~)\]\s+(.*)$")
STATUS_RE = re.compile(r"^\s*status:\s*(active|paused|done)\b(.*)$", re.I)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")

STATES = ("active", "paused", "done")


def body_lines(entry_text: str) -> Iterator[Tuple[int, str]]:
    """(index, line) for body lines OUTSIDE code fences. Index 0 is the head."""
    fenced = False
    for i, line in enumerate(entry_text.split("\n")):
        if i == 0:
            continue
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield i, line


@dataclass
class Progress:
    done: int = 0
    open: int = 0
    blocked: int = 0

    @property
    def total(self) -> int:
        return self.done + self.open + self.blocked

    def label(self) -> str:
        if not self.total:
            return ""
        return f"{self.done}/{self.total}" + (f" ({self.blocked} blocked)" if self.blocked else "")


def progress(entry_text: str) -> Progress:
    p = Progress()
    for _, line in body_lines(entry_text):
        m = BOX_RE.match(line)
        if not m:
            continue
        mark = m.group(1)
        if mark in "xX":
            p.done += 1
        elif mark == "~":
            p.blocked += 1
        else:
            p.open += 1
    return p


@dataclass
class Lifecycle:
    state: str = "active"
    since: Optional[date] = None  # paused: stale since; done: closed on


def lifecycle(entry_text: str) -> Lifecycle:
    found = Lifecycle()
    for _, line in body_lines(entry_text):
        m = STATUS_RE.match(line)
        if m:
            d = _DATE.search(m.group(2) or "")
            found = Lifecycle(m.group(1).lower(), date.fromisoformat(d.group(1)) if d else None)
    return found


def set_status(entry_text: str, state: str, when: date) -> str:
    """Drop every unfenced status line, then append the new one (none for active)."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    drop = {i for i, line in body_lines(entry_text) if STATUS_RE.match(line)}
    lines = [l for i, l in enumerate(entry_text.split("\n")) if i not in drop]
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    label = {"done": f"status: done {when.isoformat()}",
             "paused": f"status: paused (stale since {when.isoformat()})",
             "active": ""}[state]
    if label:
        lines.append(label)
    return "\n".join(lines)


def last_touched(head_date: Optional[date], session_last_activity: Optional[float]) -> Optional[date]:
    """Staleness clock: newest of the entry's date and its owner's last activity,
    so a session still at work keeps its entry fresh without editing it."""
    act = datetime.fromtimestamp(session_last_activity).date() if session_last_activity else None
    cands = [d for d in (head_date, act) if d]
    return max(cands) if cands else None


def reconcile_stale(entry_text: str, touched: Optional[date], today: date, stale_days: int) -> Tuple[str, bool]:
    """active -> paused once untouched for `stale_days`. Never touches paused/done; never deletes."""
    if lifecycle(entry_text).state != "active" or touched is None:
        return entry_text, False
    if today - touched < timedelta(days=stale_days):
        return entry_text, False
    return set_status(entry_text, "paused", today), True


def summary(entry_text: str) -> str:
    """One short column for listings: `2/4 (1 blocked) · paused`."""
    parts: List[str] = []
    p = progress(entry_text).label()
    if p:
        parts.append(p)
    lc = lifecycle(entry_text)
    if lc.state != "active":
        parts.append(lc.state)
    return " · ".join(parts)
