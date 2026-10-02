#!/usr/bin/env python3
"""Hermes plugin + derived-state tests. Temp files and temp state.db only;
the user's real tracker is never read or written."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_inflight import core, retag  # noqa: E402

PLUGIN_INIT = ROOT / "adapters" / "hermes" / "plugin" / "__init__.py"


def load_plugin(name: str = "inflight_plugin_under_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE = """## Right now

**2026-09-30 19:10 [session OLD] — older entry.** body

**2026-09-30 18:00 [session TIP] — closed task.** body
status: done 2026-09-30

**2026-09-28 13:20 [session $HERMES_SESSION_ID] — foreign literal.** body
"""


class TmpHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.f = self.home / "inflight.md"
        self.f.write_text(BASE)
        self._env = {k: os.environ.get(k) for k in ("INFLIGHT_FILE", "INFLIGHT_HOME", "HERMES_HOME", "HERMES_ROOT")}
        os.environ.update(INFLIGHT_FILE=str(self.f), INFLIGHT_HOME=str(self.home),
                          HERMES_HOME=str(self.home), HERMES_ROOT=str(self.home))
        self.p = load_plugin()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def prepend(self, entry: str):
        t = self.f.read_text()
        self.f.write_text(t.replace("## Right now\n", f"## Right now\n\n{entry}\n", 1))
        # mtime resolution on some filesystems is coarse; force a visible change
        st = self.f.stat()
        os.utime(self.f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def call(self, tool, args, edit, sid="S-NEW", tcid="t1", result='{"ok": true}'):
        self.p.on_pre_tool_call(tool_name=tool, args=args, tool_call_id=tcid)
        edit()
        return self.p.on_transform_tool_result(tool_name=tool, args=args, result=result,
                                               session_id=sid, tool_call_id=tcid)


class RetagPlugin(TmpHome):
    NEW = "**2026-09-30 20:00 [session $HERMES_SESSION_ID] — mine.** x"

    def test_new_literal_entry_retagged_foreign_left(self):
        out = self.call("patch", {"path": str(self.f), "new_string": self.NEW}, lambda: self.prepend(self.NEW))
        text = self.f.read_text()
        self.assertIn("[session S-NEW] — mine", text)
        self.assertIn("[session $HERMES_SESSION_ID] — foreign literal", text)
        self.assertIn("retagged 1 new entry", out)

    def test_braced_and_bare_variants(self):
        for i, tag in enumerate(("[${HERMES_SESSION_ID}]", "[$HERMES_SESSION_ID]")):
            e = f"**2026-09-30 20:0{i} {tag} — v{i}.** x"
            self.call("write_file", {"path": str(self.f), "content": e}, lambda e=e: self.prepend(e), tcid=f"v{i}")
            self.assertIn(f"[session S-NEW] — v{i}", self.f.read_text())

    def test_unproven_entry_not_claimed(self):
        """Another session's concurrent entry shows up as new but is not in this call's text."""
        other = "**2026-09-30 20:05 [session $HERMES_SESSION_ID] — theirs.** y"
        out = self.call("patch", {"path": str(self.f), "new_string": "unrelated"}, lambda: self.prepend(other))
        self.assertIn("$HERMES_SESSION_ID] — theirs", self.f.read_text())
        self.assertIn("can't be shown to have written", out)

    def test_shell_edit_provenance(self):
        cmd = f"cat >> {self.f} <<'E'\n{self.NEW}\nE"
        self.call("terminal", {"command": cmd}, lambda: self.prepend(self.NEW))
        self.assertIn("[session S-NEW] — mine", self.f.read_text())

    def test_read_only_call_silent(self):
        out = self.call("terminal", {"command": f"cat {self.f}"}, lambda: None)
        self.assertIsNone(out)

    def test_unrelated_file_ignored(self):
        other = self.home / "x.md"
        self.assertIsNone(self.p.touch_provenance("write_file", {"path": str(other)}, self.f.resolve()))
        self.assertIsNone(self.call("write_file", {"path": str(other)}, lambda: other.write_text("hi")))

    def test_concurrent_write_not_clobbered(self):
        real_stat = self.p._stat
        n = {"i": 0}

        def racing(path):
            n["i"] += 1
            st = real_stat(path)
            # 1 = pre snapshot, 2 = post read, 3 = pre-write recheck -> another writer
            return (st[0] + 99, st[1]) if n["i"] == 3 else st
        self.p._stat = racing
        out = self.call("patch", {"path": str(self.f), "new_string": self.NEW}, lambda: self.prepend(self.NEW))
        self.assertIn("retag skipped", out)
        self.assertIn("$HERMES_SESSION_ID] — mine", self.f.read_text())

    def test_no_snapshot_no_action(self):
        self.assertIsNone(self.p.on_transform_tool_result(
            tool_name="patch", args={"path": str(self.f)}, result="{}", session_id="S", tool_call_id="never"))

    def test_missing_file_never_raises(self):
        self.f.unlink()
        self.p.on_pre_tool_call(tool_name="patch", args={"path": str(self.f)}, tool_call_id="t9")
        self.assertIsNone(self.p.on_transform_tool_result(
            tool_name="patch", args={"path": str(self.f)}, result="{}", session_id="S", tool_call_id="t9"))

    def test_snapshot_ttl_prunes_orphans(self):
        self.p.on_pre_tool_call(tool_name="patch", args={"path": str(self.f)}, tool_call_id="orphan")
        self.assertIn("orphan", self.p._snapshots)
        self.p._snapshots["orphan"].at -= self.p.SNAPSHOT_TTL_S + 1
        self.p.on_pre_tool_call(tool_name="patch", args={"path": str(self.f)}, tool_call_id="next")
        self.assertNotIn("orphan", self.p._snapshots)
        self.assertIn("next", self.p._snapshots)

    def test_only_new_findings_reported(self):
        """The pre-existing literal-tag finding is not repeated on every edit."""
        out = self.call("patch", {"path": str(self.f), "new_string": "x"},
                        lambda: self.prepend("**2026-09-30 21:00 [session Z] — plain.** ok"))
        self.assertIsNone(out)

    def test_register_wires_two_hooks(self):
        got = []

        class Ctx:
            def register_hook(self, name, fn):
                got.append(name)
        self.p.register(Ctx())
        self.assertEqual(got, ["pre_tool_call", "transform_tool_result", "pre_llm_call"])


def _db(home: Path, rows):
    con = sqlite3.connect(home / "state.db")
    con.execute("create table sessions (id text, title text, started_at real, ended_at real, end_reason text, "
                "last_activity_at real, parent_session_id text)")
    con.executemany("insert into sessions values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


LCM = "[Recent Summary (d0, node 12)]\nstuff\n[Expand for details: h]"


class Reinject(TmpHome):
    def setUp(self):
        super().setUp()
        now = time.time()
        _db(self.home, [("OLD", "a", now - 900, now - 500, "compression", now - 500, None),
                        ("TIP", "a #2", now - 500, None, None, now - 5, "OLD"),
                        ("SUB", "Subagent", now - 450, None, None, now - 5, "TIP"),
                        ("NEW", "b", now - 10, None, None, now - 1, None)])

    def turn(self, sid, hist, first=False):
        return self.p.on_pre_llm_call(session_id=sid, conversation_history=hist, is_first_turn=first)

    def test_lineage_compression_only(self):
        from agent_inflight import sessions
        be = sessions.backend(self.home)
        self.assertEqual(be.lineage("TIP"), ["OLD", "TIP"])
        self.assertEqual(be.lineage("SUB"), ["SUB"])  # delegation parent is not lineage

    def test_inject_once_after_lcm_compaction_owned_only(self):
        self.assertIsNone(self.turn("TIP", [], first=True))  # conversation's first turn
        out = self.turn("TIP", [{"role": "user", "content": LCM}])
        self.assertIsNotNone(out)
        ctx = out["context"]
        self.assertIn("older entry", ctx)               # tagged OLD = lineage root
        self.assertNotIn("foreign literal", ctx)
        self.assertNotIn("closed task", ctx)              # done entries are not "open"
        self.assertIn("OLD -> TIP", ctx)
        self.assertIn("after compaction", ctx)
        self.assertIsNone(self.turn("TIP", [{"role": "user", "content": LCM}]))  # unchanged -> silent

    def test_list_content_markers_seen(self):
        self.turn("TIP", [], first=True)
        out = self.turn("TIP", [{"role": "user", "content": [{"type": "text", "text": LCM}]}])
        self.assertIsNotNone(out)

    def test_fresh_first_turn_silent(self):
        self.assertIsNone(self.turn("NEW", [], first=True))

    def test_resumed_session_injects(self):
        out = self.turn("TIP", [{"role": "user", "content": "earlier"}], first=False)
        self.assertIn("after resumed", out["context"])

    def test_subagent_gets_nothing(self):
        self.assertIsNone(self.turn("SUB", [{"role": "user", "content": LCM}]))

    def test_opt_out(self):
        os.environ["INFLIGHT_REINJECT"] = "0"
        try:
            self.assertIsNone(self.turn("TIP", [{"role": "user", "content": "x"}]))
        finally:
            os.environ.pop("INFLIGHT_REINJECT")

    def test_cap(self):
        from agent_inflight import reinject
        big = [core.Entry(f"**2026-09-30 [session TIP] — e{i}.** " + "x" * 3000) for i in range(5)]
        out = reinject.render(big, "TIP", ["TIP"], "compaction")
        self.assertLessEqual(len(out.encode()), reinject.MAX_BYTES + 200)
        self.assertIn("omitted for size", out)

    def test_signature_for_builtin_compressor(self):
        from agent_inflight import reinject
        a = reinject.compaction_signature([{"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] x"}])
        self.assertNotEqual(a, reinject.compaction_signature([]))


class RetagPure(unittest.TestCase):
    def test_existing_head_edit_not_new_when_id_present(self):
        before = "## Right now\n\n**2026-09-30 10:00 [session $X #ab12cd] — a.** x\n"
        after = "## Right now\n\n**2026-09-30 10:00 [session $X #ab12cd] — a, edited.** x\n"
        r = retag.retag(after, retag.snapshot(before), "S", provenance="2026-09-30 10:00 — a, edited.")
        self.assertEqual((r.retagged, r.foreign), (0, 1))

    def test_id_kept_when_tag_rewritten(self):
        after = "## Right now\n\n**2026-09-30 10:00 [session $HERMES_SESSION_ID #ab12cd] — mine.** x\n"
        r = retag.retag(after, set(), "S1", provenance="**2026-09-30 10:00 — mine.**")
        self.assertIn("[session S1 #ab12cd] — mine", r.text)
        self.assertEqual(core.right_now(core.parse(r.text)).entries[0].id, "ab12cd")

    def test_same_headline_two_sessions_disambiguated_by_id(self):
        """Two new entries with identical text: only one is in our snapshot diff by id."""
        before = "## Right now\n\n**2026-09-30 10:00 [session $X #aaaaaa] — same.** x\n"
        after = ("## Right now\n\n**2026-09-30 10:00 [session $X #bbbbbb] — same.** x\n\n"
                 "**2026-09-30 10:00 [session $X #aaaaaa] — same.** x\n")
        r = retag.retag(after, retag.snapshot(before), "S2", provenance="2026-09-30 10:00 — same.")
        self.assertEqual((r.retagged, r.foreign), (1, 1))
        self.assertIn("[session S2 #bbbbbb]", r.text)
        self.assertIn("[session $X #aaaaaa]", r.text)

    def test_no_sid_noop(self):
        r = retag.retag(BASE, set(), "", provenance=BASE)
        self.assertEqual(r.retagged, 0)


class CallDirs(TmpHome):
    """Relative tool args resolve against the session cwd, never the daemon's."""

    def setUp(self):
        super().setUp()
        import types
        self._mods = {k: sys.modules.get(k) for k in ("tools", "tools.terminal_tool", "tools.approval")}
        self.session_cwd = None
        tt = types.ModuleType("tools.terminal_tool")
        tt.get_session_cwd = lambda task_id=None: self.session_cwd
        pkg = types.ModuleType("tools")
        pkg.terminal_tool = tt
        sys.modules["tools"], sys.modules["tools.terminal_tool"] = pkg, tt
        # daemon cwd = a repo the session never touched
        self.daemon = self.home / "daemon-repo"
        (self.daemon / ".git").mkdir(parents=True)
        self.sess = self.home / "session-repo"
        (self.sess / ".git").mkdir(parents=True)
        (self.sess / "docs").mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.daemon)

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in self._mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        super().tearDown()

    def dirs(self, args):
        return self.p._call_dirs("task-1", args)

    def test_bare_relative_path_uses_session_cwd(self):
        self.session_cwd = str(self.sess)
        d = self.dirs({"path": "notes.md"})
        self.assertEqual(d, [str(self.sess), os.path.join(str(self.sess), ".")])
        self.assertNotIn(str(self.daemon), [os.path.realpath(x) for x in d])

    def test_relative_subdir_and_workdir(self):
        self.session_cwd = str(self.sess)
        d = self.dirs({"path": "docs/a.md", "workdir": "docs"})
        self.assertIn(os.path.join(str(self.sess), "docs"), d)
        self.assertEqual(d.count(os.path.join(str(self.sess), "docs")), 2)

    def test_relative_without_session_cwd_is_dropped(self):
        self.session_cwd = None
        self.assertEqual(self.dirs({"path": "notes.md", "workdir": "x"}), [])

    def test_absolute_path_kept_without_session_cwd(self):
        self.session_cwd = None
        self.assertEqual(self.dirs({"path": str(self.sess / "docs" / "a.md")}), [str(self.sess / "docs")])

    def test_record_repos_never_records_daemon_repo(self):
        self.session_cwd = None
        self.p.record_repos("S-REL", "task-1", {"path": "notes.md"})
        from agent_inflight import state
        self.assertNotIn(str(self.daemon.resolve()), (state.load("S-REL") or {}).get("repos", {}))

    def test_session_cwd_read_with_hermes_session_key_first(self):
        # Hermes records the cwd under `get_current_session_key() or task_id`.
        # One-shot / gateway runs set a session key; reading by task_id alone
        # found nothing there (found in the manual e2e run, step 6).
        import types
        by_key = {"sess-key": str(self.sess)}
        sys.modules["tools.terminal_tool"].get_session_cwd = lambda k=None: by_key.get(k)
        appr = types.ModuleType("tools.approval")
        appr.get_current_session_key = lambda default="": "sess-key"
        sys.modules["tools.approval"] = appr
        self.assertEqual(self.dirs({"command": "git status"}), [str(self.sess)])
        appr.get_current_session_key = lambda default="": ""
        by_key.clear()
        by_key["task-1"] = str(self.sess)
        self.assertEqual(self.dirs({"command": "git status"}), [str(self.sess)])


class HermesCollision(CallDirs):
    """Manual e2e step 10: a Hermes session editing a repo another live
    session (here a Claude Code heartbeat) touched gets the warning in the
    tool result. The tool already ran; nothing is blocked."""

    def setUp(self):
        super().setUp()
        from agent_inflight import state
        self.state = state
        self.session_cwd = str(self.sess)
        state.update("cc-live", repo=str(self.sess.resolve()), harness="claude-code")

    def run_tool(self, sid="H3", tcid="t9"):
        self.p.on_pre_tool_call(tool_name="write_file", args={"path": str(self.sess / "x.txt")}, tool_call_id=tcid)
        return self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(self.sess / "x.txt")},
                                               result='{"ok": true}', session_id=sid, tool_call_id=tcid,
                                               task_id="task-1")

    def test_warning_appended_once_per_repo(self):
        out = self.run_tool()
        self.assertIsInstance(out, str)
        self.assertTrue(out.startswith('{"ok": true}'))
        self.assertIn("[inflight] another active session (cc-live)", out)
        self.p._recorded.clear()  # past the 60 s record throttle
        self.assertIsNone(self.run_tool(tcid="t10"), "second call in the same repo must stay silent")

    def test_no_warning_when_other_session_ended(self):
        self.state.update("cc-live", ended_at=time.time(), end_reason="clear")
        self.assertIsNone(self.run_tool())

    def test_no_warning_for_own_session(self):
        self.assertIsNone(self.run_tool(sid="cc-live"))

    def test_ended_hermes_session_from_state_db_does_not_warn(self):
        # A Hermes session's hook state never gets `ended_at`; its end is in
        # state.db. An ended one-shot that touched the repo is not live.
        self.state.update("cc-live", ended_at=time.time(), end_reason="clear")
        self.state.update("H-old", repo=str(self.sess.resolve()), harness="hermes")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text, title text, started_at real, ended_at real, "
                    "end_reason text, last_activity_at real, parent_session_id text)")
        now = time.time()
        con.execute("insert into sessions values (?,?,?,?,?,?,?)",
                    ("H-old", "t", now - 60, now - 30, "agent_close", now - 30, None))
        con.commit()
        con.close()
        self.assertIsNone(self.run_tool())
        con = sqlite3.connect(self.home / "state.db")
        con.execute("update sessions set ended_at = null, end_reason = null where id = 'H-old'")
        con.commit()
        con.close()
        self.p._recorded.clear()
        out = self.run_tool(sid="H-new", tcid="t11")
        self.assertIn("H-old", out or "", "live again in state.db -> warns")


if __name__ == "__main__":
    unittest.main(verbosity=1)
