#!/usr/bin/env python3
"""Derived state: progress, lifecycle, children, entry ids, done, trim order.
Pure-function tests plus CLI arms against a temp home."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
sys.path.insert(0, str(ROOT / "src"))

from agent_inflight import progress as pg  # noqa: E402

E = ("**2026-09-30 19:10 [session S] — x.** prose\n- [x] a\n- [ ] b\n- [~] c\n"
     "```\n- [ ] not counted\nstatus: done 2026-01-01\n```\n  * [X] d")


def run(home: Path, *args: str, sid: str = "", env_extra=None) -> "tuple[int, str]":
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID",
                        "INFLIGHT_SESSION_ID", "CLAUDE_SESSION_ID")}
    env.update({"INFLIGHT_HOME": str(home), "HERMES_ROOT": str(home), "INFLIGHT_SESSION_ID": sid,
                "HOME": str(home)}, **(env_extra or {}))
    p = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env, check=False)
    return p.returncode, p.stdout + p.stderr


def state_db(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text, title text, started_at real, ended_at real, "
                "end_reason text, last_activity_at real, parent_session_id text, source text)")
    con.executemany("insert into sessions values (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class Progress(unittest.TestCase):
    def test_counts_skip_head_and_fences(self):
        p = pg.progress(E)
        self.assertEqual((p.done, p.open, p.blocked, p.total), (2, 1, 1, 4))
        self.assertEqual(p.label(), "2/4 (1 blocked)")
        self.assertEqual(pg.progress("**2026 — - [ ] in the head.**").total, 0)

    def test_status_inside_fence_ignored(self):
        self.assertEqual(pg.lifecycle(E).state, "active")

    def test_lifecycle_roundtrip(self):
        t = pg.set_status(E, "done", date(2026, 10, 2))
        lc = pg.lifecycle(t)
        self.assertEqual((lc.state, lc.since), ("done", date(2026, 10, 2)))
        self.assertIn("status: done 2026-01-01", t)  # fenced line untouched
        back = pg.set_status(t, "active", date(2026, 10, 3))
        self.assertEqual(pg.lifecycle(back).state, "active")
        self.assertEqual(back.count("status:"), 1)  # only the fenced one remains

    def test_stale_pause_idempotent_never_deletes(self):
        t, changed = pg.reconcile_stale(E, date(2026, 9, 25), date(2026, 9, 30), 3)
        self.assertTrue(changed)
        self.assertIn("status: paused (stale since 2026-09-30)", t)
        self.assertIn("- [ ] b", t)
        self.assertEqual(pg.reconcile_stale(t, date(2026, 9, 1), date(2026, 10, 30), 3), (t, False))

    def test_recent_session_activity_keeps_active(self):
        touched = pg.last_touched(date(2026, 9, 1), time.time())
        self.assertFalse(pg.reconcile_stale(E, touched, date.today(), 3)[1])

    def test_summary(self):
        self.assertEqual(pg.summary(E), "2/4 (1 blocked)")
        self.assertEqual(pg.summary("**2026-09-30 — y.**\nstatus: done 2026-10-01"), "done")


class SessionsProgress(unittest.TestCase):
    def test_sessions_shows_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = time.time()
            state_db(home / "state.db", [("A", "t", now - 60, None, None, now - 30, None, "tui")])
            (home / "inflight.md").write_text("## Right now\n\n**2026-09-30 [session A] — work.** p\n"
                                              "- [x] one\n- [ ] two\n- [~] three\n")
            rc, out = run(home, "sessions", "--json", sid="A")
            row = json.loads(out)["entries"][0]
            self.assertEqual(row["progress"], {"done": 1, "open": 1, "blocked": 1, "total": 3})
            self.assertEqual(row["state"], "active")
            rc, out = run(home, "sessions", sid="A")
            self.assertIn("progress 1/3 (1 blocked)", out)


class Children(unittest.TestCase):
    def test_children_exclude_compression_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = time.time()
            state_db(home / "state.db", [
                ("P", "parent", now - 900, now - 500, "compression", now - 500, None, "tui"),
                ("SUB1", "Subagent: a", now - 800, now - 700, "agent_close", now - 700, "P", "subagent"),
                ("C", "parent #2", now - 500, None, None, now - 10, "P", "tui"),
                ("SUB2", "Subagent: b", now - 400, None, None, now - 20, "C", "desktop"),
                ("X", "other", now - 300, None, None, now - 5, None, "tui"),
            ])
            (home / "inflight.md").write_text("## Right now\n\n**2026-09-30 [session P] — work.** p\n")
            rc, out = run(home, "sessions", "--json", "--children")
            row = json.loads(out)["entries"][0]
            self.assertEqual(row["continued_as"], "C")
            self.assertEqual([k["id"] for k in row["children"]], ["SUB2", "SUB1"])
            rc, out = run(home, "sessions", "--json")
            self.assertNotIn("children", json.loads(out)["entries"][0])
            rc, out = run(home, "sessions", "--children")
            self.assertIn("kids  : 2 delegated", out)
            self.assertIn("ENDED   SUB1", out)

    def test_continuation_picks_post_end_child_not_latest_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = time.time()
            state_db(home / "state.db", [
                ("P", "p", now - 900, now - 500, "compression", now - 500, None, "tui"),
                ("C", "p #2", now - 500, None, None, now - 10, "P", "tui"),
                ("LATE", "Subagent: late", now - 499.5 - 2, None, None, now - 1, "P", "subagent"),
            ])
            (home / "inflight.md").write_text("## Right now\n\n**2026-09-30 [session P] — w.**\n")
            rc, out = run(home, "sessions", "--json", "--children")
            row = json.loads(out)["entries"][0]
            self.assertEqual(row["continued_as"], "C")
            self.assertEqual([k["id"] for k in row["children"]], ["LATE"])


class EntryIds(unittest.TestCase):
    def test_parse_id_forms(self):
        from agent_inflight import core
        for head, sid, eid in (("**2026-09-30 [session S #a1b2c3] — x.**", "S", "a1b2c3"),
                               ("**2026-09-30 [session S] — old style.**", "S", None),
                               ("**2026-09-30 [#a1b2c3] — untagged.**", None, "a1b2c3"),
                               ("**2026-09-30 [session $HERMES_SESSION_ID #a1b2c3] — lit.**", None, "a1b2c3")):
            e = core.Entry(head)
            self.assertEqual((e.session, e.id), (sid, eid), head)

    def test_add_ids_unique(self):
        from agent_inflight import core
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for _ in range(3):
                run(home, "add", "same headline", sid="S")
            rn = core.right_now(core.parse((home / "inflight.md").read_text()))
            ids = [e.id for e in rn.entries]
            self.assertEqual(len(ids), 3)
            self.assertEqual(len(set(ids)), 3)
            self.assertTrue(all(i and len(i) >= 6 for i in ids))

    def test_strict_parser_bold_body(self):
        from agent_inflight import core
        t = "## Right now\n\n**2026-09-30 [session S] — a.** x\n\n**Note:** detail\n\n**2026-09-29 — b.**\n"
        rn = core.right_now(core.parse(t))
        self.assertEqual(len(rn.entries), 2)
        self.assertIn("**Note:** detail", rn.entries[0].text)
        self.assertEqual(core.render(core.parse(t)), core.render(core.parse(core.render(core.parse(t)))))

    def test_check_flags_stray_bold_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "inflight.md").write_text("## Right now\n\n**not dated** x\n\n**2026-09-30 — ok.**\n")
            rc, out = run(home, "check")
            self.assertEqual(rc, 1)
            self.assertIn("bold line(s) before the first dated entry", out)


class DoneAndTrim(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.home = Path(self._t.name)
        self.f = self.home / "inflight.md"
        self.f.write_text("## Right now\n\n"
                          "**2026-09-30 10:00 [session S #aaaaaa] — push foo.** x\n- [x] a\n\n"
                          "**2026-09-29 10:00 [session S #bbbbbb] — push bar.** y\n\n"
                          "**2026-09-20 10:00 [session T #cccccc] — old work.** z\n")

    def tearDown(self):
        self._t.cleanup()

    def test_done_by_id_and_substring(self):
        rc, out = run(self.home, "done", "#bbbbbb", "--today", "2026-09-30")
        self.assertEqual(rc, 0, out)
        self.assertIn("status: done 2026-09-30", self.f.read_text())
        rc, out = run(self.home, "done", "foo", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would mark done", out)
        self.assertEqual(self.f.read_text().count("status: done"), 1)

    def test_done_ambiguous_and_missing(self):
        rc, out = run(self.home, "done", "push")
        self.assertEqual(rc, 2)
        self.assertIn("#aaaaaa", out)
        self.assertIn("#bbbbbb", out)
        rc, _ = run(self.home, "done", "nonesuch")
        self.assertEqual(rc, 1)

    def test_reopen(self):
        run(self.home, "done", "aaaaaa", "--today", "2026-09-30")
        rc, out = run(self.home, "done", "aaaaaa", "--reopen")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("status:", self.f.read_text())
        rc, out = run(self.home, "done", "aaaaaa", "--reopen")
        self.assertIn("unchanged", out)

    def test_trim_order_and_never_age_archive_active(self):
        from agent_inflight import trim
        run(self.home, "done", "bbbbbb", "--today", "2026-09-28")
        text = self.f.read_text()
        p = trim.plan(text, date(2026, 9, 30), 7, 10**9, 10**9, 0)
        self.assertTrue(any("push bar" in a for a in p.archived))       # done past grace -> archived
        self.assertIn("old work", p.text)                               # 10 days old, active -> kept
        self.assertTrue(any("old work" in h for h in p.paused))         # ...but paused
        self.assertIn("status: paused (stale since 2026-09-30)", p.text)
        # under budget pressure paused goes before active
        p2 = trim.plan(p.text, date(2026, 9, 30), 7, len(p.text.encode()) - 1, 10**9, 0)
        self.assertTrue(any("old work" in a for a in p2.archived))
        self.assertIn("push foo", p2.text)

    def test_done_within_grace_kept(self):
        from agent_inflight import trim
        run(self.home, "done", "aaaaaa", "--today", "2026-09-30")
        p = trim.plan(self.f.read_text(), date(2026, 9, 30), 7, 10**9, 10**9, 0)
        self.assertIn("push foo", p.text)

    def test_session_activity_keeps_entry_active(self):
        from agent_inflight import trim
        p = trim.plan(self.f.read_text(), date(2026, 9, 30), 7, 10**9, 10**9, 0, activity={"T": time.time()})
        self.assertFalse(any("old work" in h for h in p.paused))

    def test_cli_trim_reads_backend_activity(self):
        """Through the real CLI: T's entry is 10 days old; with T active now it stays active,
        without a backend it is paused."""
        rc, out = run(self.home, "trim", "--today", "2026-09-30")
        self.assertIn("pause (stale 3d+): 2026-09-20 10:00 [session T", out)
        now = time.time()
        state_db(self.home / "state.db", [("T", "t", now - 99, None, None, now - 5, None, "tui")])
        rc, out = run(self.home, "trim", "--today", date.today().isoformat())
        self.assertNotIn("pause (stale 3d+): 2026-09-20 10:00 [session T", out)

    def test_done_refuses_on_concurrent_change(self):
        # exercise the stat check through the module (CLI can't be raced deterministically)
        from agent_inflight import entries as en
        real = Path.stat
        n = {"i": 0}

        def stat(p, *a, **k):
            st = real(p, *a, **k)
            if Path(p) == self.f:
                n["i"] += 1
                if n["i"] == 3:
                    os.utime(self.f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
                    return real(p, *a, **k)
            return st
        Path.stat = stat
        try:
            rc = en.done_main(["aaaaaa", "--file", str(self.f)])
        finally:
            Path.stat = real
        self.assertEqual(rc, 3)
        self.assertNotIn("status:", self.f.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
