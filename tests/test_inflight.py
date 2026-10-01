#!/usr/bin/env python3
"""Arms for agent-inflight. Stdlib only; every arm runs the real CLI as a
subprocess against a synthetic home (never the user's real files)."""
from __future__ import annotations

import json
import re
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
sys.path.insert(0, str(ROOT / "src"))
from agent_inflight import core, trim  # noqa: E402

fails = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f": {detail}" if not cond and detail else ""))
    fails += 0 if cond else 1


def run(home: Path, *args: str, sid: str = "", stdin: str = "") -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID",
                        "INFLIGHT_SESSION_ID", "CLAUDE_SESSION_ID")}
    env.update({"INFLIGHT_HOME": str(home), "HERMES_ROOT": str(home), "INFLIGHT_SESSION_ID": sid,
                "HOME": str(home)})
    p = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env,
                       input=stdin, check=False)
    return p.returncode, p.stdout + p.stderr


def state_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text, title text, started_at real, ended_at real, "
                "end_reason text, last_activity_at real, parent_session_id text)")
    con.executemany("insert into sessions values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


print("parser")
t = ("# pre\n\n## Right now\n\n**2026-09-30 [session s1] — a.** x\n"
     "**2026-09-29 — b.** y\ncontinued\n\n**untagged para**\n\n## Parked\nstuff\n")
secs = core.parse(t)
rn = core.right_now(secs)
check(rn is not None and len(rn.entries) == 2, "2 entries, incl. one with no blank line before it",
      str([e.head for e in rn.entries]) if rn else "none")
check("**untagged para**" in rn.entries[1].text, "undated bold paragraph stays inside its entry (strict parser)")
check(rn.entries[0].session == "s1" and rn.entries[1].session is None, "tag parsed per entry")
check("continued" in rn.entries[1].text, "continuation line stays with its entry")
check(core.parse(core.render(secs)) == secs or core.render(core.parse(core.render(secs))) == core.render(secs),
      "render is stable (parse∘render fixed point)")

print("trim (pure)")
today = core.parse_date("2026-09-30")
old = "\n\n".join(f"**2026-09-{d:02d} [session s{d}] — e{d}.** " + "x" * 300 for d in range(30, 0, -1))
text = f"## Right now\n\n{old}\n\n## Undated\nold stuff\n\n## 2026-09-29 notes\nfresh\n"
new, arch = trim.plan(text, today, days=7, max_bytes=10**9, max_lines=10**9, min_entries=3)
kept = core.right_now(core.parse(new)).entries
check(all(e.date >= core.parse_date("2026-09-23") for e in kept) and len(kept) == 8, "age: keeps 8 in-window",
      str(len(kept)))
check("## Undated" not in new and "## 2026-09-29 notes" in new, "undated section archived, dated kept")
new2, _ = trim.plan(text, today, days=365, max_bytes=2000, max_lines=10**9, min_entries=3)
kept2 = core.right_now(core.parse(new2)).entries
check(len(new2.encode()) <= 2000 + 400 and [e.session for e in kept2][:1] == ["s30"],
      "budget: evicts oldest, newest stays", f"{len(new2.encode())}B {[e.session for e in kept2]}")
new3, _ = trim.plan(text, today, days=365, max_bytes=10, max_lines=10**9, min_entries=3)
check(len(core.right_now(core.parse(new3)).entries) == 3, "floor: min_entries survive an impossible budget")
again, arch_again = trim.plan(new, today, 7, 10**9, 10**9, 3)
check(again == new and not arch_again, "idempotent on its own output")
dup = "## Right now\n\n**2026-09-30 — live.**\n\n## Right now\n\n**2026-09-30 — stale copy.**\n"
nd, ad = trim.plan(dup, today, 7, 10**9, 10**9, 3)
check("stale copy" not in nd and any("stale copy" in a for a in ad), "second Right now archived whole")
und = ("## Right now\n\n**2026-09-30 — newer.** " + "x" * 500 + "\n\n**2026-09-29 — older.** " + "y" * 500 + "\n")
nu, _ = trim.plan(und, today, 7, 700, 10**9, 0)
check("newer" in nu and "older" not in nu, "older entry evicted before newer under budget")
lead_only = "## Right now\n\n**no date here** text\n"
check(trim.plan(lead_only, today, 7, 10, 10**9, 0)[0] == lead_only, "undated bold lead is not an entry; trim leaves it")
blocks_in = sum(len(s.entries) for s in core.parse(text) if s.is_right_now)
blocks_out = len(kept) + sum(1 for a in arch if a.startswith("**"))
check(blocks_in == blocks_out, "count in == count out (no entry lost)", f"{blocks_in} vs {blocks_out}")

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    f = home / "inflight.md"

    print("init / add / check")
    rc, out = run(home, "init")
    check(rc == 0 and f.is_file(), "init creates file")
    rc, out = run(home, "trim")
    check("already trimmed" in out, "fresh template is already trimmed (nothing archived)", out)
    rc, out = run(home, "init")
    check(rc == 0 and "left alone" in out, "init never overwrites")
    rc, out = run(home, "add", "thing: committed, NOT pushed", "next: push", sid="20260930_000000_abcdef")
    body = f.read_text()
    check(rc == 0 and re.search(r"\[session 20260930_000000_abcdef #[0-9a-f]{6}\] — thing: committed, NOT pushed\.\*\* next: push",
                                body) is not None, "add writes dated tagged entry with an entry id", body[-300:])
    run(home, "add", "second", sid="S2")
    rn = core.right_now(core.parse(f.read_text()))
    check(rn.entries[0].session == "S2" and len(rn.entries) == 2, "add prepends (newest first)")
    check("# In-flight work" in f.read_text(), "add keeps the preamble")
    rc, out = run(home, "add", stdin="from stdin\nbody line")
    check(rc == 0 and "untagged" in out and "from stdin." in f.read_text(), "stdin + untagged warning")
    rc, out = run(home, "check")
    check(rc == 0 and "OK" in out, "check clean", out)
    f.write_text(f.read_text().replace("\n## Right now\n", "\n## Right now\n\n**2026-09-30 [session $HERMES_SESSION_ID] — lit.**\n", 1))
    rc, out = run(home, "check")
    check(rc == 1 and "unexpanded" in out, "check flags literal $VAR tag", out)

    print("trim CLI")
    f.write_text(f"## Right now\n\n{old}\n")
    rc, out = run(home, "trim", "--today", "2026-09-30")
    check(rc == 0 and "dry run" in out and f.read_text().count("**2026") == 30, "dry run writes nothing")
    rc, out = run(home, "trim", "--apply", "--today", "2026-09-30")
    archives = list((home / "inflight-archive").glob("*.md"))
    check(rc == 0 and len(archives) == 1 and f.read_text().count("**2026") == 8, "apply archives + writes", out)
    rc, out = run(home, "trim", "--apply", "--today", "2026-09-30")
    check("already trimmed" in out and len(list((home / "inflight-archive").glob("*.md"))) == 1,
          "second apply is a no-op, no new archive")
    total = archives[0].read_text().count("**2026") + f.read_text().count("**2026")
    check(total == 30, "archive + file hold all 30 entries", str(total))

    print("sessions (hermes backend)")
    now = time.time()
    A, I, E, P, C, K = (f"20260927_0000{n}0_aaaaa{n}" for n in range(6))
    state_db(home / "state.db", [
        (A, "active", now - 600, None, None, now - 60, None),
        (I, "idle", now - 9000, None, None, now - 7200, None),
        (E, "ended", now - 9000, now - 8000, "agent_close", now - 8000, None),
        (P, "parent", now - 9000, now - 8000, "compression", now - 8000, None),
        (C, "child", now - 8000, None, None, now - 30, P),
    ])
    state_db(home / "profiles" / "ksai" / "state.db", [(K, "work", now - 9000, now - 100, "cli_close", now - 100, None)])
    tags = [A, I, E, P, K, "20990101_000000_ffffff"]
    body = "\n\n".join(f"**2026-09-27 [session {t}] — entry {t[-1]}**" for t in tags)
    f.write_text(f"## Right now\n{body}\n\n**2026-09-27 — untagged entry**\n\n## Later\n**x [session {A}]**\n")
    rc, out = run(home, "sessions", "--json", sid=A)
    d = json.loads(out)
    st = {e["session"]: e for e in d["entries"]}
    check(len(d["entries"]) == 6 and d["untagged"] == 1, "6 tagged + 1 untagged; only § Right now")
    check(st[A]["status"] == "ACTIVE" and st[A]["this_session"], "ACTIVE, this session")
    check(st[I]["status"] == "IDLE", "IDLE")
    check(st[E]["status"] == "ENDED" and st[E]["end_reason"] == "agent_close", "ENDED + reason")
    check(st[P]["status"] == "ACTIVE" and st[P].get("continued_as") == C, "compression parent -> live child")
    check(st[K]["status"] == "ENDED" and st[K]["profile"] == "ksai", "secondary profile db")
    check(st["20990101_000000_ffffff"]["status"] == "UNKNOWN", "UNKNOWN")
    rc, out = run(home, "sessions", sid=A)
    check(f"lcm_load_session(session_id='{C}')" in out, "drill targets continuing child")
    check(out.count("do not resume") == 1 and f"--resume {A}" not in out, "resume warning only on ACTIVE sibling")
    check(f"hermes -p ksai --resume {K}" in out, "resume carries profile flag")
    rc, out = run(home, "me", sid=A)
    check(rc == 0 and out.strip() == f"[session {A}]", "me prints tag")
    rc, _ = run(home, "me", sid="")
    check(rc == 1, "me outside a session exits 1")

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    (home / "inflight.md").write_text("## Right now\n\n**2026-09-30 [session x1] — e.**\n")
    rc, out = run(home, "sessions", "--json")
    d = json.loads(out)
    check(d["backend"] is None and d["entries"][0]["status"] == "NO-BACKEND", "no state.db -> NO-BACKEND, still lists")
    rc, _ = run(home, "sessions", "--file", str(home / "missing.md"))
    check(rc == 2, "missing file exits 2")

print(f"{'FAIL' if fails else 'PASS'} ({fails} failing)")
sys.exit(1 if fails else 0)
