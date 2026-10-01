"""Parse an inflight file into sections and entries.

File shape (all of it optional, parsing never fails):

    # anything before the first `## ` header is the preamble
    ## Right now
    **2026-09-30 14:05 [session 20260930_130520_a7dc5f] — short headline.** body...
    more body lines belong to the entry above

    **2026-09-29 — next entry (untagged is fine).**
    ## Some other section
    ...

An ENTRY starts at a line that opens with `**` followed by an ISO date
(`**2026-09-30`), or with `**` right after a blank line. Every following line
belongs to it until the next entry start. Entries do not need blank lines
between them; writers often forget, and a parser that needed them merged two
entries into one and lost the second one's session tag.
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
TAG_RE = re.compile(r"\[session ([A-Za-z0-9][A-Za-z0-9_.:-]*)\]")
# A tag whose id was never expanded: `[session $HERMES_SESSION_ID]`, `[$X]`,
# `[${X}]`. Written when an agent hand-types the tag instead of using `add`.
LITERAL_TAG_RE = re.compile(r"\[(?:session )?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\]")


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
    """(lead, entries) for one section body."""
    lead: List[str] = []
    entries: List[List[str]] = []
    prev_blank = True
    for line in body.split("\n"):
        starts = line.startswith("**") and (ENTRY_DATE_HEAD.match(line) or prev_blank)
        if starts:
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            lead.append(line)
        prev_blank = not line.strip()
    return "\n".join(lead), [Entry("\n".join(e).strip("\n")) for e in entries]


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
