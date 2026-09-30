"""init / add / check: create the file, write a tagged entry, lint it."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import core, paths, trim

TEMPLATE = (Path(__file__).resolve().parent / "template.md")


def init_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight init", description="Create the inflight file if missing.")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args(argv)
    path = (args.file or paths.inflight_file()).expanduser()
    if path.exists():
        print(f"{path} exists, left alone")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {path}")
    return 0


def add_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inflight add",
        description="Prepend a dated, session-tagged entry to `## Right now`. "
                    "Text from args, or stdin when no args.")
    ap.add_argument("headline", nargs="?", help="bolded one-line state, e.g. 'foo: committed, NOT pushed'")
    ap.add_argument("body", nargs="?", default="", help="optional detail after the headline")
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--session", default=None, help="override the session id")
    args = ap.parse_args(argv)

    headline = args.headline
    body = args.body
    if headline is None:
        raw = sys.stdin.read().strip()
        if not raw:
            ap.error("no headline given")
        headline, _, body = raw.partition("\n")
    headline = headline.strip().strip("*").strip()
    if not headline.endswith((".", "!", "?")):
        headline += "."

    path = (args.file or paths.inflight_file()).expanduser()
    if not path.exists():
        init_main(["--file", str(path)])
    sid = args.session if args.session is not None else paths.session_id()
    tag = f" [session {sid}]" if sid else ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = core.Entry(f"**{stamp}{tag} — {headline}**" + (f" {body.strip()}" if body.strip() else ""))

    before = path.stat()
    sections = core.parse(path.read_text(encoding="utf-8"))
    rn = core.right_now(sections)
    if rn is None:
        rn = core.Section(core.RIGHT_NOW)
        idx = 1 if sections and sections[0].header == "" else 0
        sections.insert(idx, rn)
    rn.entries.insert(0, entry)
    now = path.stat()
    if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
        print("REFUSED: file changed while writing; re-run", file=sys.stderr)
        return 3
    trim.atomic_write(path, core.render(sections))
    print(f"added to {path}: {entry.head[:120]}")
    if not sid:
        print("note: untagged (no session id in env); `inflight sessions` cannot track it")
    return 0


def check_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight check",
                                 description="Lint the file. Exit 1 on findings, 0 when clean.")
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--max-bytes", type=int, default=paths.env_int("INFLIGHT_MAX_BYTES", trim.DEFAULT_MAX_BYTES))
    args = ap.parse_args(argv)
    path = (args.file or paths.inflight_file()).expanduser()
    if not path.is_file():
        print(f"FAIL  {path} missing (run `inflight init`)")
        return 1
    text = path.read_text(encoding="utf-8")
    sections = core.parse(text)
    findings: List[str] = []
    rns = [s for s in sections if s.is_right_now]
    if not rns:
        findings.append("no `## Right now` section")
    if len(rns) > 1:
        findings.append(f"{len(rns)} `## Right now` sections (stale copy; `inflight trim --apply` archives it)")
    size = len(text.encode())
    if size > args.max_bytes:
        findings.append(f"{size:,}B over budget {args.max_bytes:,}B (`inflight trim --apply`)")
    if rns:
        entries = rns[0].entries
        undated = sum(1 for e in entries if e.date is None)
        untagged = sum(1 for e in entries if not e.session)
        literal = sum(1 for e in entries if "[session $" in e.head)
        if undated:
            findings.append(f"{undated} entr{'y' if undated == 1 else 'ies'} with no date in the head")
        if literal:
            findings.append(f"{literal} entr{'y' if literal == 1 else 'ies'} tagged with an unexpanded "
                            "variable (`[session $...]`); write entries with `inflight add`")
        print(f"{len(entries)} entries, {untagged} untagged, {size:,}B (~{size // 4:,} tok)")
    for f in findings:
        print(f"FAIL  {f}")
    if not findings:
        print("OK")
    return 1 if findings else 0
