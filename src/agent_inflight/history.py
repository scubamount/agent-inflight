"""History: find any entry, open or archived. `inflight show` and `inflight log`.

`trim` moves entries out of the tracker into `inflight-archive/*.md` and
never deletes them, but nothing read the archive back: an archived id made
`inflight done` answer "no entry matches", as if the entry never existed.
This module reads the tracker and every archive file as one history.

The same entry can sit in several files (a snapshot copy and a later trim).
The newest copy wins: archive files in name order (their names carry the
stamp), then the tracker last. Entries with an id are keyed by it; entries
from before ids existed are keyed by their head line without its tag.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import core, paths, progress


@dataclass
class Found:
    entry: core.Entry
    where: str      # "tracker" or the archive file name
    open: bool      # in the tracker's `## Right now` and not done


def _entries(text: str) -> List[core.Entry]:
    """Every dated entry in a file, in any section, including the preamble."""
    out: List[core.Entry] = []
    for chunk in re.split(r"(?m)^## .*$", text):
        out += core.split_entries(chunk)[1]
    return out


def _key(e: core.Entry) -> str:
    """Entry id, else the bold head without its tag: `retag` rewrote literal
    `[session $X]` tags and agents append to the head line, and the older
    copy is still the same entry."""
    if e.id:
        return e.id
    bold = e.head.split("**", 2)[1] if e.head.count("**") >= 2 else e.head
    return " ".join(core.LITERAL_TAG_RE.sub("", core.TAG_RE.sub("", bold)).split())


def ids(tracker: Path) -> "set[str]":
    """Every entry id in the archive, so `add` never reuses an archived one."""
    return {x.entry.id for x in load(tracker) if x.entry.id and x.where != "tracker"}


def archive_files(tracker: Path) -> List[Path]:
    adir = paths.archive_dir(tracker)
    return sorted(adir.glob("inflight-*.md")) if adir.is_dir() else []


def load(tracker: Path) -> List[Found]:
    """One copy per entry, newest copy wins; newest entries first."""
    seen: Dict[str, Found] = {}
    for f in archive_files(tracker):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for e in _entries(text):
            seen[_key(e)] = Found(e, f.name, False)
    if tracker.is_file():
        rn = core.right_now(core.parse(tracker.read_text(encoding="utf-8")))
        for e in (rn.entries if rn else []):
            seen[_key(e)] = Found(e, "tracker", progress.lifecycle(e.text).state != "done")
    return sorted(seen.values(), key=lambda x: x.entry.head[:40], reverse=True)


def find(history: List[Found], needle: str) -> List[Found]:
    """An exact entry id wins outright; otherwise a case-insensitive substring
    of the whole entry (head and body)."""
    n = core.normalize_id(needle)
    by_id = [x for x in history if x.entry.id and x.entry.id == n]
    if by_id:
        return by_id
    low = needle.strip().lower()
    return [x for x in history if low and low in x.entry.text.lower()]


def archived_hint(tracker: Path, needle: str) -> str:
    """For `done`: where an entry that is no longer in `## Right now` went, or ""."""
    hits = [x for x in find(load(tracker), needle) if x.where != "tracker"]
    if len(hits) != 1:
        return ""
    e = hits[0].entry
    return f"#{e.id or '------'} is archived in {hits[0].where} (`inflight show {e.id or needle}`)"


def _label(x: Found) -> str:
    head = x.entry.label
    state = "archived" if x.where != "tracker" else progress.lifecycle(x.entry.text).state
    return f"{x.where}  {state}  {head[:110]}"


def _tracker(path: Optional[Path]) -> Path:
    return (path or paths.inflight_file()).expanduser()


def show_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inflight show",
        description="Print one entry in full, open or archived, and the file it is in. "
                    "Exit 0 found, 1 no match, 2 ambiguous.")
    ap.add_argument("match", help="entry id (a1b2c3), or words from the entry")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args(argv)
    hits = find(load(_tracker(args.file)), args.match)
    if not hits:
        print(f"no entry, open or archived, matches {args.match!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"{len(hits)} entries match {args.match!r}; use the entry id (`inflight log` lists them):",
              file=sys.stderr)
        for x in hits[:20]:
            print(f"  #{x.entry.id or '------'}  {_label(x)}", file=sys.stderr)
        return 2
    x = hits[0]
    print(f"[{x.where}]")
    print(x.entry.text)
    return 0


def log_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inflight log",
        description="List entries, open and archived, newest first. With a query: only entries "
                    "whose text contains it (case-insensitive) or whose id it is.")
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("-n", "--limit", type=int, default=30, help="at most this many lines (0 = all)")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args(argv)
    history = load(_tracker(args.file))
    hits = find(history, args.query) if args.query else history
    shown = hits if args.limit <= 0 else hits[:args.limit]
    for x in shown:
        print(f"#{x.entry.id or '------'}  {_label(x)}")
    more = len(hits) - len(shown)
    print(f"{len(hits)} of {len(history)} entries" + (f" ({more} not shown; -n 0 for all)" if more else ""))
    return 0 if hits else 1
