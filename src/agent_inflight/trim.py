"""Keep the inflight file small enough to ride along on every turn.

The file is read into the agent's context at session start (and on Hermes
every turn once referenced), so its size is a recurring token bill. Left
alone it grows without bound: every session appends, nobody deletes.

Policy, applied to `## Right now`:
  1. age:    entries dated older than --days are archived
  2. budget: while the section is over --max-bytes or --max-lines, the oldest
             remaining entry is archived (undated counts as oldest; on a tie
             the one lower in the file goes first, since writers prepend)
  3. floor:  the newest --min-entries always stay, whatever the budget says

Other sections: kept when their header (or first 400 chars) carries a date
inside --days, archived otherwise. A SECOND `## Right now` is a stale tracker
nobody archived and goes to the archive whole.

Nothing is deleted. Archived text lands in inflight-archive/inflight-<stamp>.md
next to the file. Idempotent: a second run over its own output is a no-op and
writes no archive. Default is a dry run.

If the file changes between read and write (another session saved it), the
write is refused with exit 3; re-run.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from . import core, paths

DEFAULT_DAYS = 7
DEFAULT_MAX_BYTES = 24_000  # ~6k tokens
DEFAULT_MAX_LINES = 80
DEFAULT_MIN_ENTRIES = 3


def _sort_key(indexed: Tuple[int, core.Entry]):
    i, e = indexed
    # oldest first: undated < any date; same date -> lower in file first
    return (e.date or date.min, -i)


def plan(text: str, today: date, days: int, max_bytes: int, max_lines: int,
         min_entries: int) -> Tuple[str, List[str]]:
    """Return (new_text, archived_blocks). Pure function; no I/O."""
    cutoff = today - timedelta(days=days)
    sections = core.parse(text)
    archived: List[str] = []
    seen_rn = False
    kept_sections: List[core.Section] = []

    for s in sections:
        if s.is_right_now and not seen_rn:
            seen_rn = True
            keep: List[Tuple[int, core.Entry]] = []
            for i, e in enumerate(s.entries):
                if e.date is not None and e.date < cutoff:
                    archived.append(e.text)
                else:
                    keep.append((i, e))

            def size(items):
                sec = core.Section(s.header, s.lead, [e for _, e in items])
                r = sec.render()
                return len(r.encode()), r.count("\n")

            protected = {i for i, _ in sorted(keep, key=_sort_key)[-min_entries:]} if min_entries > 0 else set()
            evictable = [x for x in sorted(keep, key=_sort_key) if x[0] not in protected]
            b, n = size(keep)
            while (b > max_bytes or n > max_lines) and evictable:
                victim = evictable.pop(0)
                keep.remove(victim)
                archived.append(victim[1].text)
                b, n = size(keep)
            keep.sort(key=lambda x: x[0])
            kept_sections.append(core.Section(s.header, s.lead, [e for _, e in keep]))
            continue

        if s.header == "":
            kept_sections.append(s)
        elif s.is_right_now:
            archived.append(s.render().rstrip())
        else:
            d = core.parse_date(s.header) or core.parse_date(s.render()[:400])
            if d is not None and d >= cutoff:
                kept_sections.append(s)
            else:
                archived.append(s.render().rstrip())

    return core.render(kept_sections), archived


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight trim", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=paths.env_int("INFLIGHT_DAYS", DEFAULT_DAYS))
    ap.add_argument("--max-bytes", type=int, default=paths.env_int("INFLIGHT_MAX_BYTES", DEFAULT_MAX_BYTES))
    ap.add_argument("--max-lines", type=int, default=paths.env_int("INFLIGHT_MAX_LINES", DEFAULT_MAX_LINES))
    ap.add_argument("--min-entries", type=int, default=paths.env_int("INFLIGHT_MIN_ENTRIES", DEFAULT_MIN_ENTRIES))
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--file", "--path", dest="file", type=Path, default=None)
    ap.add_argument("--today", help=argparse.SUPPRESS)  # tests pin the clock
    args = ap.parse_args(argv)

    path = (args.file or paths.inflight_file()).expanduser()
    if not path.exists():
        print(f"no inflight file at {path}")
        return 0

    before_stat = path.stat()
    original = path.read_text(encoding="utf-8")
    today = date.fromisoformat(args.today) if args.today else date.today()
    new_text, archived = plan(original, today, args.days, args.max_bytes, args.max_lines, args.min_entries)

    before_b, after_b = len(original.encode()), len(new_text.encode())
    print(f"{path}: {before_b:,}B (~{before_b // 4:,} tok) -> {after_b:,}B (~{after_b // 4:,} tok)")
    print(f"{len(archived)} block(s) to archive")
    if not archived and new_text == original:
        print("already trimmed — nothing to do")
        return 0
    if not args.apply:
        print("(dry run — pass --apply to write)")
        return 0

    now = path.stat()
    if (now.st_mtime_ns, now.st_size) != (before_stat.st_mtime_ns, before_stat.st_size):
        print(f"REFUSED: {path} changed while trimming (another session wrote it); re-run", file=sys.stderr)
        return 3

    if archived:
        adir = paths.archive_dir(path)
        adir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = adir / f"inflight-{stamp}.md"
        n = 1
        while dest.exists():
            dest = adir / f"inflight-{stamp}-{n}.md"
            n += 1
        dest.write_text(f"# Archived from {path.name} on {datetime.now():%Y-%m-%d %H:%M}\n\n"
                        + "\n\n".join(archived) + "\n", encoding="utf-8")
        print(f"archived -> {dest}")
    atomic_write(path, new_text)
    print(f"wrote {path}")
    return 0
