"""Keep the inflight file small enough to ride along on every turn.

The file is read into the agent's context at session start (and on Hermes
every turn once referenced), so its size is a recurring token bill. Left
alone it grows without bound: every session appends, nobody deletes.

Policy, applied to `## Right now`. Each entry has a lifecycle state (see
progress.py): active (default), paused, done.

  1. stale:  an ACTIVE entry untouched for --stale-days becomes paused (a
             `status: paused (stale since D)` line is added). Touched = the
             newer of its head date and its session's last activity, so a
             session still at work keeps its entry active.
  2. done:   DONE entries older than --done-grace-days are archived.
  3. budget: while over --max-bytes or --max-lines, archive in this order:
             done (oldest first), then paused (longest-paused first), then
             active (oldest first). The newest --min-entries entries that
             are not done are never archived for budget.
  Active entries are NEVER archived by age; age alone only pauses them.
  An entry started before 0.2.0 has no status and counts as active.

Other sections: kept when their header (or first 400 chars) carries a date
inside --days, archived otherwise. A SECOND `## Right now` is a stale tracker
nobody archived and goes to the archive whole.

Nothing is deleted. Archived text lands in inflight-archive/inflight-<stamp>.md
next to the file. Idempotent: a second run over its own output is a no-op and
writes no archive. Default is a dry run; it prints what would change.

If the file changes between read and write (another session saved it), the
write is refused with exit 3; re-run.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import core, paths, progress, safety

DEFAULT_DAYS = 7
DEFAULT_MAX_BYTES = 24_000  # ~6k tokens
DEFAULT_MAX_LINES = 80
DEFAULT_MIN_ENTRIES = 3
DEFAULT_STALE_DAYS = 3
DEFAULT_DONE_GRACE_DAYS = 1


@dataclass
class Plan:
    text: str
    archived: List[str] = field(default_factory=list)
    paused: List[str] = field(default_factory=list)  # heads newly marked paused


def _order(e: "core.Entry", i: int):
    """Budget eviction order: done < paused < active; oldest first within a class;
    on a tie the one lower in the file goes first (writers prepend)."""
    lc = progress.lifecycle(e.text)
    rank = {"done": 0, "paused": 1}.get(lc.state, 2)
    when = (lc.since if lc.state == "paused" and lc.since else None) or e.date or date.min
    return (rank, when, -i)


def plan(text: str, today: date, days: int, max_bytes: int, max_lines: int, min_entries: int, *,
         stale_days: int = DEFAULT_STALE_DAYS, done_grace_days: int = DEFAULT_DONE_GRACE_DAYS,
         activity: Optional[Dict[str, float]] = None) -> Plan:
    """Pure function; no I/O. `activity` maps session id -> last activity (epoch s)."""
    cutoff = today - timedelta(days=days)
    done_cutoff = today - timedelta(days=done_grace_days)
    activity = activity or {}
    sections = core.parse(text)
    out = Plan(text)
    seen_rn = False
    kept_sections: List[core.Section] = []

    for s in sections:
        if s.is_right_now and not seen_rn:
            seen_rn = True
            keep: List[Tuple[int, core.Entry]] = []
            for i, e in enumerate(s.entries):
                lc = progress.lifecycle(e.text)
                if lc.state == "done":
                    if (lc.since or e.date or date.min) < done_cutoff:
                        out.archived.append(e.text)
                        continue
                elif lc.state == "active":
                    touched = progress.last_touched(e.date, activity.get(e.session or ""))
                    new_text, changed = progress.reconcile_stale(e.text, touched, today, stale_days)
                    if changed:
                        e = core.Entry(new_text)
                        out.paused.append(e.head)
                keep.append((i, e))

            def size(items, s=s):
                sec = core.Section(s.header, s.lead, [e for _, e in items])
                r = sec.render()
                return len(r.encode()), r.count("\n")

            live = [x for x in keep if progress.lifecycle(x[1].text).state != "done"]
            newest = sorted(live, key=lambda x: (x[1].date or date.min, -x[0]))
            protected = {i for i, _ in newest[-min_entries:]} if min_entries > 0 else set()
            evictable = sorted((x for x in keep if x[0] not in protected), key=lambda x: _order(x[1], x[0]))
            b, n = size(keep)
            while (b > max_bytes or n > max_lines) and evictable:
                victim = evictable.pop(0)
                keep.remove(victim)
                out.archived.append(victim[1].text)
                b, n = size(keep)
            keep.sort(key=lambda x: x[0])
            kept_sections.append(core.Section(s.header, s.lead, [e for _, e in keep]))
            continue

        if s.header == "":
            kept_sections.append(s)
        elif s.is_right_now:
            out.archived.append(s.render().rstrip())
        else:
            d = core.parse_date(s.header) or core.parse_date(s.render()[:400])
            if d is not None and d >= cutoff:
                kept_sections.append(s)
            else:
                out.archived.append(s.render().rstrip())

    out.text = core.render(kept_sections)
    return out


def session_activity(text: str) -> Dict[str, float]:
    """Last activity per tagged session, from the session backend (read-only).
    Empty without a backend: staleness then runs on head dates alone."""
    from . import sessions
    be = sessions.backend()
    if be is None:
        return {}
    rn = core.right_now(core.parse(text))
    acts: Dict[str, float] = {}
    for e in (rn.entries if rn else []):
        if e.session and e.session not in acts:
            info = be.lookup(e.session)
            if info and info.get("last_activity_at"):
                acts[e.session] = float(info["last_activity_at"])
    return acts


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight trim", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=paths.env_int("INFLIGHT_DAYS", DEFAULT_DAYS))
    ap.add_argument("--max-bytes", type=int, default=paths.env_int("INFLIGHT_MAX_BYTES", DEFAULT_MAX_BYTES))
    ap.add_argument("--max-lines", type=int, default=paths.env_int("INFLIGHT_MAX_LINES", DEFAULT_MAX_LINES))
    ap.add_argument("--min-entries", type=int, default=paths.env_int("INFLIGHT_MIN_ENTRIES", DEFAULT_MIN_ENTRIES))
    ap.add_argument("--stale-days", type=int, default=paths.env_int("INFLIGHT_STALE_DAYS", DEFAULT_STALE_DAYS))
    ap.add_argument("--done-grace-days", type=int,
                    default=paths.env_int("INFLIGHT_DONE_GRACE_DAYS", DEFAULT_DONE_GRACE_DAYS))
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
    p = plan(original, today, args.days, args.max_bytes, args.max_lines, args.min_entries,
             stale_days=args.stale_days, done_grace_days=args.done_grace_days,
             activity=session_activity(original))
    new_text, archived = p.text, p.archived

    before_b, after_b = len(original.encode()), len(new_text.encode())
    print(f"{path}: {before_b:,}B (~{before_b // 4:,} tok) -> {after_b:,}B (~{after_b // 4:,} tok)")
    for h in p.paused:
        print(f"  pause (stale {args.stale_days}d+): {' '.join(h.replace('**', '').split())[:100]}")
    for a in archived:
        print(f"  archive: {' '.join(a.split(chr(10), 1)[0].replace('**', '').split())[:100]}")
    print(f"{len(p.paused)} entr{'y' if len(p.paused) == 1 else 'ies'} to pause, {len(archived)} block(s) to archive")
    if not archived and new_text == original:
        print("already trimmed — nothing to do")
        return 0
    if not args.apply:
        print("(dry run — pass --apply to write)")
        return 0

    try:
        with safety.locked(path):
            now = path.stat()
            if (now.st_mtime_ns, now.st_size) != (before_stat.st_mtime_ns, before_stat.st_size):
                print(f"REFUSED: {path} changed while trimming (another session wrote it); re-run",
                      file=sys.stderr)
                return 3
            if archived:
                adir = paths.archive_dir(path)
                safety.ensure_private_dir(adir)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                body = (f"# Archived from {path.name} on {datetime.now():%Y-%m-%d %H:%M}\n\n"
                        + "\n\n".join(archived) + "\n")
                n = 0
                while True:
                    dest = adir / (f"inflight-{stamp}.md" if n == 0 else f"inflight-{stamp}-{n}.md")
                    try:
                        safety.create_private(dest, body)
                        break
                    except FileExistsError:
                        n += 1
                print(f"archived -> {dest}")
            safety.write_private(path, new_text)
    except safety.LockTimeout as e:
        print(f"REFUSED: {e}; re-run", file=sys.stderr)
        return 3
    print(f"wrote {path}")
    return 0
