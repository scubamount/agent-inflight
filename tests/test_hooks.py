#!/usr/bin/env python3
"""PR 2 arms: hook protocol v1, per-session state, backend chain, plugin
allowlist + entry-point loading, private-venv install. Stdlib only; every arm
runs against a temp home, never the user's files."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
sys.path.insert(0, str(ROOT / "src"))

from agent_inflight import backends, hook, plugins, state  # noqa: E402

SCRUB = ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID", "INFLIGHT_SESSION_ID",
         "CLAUDE_CODE_SESSION_ID", "INFLIGHT_HOME", "PYTHONPATH")


def env_for(home: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update({"INFLIGHT_HOME": str(home), "HERMES_ROOT": str(home), "HOME": str(home), **extra})
    return env


def cli(home: Path, *args: str, stdin: str = "", py: str = sys.executable, **extra: str):
    p = subprocess.run([py, str(BIN), *args], input=stdin, capture_output=True, text=True,
                       env=env_for(home, **extra), check=False, timeout=60)
    return p.returncode, p.stdout, p.stderr


class Home(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.home = Path(self._t.name).resolve()
        self._env = {k: os.environ.get(k) for k in SCRUB}
        for k in SCRUB:
            os.environ.pop(k, None)
        os.environ["INFLIGHT_HOME"] = str(self.home)
        os.environ["HERMES_ROOT"] = str(self.home)
        (self.home / "inflight.md").write_text("## Right now\n\n")
        self.repo = self.home / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "sub").mkdir()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._t.cleanup()

    def hook(self, event: str, payload) -> tuple:
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = hook.run([event], stdin=io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
        finally:
            sys.stdout = real
        return rc, out.getvalue()


class HookProtocol(Home):
    def test_fail_open_on_handler_error(self):
        real = hook.handle

        def boom(*a, **k):
            raise RuntimeError("secret-ish detail")
        hook.handle = boom
        try:
            rc, out = self.hook("pre-tool", {"session_id": "s1"})
        finally:
            hook.handle = real
        self.assertEqual((rc, out), (0, ""))
        log = state.read_log()
        self.assertEqual(log[-1]["action"], "hook-error")
        self.assertEqual(log[-1]["error"], "RuntimeError")
        self.assertNotIn("secret-ish", state.hooks_log().read_text())

    def test_fail_open_via_cli(self):
        for event, stdin in (("pre-tool", "{not json"), ("pre-tool", "[1,2]"), ("nope", "{}"), ("", "")):
            rc, out, err = cli(self.home, "hook", *([event] if event else []), stdin=stdin)
            self.assertEqual((rc, out), (0, ""), f"{event!r} {stdin!r}: {err}")

    def test_bad_session_id_never_becomes_a_path(self):
        for bad in ("../../etc/x", "a/b", "..", ".hidden", "x" * 200, "", 5):
            rc, out = self.hook("post-tool", {"session_id": bad, "cwd": str(self.repo)})
            self.assertEqual((rc, out), (0, ""))
            with self.assertRaises(ValueError):
                state.session_path(bad if isinstance(bad, str) else "a/b")
        sd = state.sessions_dir()
        self.assertFalse(sd.exists() and any(sd.iterdir()))
        self.assertFalse((self.home.parent / "etc").exists())

    def test_state_recorded_private(self):
        self.hook("session-start", {"session_id": "s1", "cwd": str(self.repo / "sub"), "source": "startup"})
        self.hook("post-tool", {"session_id": "s1", "cwd": str(self.repo), "tool": "Bash",
                                "args": {"command": "echo TOPSECRETARG"}})
        p = state.session_path("s1")
        d = json.loads(p.read_text())
        self.assertIn(str(self.repo), d["repos"])
        self.assertIsNone(d.get("ended_at"))
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.sessions_dir().stat().st_mode & 0o777, 0o700)
        self.assertEqual(state.state_dir().stat().st_mode & 0o777, 0o700)
        for f in [p, *state.hooks_log().parent.glob("*")]:
            if f.is_file():
                self.assertNotIn("TOPSECRETARG", f.read_text(errors="ignore"))

    def test_session_end_then_heartbeat_backend_says_ended(self):
        self.hook("session-start", {"session_id": "s1", "cwd": str(self.repo)})
        info = backends.HeartbeatBackend().lookup("s1")
        self.assertIsNone(info.ended_at)
        self.hook("session-end", {"session_id": "s1", "reason": "clear"})
        info = backends.HeartbeatBackend().lookup("s1")
        self.assertIsNotNone(info.ended_at)
        self.assertEqual(info.end_reason, "clear")
        self.hook("session-start", {"session_id": "s1", "source": "resume"})
        self.assertIsNone(backends.HeartbeatBackend().lookup("s1").ended_at)

    def test_collision_warning_once(self):
        self.hook("post-tool", {"session_id": "A", "cwd": str(self.repo)})
        rc, out = self.hook("pre-tool", {"session_id": "B", "cwd": str(self.repo / "sub")})
        self.assertIn("another active session (A)", out)
        rc, out2 = self.hook("pre-tool", {"session_id": "B", "cwd": str(self.repo)})
        self.assertEqual(out2, "")
        self.hook("session-end", {"session_id": "A"})
        rc, out3 = self.hook("pre-tool", {"session_id": "C", "cwd": str(self.repo)})
        self.assertEqual(out3, "")  # ended sessions don't collide

    def test_session_start_compact_reinjects_own_entries(self):
        (self.home / "inflight.md").write_text(
            "## Right now\n\n**2026-10-01 10:00 [session s1 #aaaaaa] — mine: NOT pushed.** x\n\n"
            "**2026-10-01 09:00 [session s2 #bbbbbb] — theirs.** y\n")
        rc, out = self.hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertEqual(out, "")
        rc, out = self.hook("session-start", {"session_id": "s1", "source": "compact"})
        self.assertIn("mine: NOT pushed", out)
        self.assertNotIn("theirs", out)
        self.assertIn("verify on disk", out)

    def test_hook_log_whitelist(self):
        state.log("x", session="s1", tool_args="LEAK", content="LEAK", error="E")
        text = state.hooks_log().read_text()
        self.assertNotIn("LEAK", text)
        self.assertEqual(state.hooks_log().stat().st_mode & 0o777, 0o600)

    def test_force_override_logged_without_value(self):
        fake = "sk-proj-" + "x9Y" * 30
        rc, out, err = cli(self.home, "add", f"leak {fake}", "--force", INFLIGHT_SESSION_ID="s1")
        self.assertEqual(rc, 0, err)
        log = [r for r in state.read_log() if r["action"] == "secret-force"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["kinds"], ["openai/anthropic-style key"])
        self.assertRegex(log[0]["entry_id"], r"^[0-9a-f]{6}$")
        self.assertNotIn(fake, state.hooks_log().read_text())


class Backends(Home):
    def test_chain_heartbeat_used_when_no_hermes_db(self):
        self.hook("post-tool", {"session_id": "cc-1", "cwd": str(self.repo)})
        (self.home / "inflight.md").write_text("## Right now\n\n**2026-10-01 10:00 [session cc-1 #aaaaaa] — x.**\n")
        rc, out, err = cli(self.home, "sessions", "--json")
        d = json.loads(out)
        self.assertEqual(d["backend"], "heartbeat")
        self.assertEqual(d["entries"][0]["status"], "ACTIVE")
        self.assertEqual(d["entries"][0]["backend"], "heartbeat")

    def test_status_override_and_unknown_falls_through(self):
        class A:
            name = "a"
            def available(self): return True
            def lookup(self, sid): return backends.SessionInfo(status="UNKNOWN")
        class B:
            name = "b"
            def available(self): return True
            def lookup(self, sid): return backends.SessionInfo(status="ENDED", end_reason="x")
        ch = backends.Chain([A(), B()])
        info = ch.lookup("q")
        self.assertEqual((info["backend"], info["status"]), ("b", "ENDED"))
        self.assertEqual(ch.lineage("q"), ["q"])  # B has no lineage(): default

    def test_broken_backend_skipped(self):
        class Bad:
            name = "bad"
            def available(self): return True
            def lookup(self, sid): raise RuntimeError("x")
        self.assertIsNone(backends.Chain([Bad()]).lookup("q"))
        self.assertEqual(state.read_log()[-1]["action"], "backend-error")


def make_plugin_pkg(where: Path, name: str, marker: Path, api: int = 1) -> Path:
    """A real installable package with an `agent_inflight.backends` entry point.
    Its module writes `marker` on import, so tests can prove it was (not) imported."""
    pkg = where / f"src_{name}"
    mod = pkg / f"{name}_mod"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        Path({str(marker)!r}).write_text("imported")
        PLUGIN_API_VERSION = {api}
        class Backend:
            name = {name!r}
            def available(self):
                return True
            def lookup(self, sid):
                return {{"status": "ACTIVE", "last_activity_at": 1.0, "title": "from {name}"}} if sid.startswith("ext-") else None
    """))
    (pkg / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["setuptools>=61"]
        build-backend = "setuptools.build_meta"
        [project]
        name = "{name}"
        version = "0.0.1"
        [project.entry-points."agent_inflight.backends"]
        {name} = "{name}_mod:Backend"
    """))
    return pkg


def install_dist_info(site: Path, name: str, src_root: Path) -> None:
    """Install without pip/network: a .pth for the source + a dist-info with
    entry_points.txt. Exactly what importlib.metadata reads."""
    (site / f"{name}.pth").write_text(str(src_root) + "\n")
    di = site / f"{name}-0.0.1.dist-info"
    di.mkdir()
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.1\n")
    (di / "entry_points.txt").write_text(f"[agent_inflight.backends]\n{name} = {name}_mod:Backend\n")


class PluginsEndToEnd(Home):
    """Done-criterion 2: a throwaway plugin in a temp venv, enabled by name,
    discovered by `inflight sessions`, with zero edits to this repo."""

    def setUp(self):
        super().setUp()
        self.venv = self.home / "venv"
        venv.EnvBuilder(with_pip=False, system_site_packages=False).create(self.venv)
        self.py = str(self.venv / "bin" / "python")
        site = Path(subprocess.check_output(
            [self.py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True).strip())
        self.marker_ok = self.home / "ok.marker"
        self.marker_off = self.home / "off.marker"
        for name, marker in (("goodbe", self.marker_ok), ("offbe", self.marker_off)):
            pkg = make_plugin_pkg(self.home, name, marker)
            install_dist_info(site, name, pkg)
        (self.home / "inflight.md").write_text(
            "## Right now\n\n**2026-10-01 10:00 [session ext-1 #aaaaaa] — from a plugin session.**\n")

    def test_listed_but_not_imported_until_enabled(self):
        rc, out, err = cli(self.home, "plugin", "list", py=self.py)
        self.assertEqual(rc, 0, err)
        self.assertIn("disabled  goodbe", out)
        self.assertIn("disabled  offbe", out)
        rc, out, err = cli(self.home, "sessions", "--json", py=self.py)
        self.assertIn(json.loads(out)["entries"][0]["status"], ("UNKNOWN", "NO-BACKEND"))
        self.assertFalse(self.marker_ok.exists() or self.marker_off.exists(), "imported without enable")

    def test_enable_then_sessions_uses_it(self):
        rc, out, err = cli(self.home, "plugin", "enable", "goodbe", py=self.py)
        self.assertEqual(rc, 0, err)
        rc, out, err = cli(self.home, "sessions", "--json", py=self.py)
        d = json.loads(out)
        e = d["entries"][0]
        self.assertEqual((e["status"], e["backend"], e["title"]), ("ACTIVE", "goodbe", "from goodbe"))
        self.assertTrue(self.marker_ok.exists())
        self.assertFalse(self.marker_off.exists(), "a plugin not on the allowlist was imported")
        cfg = state.state_dir() / "config.json"
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        acts = [r["action"] for r in state.read_log()]
        self.assertIn("plugin-enable", acts)
        rc, out, err = cli(self.home, "plugin", "disable", "goodbe", py=self.py)
        self.assertIn("plugin-disable", [r["action"] for r in state.read_log()])

    def test_enable_unknown_refused(self):
        rc, out, err = cli(self.home, "plugin", "enable", "nosuch", py=self.py)
        self.assertEqual(rc, 1)

    def test_add_and_hook_never_import_plugins(self):
        cli(self.home, "plugin", "enable", "goodbe", py=self.py)
        if self.marker_ok.exists():
            self.marker_ok.unlink()
        cli(self.home, "add", "x", py=self.py, INFLIGHT_SESSION_ID="s1")
        cli(self.home, "check", py=self.py)
        cli(self.home, "hook", "pre-tool", stdin='{"session_id":"s1"}', py=self.py)
        self.assertFalse(self.marker_ok.exists(), "fast-path command imported a plugin")


class EntryPointShim(unittest.TestCase):
    def test_39_dict_shape(self):
        from importlib import metadata
        real = metadata.entry_points

        class EP:
            name, value = "x", "m:B"

        def fake():  # Python 3.9: a dict of group -> tuple, no .select
            return {plugins.GROUP: (EP(),), "other": ()}
        metadata.entry_points = fake
        try:
            self.assertEqual([e.name for e in plugins._entry_points()], ["x"])
        finally:
            metadata.entry_points = real

    def test_current_python(self):
        self.assertIsInstance(plugins._entry_points(), list)


class Install(unittest.TestCase):
    """install.sh offline + idempotent, into a temp home."""

    def test_install_venv_launcher_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t).resolve()
            env = env_for(home, INFLIGHT_BIN_DIR=str(home / "bin"), HERMES_HOME=str(home / "nohermes"))
            env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
            run = lambda: subprocess.run(["sh", str(ROOT / "install.sh"), "--no-skill", "--no-plugin"],
                                         env=env, capture_output=True, text=True, timeout=180)
            p1 = run()
            self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
            st = home / "inflight-state"
            self.assertTrue((st / "venv" / "bin" / "python").exists(), p1.stdout + p1.stderr)
            self.assertEqual(os.readlink(home / "bin" / "inflight"), str(st / "inflight"))
            self.assertEqual(st.stat().st_mode & 0o777, 0o700)
            p = subprocess.run([str(home / "bin" / "inflight"), "check"], env=env, capture_output=True, text=True)
            self.assertNotIn("not the pinned venv", p.stdout)
            p = subprocess.run([sys.executable, str(BIN), "check"], env=env, capture_output=True, text=True)
            if Path(sys.prefix).resolve() != (st / "venv").resolve():
                self.assertIn("not the pinned venv", p.stdout)
            p2 = run()
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertIn("ok venv", p2.stdout)
            self.assertIn("ok launcher", p2.stdout)
            self.assertIn("(up to date)", p2.stdout)


if __name__ == "__main__":
    unittest.main()
