"""init / add / check: create the file, write a tagged entry, lint it."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import core, paths, progress, safety, trim

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
    try:
        safety.create_private(path, TEMPLATE.read_text(encoding="utf-8"))
    except FileExistsError:
        print(f"{path} exists, left alone")
        return 0
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
    ap.add_argument("--force", action="store_true",
                    help="write even if the text looks like a credential (false positive)")
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
    problems = safety.headline_problems(headline) + safety.body_problems(body or "")
    if problems:
        print("REFUSED: " + "; ".join(problems) + " (would forge or corrupt an entry)", file=sys.stderr)
        return 4
    kinds = safety.secret_kinds(f"{headline}\n{body or ''}")
    if kinds and not args.force:
        print(f"REFUSED: text looks like a credential ({', '.join(kinds)}). Never put secrets in the "
              "tracker; rotate it if real. False positive: re-run with --force.", file=sys.stderr)
        return 5

    path = (args.file or paths.inflight_file()).expanduser()
    if not path.exists():
        init_main(["--file", str(path)])
    sid = args.session if args.session is not None else paths.session_id()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Prepending doesn't depend on what's below, so a write that races us
    # (an editor that ignores the lock) is retried, not refused.
    try:
        with safety.locked(path):
            for _attempt in range(3):
                before = path.stat()
                sections = core.parse(path.read_text(encoding="utf-8"))
                taken = {e.id for s in sections for e in s.entries if e.id}
                eid = core.new_id(f"{stamp}|{sid}|{headline}|{body}", taken)
                tag = f" [session {sid} #{eid}]" if sid else f" [#{eid}]"
                entry = core.Entry(f"**{stamp}{tag} — {headline}**" + (f" {body.strip()}" if body.strip() else ""))
                rn = core.right_now(sections)
                if rn is None:
                    rn = core.Section(core.RIGHT_NOW)
                    idx = 1 if sections and sections[0].header == "" else 0
                    sections.insert(idx, rn)
                rn.entries.insert(0, entry)
                now = path.stat()
                if (now.st_mtime_ns, now.st_size) == (before.st_mtime_ns, before.st_size):
                    safety.write_private(path, core.render(sections))
                    break
            else:
                print("REFUSED: file kept changing while writing; re-run", file=sys.stderr)
                return 3
    except safety.LockTimeout as e:
        print(f"REFUSED: {e}; re-run", file=sys.stderr)
        return 3
    if kinds:  # --force over a credential match: audit trail, kinds + id only, never the text
        from . import state
        state.log("secret-force", event="add", session=sid or None, entry_id=eid, kinds=kinds)
    print(f"added to {path}: {entry.head[:120]}")
    if not sid:
        print("note: untagged (no session id in env); `inflight sessions` cannot track it")
    return 0


def match(entries: "List[core.Entry]", needle: str) -> List[int]:
    """Indexes of entries matching `needle`: an exact entry id (`a1b2c3` or
    `#a1b2c3`) wins outright; otherwise a case-insensitive substring of the
    head (session id, date, headline words)."""
    n = needle.strip().lstrip("#")
    by_id = [i for i, e in enumerate(entries) if e.id and e.id == n.lower()]
    if by_id:
        return by_id
    low = needle.strip().lower()
    return [i for i, e in enumerate(entries) if low and low in e.head.lower()]


def done_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inflight done",
        description="Mark one `## Right now` entry done (or reopen it). Trim archives done entries "
                    "after the grace period. Exit 0 written, 1 no match, 2 ambiguous, 3 file changed.")
    ap.add_argument("match", help="entry id (a1b2c3), or a substring of the head: session id, date, headline words")
    ap.add_argument("--reopen", action="store_true", help="remove the status line (back to active)")
    ap.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--today", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    path = (args.file or paths.inflight_file()).expanduser()
    if not path.is_file():
        print(f"{path} not found (run `inflight init`)", file=sys.stderr)
        return 1
    try:
        with safety.locked(path):
            return _done_locked(path, args)
    except safety.LockTimeout as e:
        print(f"REFUSED: {e}; re-run", file=sys.stderr)
        return 3


def _done_locked(path: Path, args: argparse.Namespace) -> int:
    before = path.stat()
    sections = core.parse(path.read_text(encoding="utf-8"))
    rn = core.right_now(sections)
    hits = match(rn.entries if rn else [], args.match)
    if not hits:
        print(f"no entry in `## Right now` matches {args.match!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"{len(hits)} entries match {args.match!r}; use the entry id or more words:", file=sys.stderr)
        for i in hits:
            e = rn.entries[i]
            print(f"  #{e.id or '------'}  {' '.join(e.head.replace('**', '').split())[:100]}", file=sys.stderr)
        return 2
    e = rn.entries[hits[0]]
    today = date.fromisoformat(args.today) if args.today else date.today()
    state = "active" if args.reopen else "done"
    new_text = progress.set_status(e.text, state, today)
    label = " ".join(e.head.replace("**", "").split())[:100]
    if new_text == e.text:
        print(f"unchanged (already {state}): {label}")
        return 0
    if args.dry_run:
        print(f"would mark {state}: {label}")
        return 0
    e.text = new_text
    now = path.stat()
    if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
        print("REFUSED: file changed while writing; re-run", file=sys.stderr)
        return 3
    safety.write_private(path, core.render(sections))
    print(f"marked {state}: {label}")
    return 0


def lint(text: str, max_bytes: int = trim.DEFAULT_MAX_BYTES) -> Tuple[str, List[str]]:
    """(summary line, findings) for one tracker text. Shared by `inflight check`
    and the Hermes plugin, so both report the same thing."""
    sections = core.parse(text)
    findings: List[str] = []
    rns = [s for s in sections if s.is_right_now]
    if not rns:
        findings.append("no `## Right now` section")
    if len(rns) > 1:
        findings.append(f"{len(rns)} `## Right now` sections (stale copy; `inflight trim --apply` archives it)")
    size = len(text.encode())
    if size > max_bytes:
        findings.append(f"{size:,}B over budget {max_bytes:,}B (`inflight trim --apply`)")
    summary = f"{size:,}B (~{size // 4:,} tok)"
    if rns:
        entries = rns[0].entries
        undated = sum(1 for e in entries if e.date is None)
        untagged = sum(1 for e in entries if not e.session)
        literal = sum(1 for e in entries if core.LITERAL_TAG_RE.search(e.head))
        stray = sum(1 for line in rns[0].lead.split("\n") if line.startswith("**"))
        if stray:
            findings.append(f"{stray} bold line(s) before the first dated entry (not an entry; "
                            "start entries with `**YYYY-MM-DD`)")
        if undated:
            findings.append(f"{undated} entr{'y' if undated == 1 else 'ies'} with no date in the head")
        if literal:
            findings.append(f"{literal} entr{'y' if literal == 1 else 'ies'} tagged with an unexpanded "
                            "variable (`[session $...]`); write entries with `inflight add`")
        leaky = [e for e in entries if safety.secret_kinds(e.text)]
        if leaky:
            kinds = sorted({k for e in leaky for k in safety.secret_kinds(e.text)})
            ids = ", ".join(f"#{e.id}" if e.id else e.head[:40] for e in leaky)
            findings.append(f"{len(leaky)} entr{'y' if len(leaky) == 1 else 'ies'} with credential-looking "
                            f"text ({', '.join(kinds)}): {ids}. Remove it and rotate the credential if real")
        summary = f"{len(entries)} entries, {untagged} untagged, " + summary
    return summary, findings


def interpreter_warning(tracker: Path) -> str:
    """Non-fatal: install.sh pinned a venv but this process runs another
    Python (e.g. bin/inflight called directly), so plugins installed in the
    venv are invisible here."""
    venv = tracker.parent / "inflight-state" / "venv"
    if not (venv / "bin" / "python").exists():
        return ""
    try:
        if Path(sys.prefix).resolve() == venv.resolve():
            return ""
    except OSError:
        return ""
    return (f"running {sys.executable}, not the pinned venv {venv}; backend plugins installed there "
            "are not visible (run `inflight` from PATH, or re-run install.sh)")


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
    summary, findings = lint(path.read_text(encoding="utf-8"), args.max_bytes)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        findings.append(f"{path.name} is mode {mode:o}, readable by others (`chmod 600 {path}`)")
    warn = interpreter_warning(path)
    if warn:
        print(f"warn  {warn}")
    from . import audit
    ign = audit.ignore_branches()
    if ign:
        print(f"note  audit.ignore_branches hides branches matching: {', '.join(ign)}")
    print(summary)
    for f in findings:
        print(f"FAIL  {f}")
    if not findings:
        print("OK")
    return 1 if findings else 0
