#!/usr/bin/env python3
"""PR 4 arms: Claude Code adapter (merge, uninstall, backup, managed-hooks
lockdown, layout guard) and the hook's Claude Code wire format. Temp files
only; the user's ~/.claude is never read or written."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
sys.path.insert(0, str(ROOT / "src"))

from agent_inflight import adapter_claude as ac, hook, state  # noqa: E402

SCRUB = ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID", "INFLIGHT_SESSION_ID",
         "CLAUDE_CODE_SESSION_ID", "INFLIGHT_HOME", "PYTHONPATH", "CLAUDE_CONFIG_DIR",
         "INFLIGHT_CLAUDE_MANAGED_DIRS")

# Shape of a real user file: unrelated hooks on the same events, other keys, unicode.
EXISTING = {
    "hooks": {
        "PreToolUse": [{"matcher": "Grep|Glob", "hooks": [{"type": "command", "command": "~/.claude/hooks/gate"}]}],
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "\"/usr/bin/node\" \"/x/activate.js\""}]},
            {"matcher": "startup", "hooks": [{"type": "command", "command": "~/.claude/hooks/remind"}]},
        ],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "node /x/track.js"}]}],
    },
    "statusLine": {"type": "command", "command": "echo é"},
    "effortLevel": "high",
}


class Env(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name).resolve()
        self._env = {k: os.environ.get(k) for k in SCRUB}
        for k in SCRUB:
            os.environ.pop(k, None)
        os.environ["INFLIGHT_HOME"] = str(self.t / "home")
        os.environ["HERMES_ROOT"] = str(self.t / "home")
        os.environ["INFLIGHT_CLAUDE_MANAGED_DIRS"] = str(self.t / "managed")
        (self.t / "home").mkdir()
        (self.t / "home" / "inflight.md").write_text("## Right now\n\n")
        self.settings = self.t / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.raw = ac.dump(EXISTING)
        self.settings.write_text(self.raw)
        self.settings.chmod(0o644)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._t.cleanup()

    def cli(self, *args):
        env = {**os.environ}
        p = subprocess.run([sys.executable, str(BIN), "adapter", *args, "--settings", str(self.settings)],
                           capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def backups(self):
        return sorted(self.settings.parent.glob("settings.json.inflight-bak-*"))


class Merge(Env):
    def test_dry_run_writes_nothing_and_shows_diff(self):
        rc, out, err = self.cli("install", "claude-code")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry run: nothing written", out)
        self.assertIn("+", out)
        self.assertIn("hook pre-tool --harness claude-code", out)
        self.assertEqual(self.settings.read_text(), self.raw)
        self.assertEqual(self.backups(), [])

    def test_install_keeps_existing_hooks_in_order(self):
        rc, out, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 0, err)
        d = json.loads(self.settings.read_text())
        self.assertEqual(d["hooks"]["PreToolUse"][0], EXISTING["hooks"]["PreToolUse"][0])
        self.assertEqual(d["hooks"]["SessionStart"][:2], EXISTING["hooks"]["SessionStart"])
        self.assertEqual(d["hooks"]["UserPromptSubmit"], EXISTING["hooks"]["UserPromptSubmit"])
        self.assertEqual(d["statusLine"], EXISTING["statusLine"])
        for ev in ("SessionStart", "PreToolUse", "PostToolUse", "CwdChanged", "SessionEnd"):
            self.assertTrue(any(ac.is_ours(h) for g in d["hooks"][ev] for h in g["hooks"]), ev)
        self.assertEqual(d["hooks"]["PreToolUse"][-1]["matcher"], "^(Edit|Write|NotebookEdit|Bash)$")
        self.assertNotIn("matcher", d["hooks"]["CwdChanged"][0])  # no matcher support
        self.assertTrue(d["hooks"]["PostToolUse"][0]["hooks"][0]["async"])
        self.assertNotIn("timeout", d["hooks"]["SessionEnd"][0]["hooks"][0])  # keep the 1.5 s budget
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o644)  # mode kept

    def test_backup_created_0600_with_original_bytes(self):
        self.cli("install", "claude-code", "--apply")
        b = self.backups()
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].read_text(), self.raw)
        self.assertEqual(b[0].stat().st_mode & 0o777, 0o600)

    def test_uninstall_restores_byte_for_byte(self):
        self.cli("install", "claude-code", "--apply")
        rc, out, err = self.cli("uninstall", "claude-code", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.settings.read_text(), self.raw)

    def test_install_idempotent(self):
        self.cli("install", "claude-code", "--apply")
        after = self.settings.read_text()
        rc, out, _ = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 0)
        self.assertIn("no change", out)
        self.assertEqual(self.settings.read_text(), after)
        self.assertEqual(len(self.backups()), 1)

    def test_user_edits_after_install_survive_uninstall(self):
        self.cli("install", "claude-code", "--apply")
        d = json.loads(self.settings.read_text())
        d["hooks"]["PreToolUse"].append({"matcher": "Read", "hooks": [{"type": "command", "command": "mine"}]})
        d["model"] = "opus"
        self.settings.write_text(ac.dump(d))
        self.cli("uninstall", "claude-code", "--apply")
        d2 = json.loads(self.settings.read_text())
        self.assertEqual(d2["model"], "opus")
        self.assertEqual(d2["hooks"]["PreToolUse"][-1]["hooks"][0]["command"], "mine")
        self.assertFalse(any(ac.is_ours(h) for gs in d2["hooks"].values() for g in gs for h in g["hooks"]))

    def test_lookalike_command_not_removed(self):
        d = json.loads(self.raw)
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command",
                                                     "command": "echo hook pre-tool --harness claude-code-x"}]})
        self.settings.write_text(ac.dump(d))
        self.cli("install", "claude-code", "--apply")
        self.cli("uninstall", "claude-code", "--apply")
        self.assertIn("claude-code-x", self.settings.read_text())

    def test_missing_file_created_0600_never_deleted(self):
        self.settings.unlink()
        rc, _, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.cli("uninstall", "claude-code", "--apply")
        self.assertEqual(self.settings.read_text(), "{}\n")
        rc, out, _ = self.cli("uninstall", "claude-code", "--apply")
        self.assertIn("no change", out)

    def test_nonstandard_layout_refused(self):
        self.settings.write_text(json.dumps(EXISTING))  # one line
        rc, _, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("2-space JSON layout", err)
        self.assertEqual(self.settings.read_text(), json.dumps(EXISTING))

    def test_launcher_path_quoted(self):
        self.assertEqual(ac.command_for("pre-tool", "/a b/inflight"),
                         "'/a b/inflight' hook pre-tool --harness claude-code")


class Lockdown(Env):
    def managed(self, d, name="managed-settings.json"):
        m = self.t / "managed"
        m.mkdir(exist_ok=True)
        (m / name).write_text(json.dumps(d))

    def test_allow_managed_hooks_only_writes_nothing(self):
        self.managed({"allowManagedHooksOnly": True})
        rc, out, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 3)
        self.assertIn("allowManagedHooksOnly", err)
        self.assertIn("Ask IT", err)
        self.assertEqual(self.settings.read_text(), self.raw)
        self.assertEqual(self.backups(), [])

    def test_strict_plugin_only_and_dropin_dir(self):
        (self.t / "managed" / "managed-settings.d").mkdir(parents=True)
        self.managed({"strictPluginOnlyCustomization": ["hooks"]}, "managed-settings.d/20-x.json")
        rc, _, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 3)
        self.assertIn("strictPluginOnlyCustomization", err)
        self.assertEqual(self.settings.read_text(), self.raw)

    def test_unrelated_managed_settings_dont_block(self):
        self.managed({"permissions": {"deny": ["Read(./.env)"]}})
        rc, _, err = self.cli("install", "claude-code", "--apply")
        self.assertEqual(rc, 0, err)

    def test_uninstall_allowed_under_lockdown(self):
        self.cli("install", "claude-code", "--apply")
        self.managed({"allowManagedHooksOnly": True})
        rc, _, err = self.cli("uninstall", "claude-code", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.settings.read_text(), self.raw)


class WireFormat(Env):
    def run_hook(self, event, payload, harness: "str | None" = "claude-code"):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            argv = [event] + (["--harness", harness] if harness else [])
            rc = hook.run(argv, stdin=io.StringIO(json.dumps(payload)))
        finally:
            sys.stdout = real
        return rc, out.getvalue()

    def test_collision_warning_is_additional_context_never_a_decision(self):
        repo = self.t / "repo"
        (repo / ".git").mkdir(parents=True)
        self.run_hook("post-tool", {"session_id": "other-1", "cwd": str(repo), "tool_name": "Edit"})
        rc, out = self.run_hook("pre-tool", {"session_id": "me-1", "cwd": str(repo), "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("another active session", d["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", out)
        self.assertNotIn("decision", json.dumps(d).replace("hookSpecificOutput", ""))

    def test_new_cwd_and_harness_recorded(self):
        repo = self.t / "r2"
        (repo / ".git").mkdir(parents=True)
        self.run_hook("cwd-changed", {"session_id": "cc-1", "cwd": str(self.t), "old_cwd": str(self.t),
                                      "new_cwd": str(repo)})
        d = state.load("cc-1")
        self.assertIn(str(repo), d.get("repos", {}))
        self.run_hook("session-start", {"session_id": "cc-1", "cwd": str(repo), "source": "startup"})
        self.assertEqual(state.load("cc-1").get("harness"), "claude-code")

    def test_session_start_compact_is_additional_context(self):
        (self.t / "home" / "inflight.md").write_text(
            "## Right now\n\n**2026-10-01 10:00 [session cc-2 #abcdef] — mine.** x\n")
        rc, out = self.run_hook("session-start", {"session_id": "cc-2", "source": "compact"})
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("mine.", d["hookSpecificOutput"]["additionalContext"])

    def test_without_harness_plain_text_unchanged(self):
        (self.t / "home" / "inflight.md").write_text(
            "## Right now\n\n**2026-10-01 10:00 [session cc-3 #abcdef] — mine.** x\n")
        rc, out = self.run_hook("session-start", {"session_id": "cc-3", "source": "compact"}, harness=None)
        self.assertTrue(out.startswith("[inflight: your open entries"))

    def test_session_end_through_hook_run_marks_ended(self):
        repo = self.t / "r3"
        (repo / ".git").mkdir(parents=True)
        self.run_hook("session-start", {"session_id": "cc-4", "cwd": str(repo), "source": "startup"})
        rc, out = self.run_hook("session-end", {"session_id": "cc-4", "cwd": str(repo), "reason": "clear",
                                                "hook_event_name": "SessionEnd"})
        self.assertEqual((rc, out), (0, ""))
        d = state.load("cc-4")
        self.assertIsNotNone(d.get("ended_at"))
        self.assertEqual(d.get("end_reason"), "clear")

    def test_notebook_edit_payload_records_repo(self):
        repo = self.t / "r4"
        (repo / ".git").mkdir(parents=True)
        payload = {"session_id": "cc-5", "cwd": str(repo), "hook_event_name": "PostToolUse",
                   "tool_name": "NotebookEdit",
                   "tool_input": {"notebook_path": str(repo / "n.ipynb"), "new_source": "SECRETCELL"}}
        rc, out = self.run_hook("post-tool", payload)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn(str(repo), state.load("cc-5").get("repos", {}))
        for f in state.state_dir().rglob("*"):
            if f.is_file() and f.suffix in (".json", ".log", ""):
                self.assertNotIn("SECRETCELL", f.read_text(errors="ignore"))

    def test_bad_input_still_exit_0_empty(self):
        for raw in ("{", "[]", '"x"', ""):
            out = io.StringIO()
            real = sys.stdout
            sys.stdout = out
            try:
                rc = hook.run(["pre-tool", "--harness", "claude-code"], stdin=io.StringIO(raw))
            finally:
                sys.stdout = real
            self.assertEqual((rc, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
