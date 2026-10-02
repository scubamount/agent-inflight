"""Parse an inflight file into sections and entries.

File shape (all of it optional, parsing never fails):

    # anything before the first `## ` header is the preamble
    ## Right now
    **2026-09-30 14:05 [session 20260930_130520_a7dc5f] — short headline.** body...
    more body lines belong to the entry above

    **2026-09-29 — next entry (untagged is fine).**
    ## Some other section
    ...

An ENTRY starts only at a line that opens with `**` followed by an ISO date
(`**2026-09-30`). Every following line belongs to it until the next entry
start, so a bolded paragraph inside an entry (`**Note:** ...`) stays part of
it. Entries do not need blank lines between them; writers often forget, and a
parser that needed them merged two entries into one and lost the second one's
session tag. Undated bold text before the first entry is section lead
(`inflight check` reports it).

The head tag is `[session <id> #<entry-id>]`; `inflight add` writes both.
The entry id (6 hex chars) is the stable key `done` and the Hermes plugin
match on, so editing the headline doesn't change which entry it is. Entries
written before ids existed have none and fall back to the head text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

RIGHT_NOW = "## Right now"
DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
ENTRY_DATE_HEAD = re.compile(r"^\*\*\s*20\d{2}-\d{2}-\d{2}")
# A session tag: `[session <id>]`. Ids are opaque; Hermes uses
# 20260930_130520_a7dc5f, other harnesses use UUIDs or anything else.
TAG_RE = re.compile(r"\[session ([A-Za-z0-9][A-Za-z0-9_.:-]*)(?: #([0-9a-f]{4,12}))?\]")
ID_ONLY_RE = re.compile(r"\[#([0-9a-f]{4,12})\]")
# A tag whose id was never expanded: `[session $HERMES_SESSION_ID]`, `[$X]`,
# `[${X}]`. Written when an agent hand-types the tag instead of using `add`.
LITERAL_TAG_RE = re.compile(r"\[(?:session )?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?(?: #([0-9a-f]{4,12}))?\]")


def parse_date(text: str) -> Optional[date]:
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


@dataclass
class Entry:
    text: str  # full entry text, no trailing newline

    @property
    def head(self) -> str:
        return self.text.split("\n", 1)[0]

    @property
    def date(self) -> Optional[date]:
        return parse_date(self.head[:40])

    @property
    def session(self) -> Optional[str]:
        m = TAG_RE.search(self.head)
        return m.group(1) if m else None

    @property
    def id(self) -> Optional[str]:
        """`#<id>` from the head tag (real or literal), or a bare `[#<id>]`."""
        m = TAG_RE.search(self.head)
        if m and m.group(2):
            return m.group(2)
        m = LITERAL_TAG_RE.search(self.head)
        if m and m.group(1):
            return m.group(1)
        m = ID_ONLY_RE.search(self.head)
        return m.group(1) if m else None


@dataclass
class Section:
    header: str  # "" for the preamble
    lead: str = ""  # text between the header and the first entry
    entries: List[Entry] = field(default_factory=list)

    @property
    def is_right_now(self) -> bool:
        return self.header.strip().lower() == RIGHT_NOW.lower()

    def render(self) -> str:
        parts = []
        if self.header:
            parts.append(self.header)
        if self.lead.strip():
            parts.append(self.lead.strip("\n"))
        parts.extend(e.text for e in self.entries)
        return "\n\n".join(parts).rstrip() + "\n"


def split_entries(body: str) -> Tuple[str, List[Entry]]:
    """(lead, entries) for one section body. Entries start only at dated `**` heads."""
    lead: List[str] = []
    entries: List[List[str]] = []
    for line in body.split("\n"):
        if ENTRY_DATE_HEAD.match(line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            lead.append(line)
    return "\n".join(lead), [Entry("\n".join(e).strip("\n")) for e in entries]


def new_id(seed: str, taken: "set[str]") -> str:
    """6 hex chars from the seed, lengthened on the (rare) collision."""
    import hashlib
    h = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()
    n = 6
    while h[:n] in taken and n < len(h):
        n += 1
    return h[:n]


def parse(text: str) -> List[Section]:
    parts = re.split(r"(?m)^(## .*)$", text)
    out: List[Section] = []
    if parts[0].strip():
        out.append(Section(header="", lead=parts[0]))
    for i in range(1, len(parts), 2):
        header = parts[i].rstrip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        lead, entries = split_entries(body)
        out.append(Section(header=header, lead=lead, entries=entries))
    return out


def render(sections: List[Section]) -> str:
    return "\n".join(s.render() for s in sections if s.header or s.lead.strip()).rstrip() + "\n"


def right_now(sections: List[Section]) -> Optional[Section]:
    return next((s for s in sections if s.is_right_now), None)
