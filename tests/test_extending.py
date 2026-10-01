#!/usr/bin/env python3
"""Phase 5 arms: docs/extending.md is executable, and the worked example
backend in examples/backend-example works as a real third-party package.

- ExampleBackend: the example's lookup() against fixture files (offline).
- ExampleOffline: the example installed into a temp venv without pip
  (dist-info + entry point read from its own pyproject.toml), enabled by name,
  answering `inflight sessions` (offline, always runs).
- Walkthrough: the shell block between the walkthrough markers in
  docs/extending.md, run verbatim from a clean temp dir (real install.sh, real
  `pip install` of the example: needs network), and its output compared with
  the doc's expected block. Also the hook-protocol smoke block.
  Offline: skipped locally, FAILS under CI=true, so CI can't go green on a skip.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
DOC = ROOT / "docs" / "extending.md"
EXAMPLE = ROOT / "examples" / "backend-example"
sys.path.insert(0, str(EXAMPLE / "src"))

import inflight_backend_example as ex  # noqa: E402

SCRUB = ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID", "INFLIGHT_SESSION_ID",
         "CLAUDE_CODE_SESSION_ID", "INFLIGHT_HOME", "INFLIGHT_BIN_DIR", "PYTHONPATH",
         "EXAMPLE_HARNESS_SESSIONS", "PIP_REQUIRE_VIRTUALENV")


def clean_env(home: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    # HOME and HERMES_ROOT point into the temp dir: no real ~/.hermes is ever seen.
    env.update({"HOME": str(home), "HERMES_ROOT": str(home / "nohermes"), **extra})
    return env


def block(name: str) -> str:
    """The fenced block between <!-- name:start --> and <!-- name:end -->."""
    text = DOC.read_text(encoding="utf-8")
    m = re.search(rf"<!-- {name}:start -->\s*```[a-z]*\n(.*?)```\s*<!-- {name}:end -->", text, re.S)
    if not m:
        raise AssertionError(f"docs/extending.md: no {name} block")
    return m.group(1)


def normalize(text: str) -> str:
    text = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d", "YYYY-MM-DD HH:MM", text)
    return re.sub(r"#[0-9a-f]{6}\]", "#xxxxxx]", text)


def entry_point() -> "tuple[str, str]":
    """(name, value) of the example's entry point, from its pyproject.toml
    (regex, not tomllib: tomllib is 3.11+ and CI runs 3.9)."""
    text = (EXAMPLE / "pyproject.toml").read_text(encoding="utf-8")
    sec = text.split('[project.entry-points."agent_inflight.backends"]', 1)[1]
    m = re.search(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]+)"', sec, re.M)
    assert m, "no entry point in examples/backend-example/pyproject.toml"
    return m.group(1), m.group(2)


def online() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


class ExampleBackend(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.f = Path(self._t.name) / "sessions.json"
        self._old = os.environ.get("EXAMPLE_HARNESS_SESSIONS")
        os.environ["EXAMPLE_HARNESS_SESSIONS"] = str(self.f)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("EXAMPLE_HARNESS_SESSIONS", None)
        else:
            os.environ["EXAMPLE_HARNESS_SESSIONS"] = self._old
        self._t.cleanup()

    def write(self, data) -> None:
        self.f.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def test_missing_file_unavailable(self):
        self.assertFalse(ex.Backend().available())
        self.assertIsNone(ex.Backend().lookup("x"))

    def test_lookup_copies_only_listed_fields(self):
        self.write({"sessions": {"s1": {"title": "t", "last_activity_at": 5.0, "ended_at": 9,
                                        "end_reason": "quit", "prompt": "SECRET", "token": "SECRET"}}})
        b = ex.Backend()
        self.assertTrue(b.available())
        got = b.lookup("s1")
        self.assertEqual(got, {"title": "t", "last_activity_at": 5.0, "ended_at": 9,
                               "end_reason": "quit", "profile": "example-harness"})
        self.assertNotIn("SECRET", json.dumps(got))

    def test_not_ours_is_none(self):
        self.write({"sessions": {"s1": {}}})
        self.assertIsNone(ex.Backend().lookup("other"))

    def test_bad_values_dropped_not_raised(self):
        self.write({"sessions": {"s1": {"status": "RUNNING", "started_at": "yesterday", "title": "x" * 500}}})
        got = ex.Backend().lookup("s1")
        self.assertNotIn("status", got)
        self.assertNotIn("started_at", got)
        self.assertEqual(len(got["title"]), 200)

    def test_garbage_and_oversized_files(self):
        for bad in ("not json", "[1,2]", '{"sessions": [1]}'):
            self.write(bad)
            self.assertIsNone(ex.Backend().lookup("s1"))
        self.write(json.dumps({"sessions": {"s1": {"title": "t"}}, "pad": "x" * ex.MAX_BYTES}))
        self.assertIsNone(ex.Backend().lookup("s1"))

    def test_api_version_matches_core(self):
        sys.path.insert(0, str(ROOT / "src"))
        from agent_inflight import backends
        self.assertEqual(ex.PLUGIN_API_VERSION, backends.PLUGIN_API_VERSION)


class ExampleOffline(unittest.TestCase):
    """The example package, not a test-generated stub, through the real
    entry-point loader. No pip, no network."""

    def test_enable_then_sessions_answers(self):
        name, value = entry_point()
        self.assertEqual((name, value), ("example", "inflight_backend_example:Backend"))
        with tempfile.TemporaryDirectory() as t:
            home = Path(t).resolve()
            venv.EnvBuilder(with_pip=False).create(home / "venv")
            py = str(home / "venv" / "bin" / "python")
            site = Path(subprocess.check_output(
                [py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True).strip())
            (site / "example.pth").write_text(str(EXAMPLE / "src") + "\n")
            di = site / "agent_inflight_backend_example-0.1.0.dist-info"
            di.mkdir()
            (di / "METADATA").write_text("Metadata-Version: 2.1\nName: agent-inflight-backend-example\nVersion: 0.1.0\n")
            (di / "entry_points.txt").write_text(f"[agent_inflight.backends]\n{name} = {value}\n")
            sessions = home / "sessions.json"
            sessions.write_text(json.dumps({"sessions": {"ex-1": {"title": "from example", "status": "ENDED",
                                                                   "end_reason": "quit", "secret_note": "LEAK"}}}))
            env = clean_env(home, INFLIGHT_HOME=str(home / "h"), EXAMPLE_HARNESS_SESSIONS=str(sessions))

            def cli(*a):
                p = subprocess.run([py, str(BIN), *a], env=env, capture_output=True, text=True, timeout=60)
                return p.returncode, p.stdout, p.stderr

            self.assertEqual(cli("add", "--session", "ex-1", "x")[0], 0)
            rc, out, err = cli("plugin", "list")
            self.assertIn("disabled  example              inflight_backend_example:Backend", out, err)
            self.assertEqual(cli("plugin", "enable", "example")[0], 0)
            rc, out, err = cli("sessions", "--json")
            self.assertEqual(rc, 0, err)
            e = json.loads(out)["entries"][0]
            self.assertEqual((e["backend"], e["status"], e["profile"], e["end_reason"]),
                             ("example", "ENDED", "example-harness", "quit"))
            self.assertNotIn("LEAK", out)
            rc, out, err = cli("sessions")
            self.assertIn("drill : example-harness show ex-1", out)


class Walkthrough(unittest.TestCase):
    """Done-when for phase 5: someone following docs/extending.md from a clean
    temp dir gets the example backend answering `inflight sessions`."""

    def setUp(self):
        if not online():
            if os.environ.get("CI"):
                self.fail("walkthrough needs network for pip (setuptools); CI must not skip it")
            self.skipTest("offline: walkthrough needs pip to fetch setuptools")

    def test_backend_walkthrough_verbatim(self):
        script = block("walkthrough")
        expected = normalize(block("walkthrough-expected")).rstrip("\n")
        with tempfile.TemporaryDirectory() as t:
            home = Path(t).resolve()
            env = clean_env(home, REPO=str(ROOT), TMPDIR=str(home))
            env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
            p = subprocess.run(["sh", "-eu", "-c", script], env=env, capture_output=True,
                               text=True, timeout=600)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            got = normalize(p.stdout)
            self.assertIn(expected, got, f"doc's expected block not in real output:\n{p.stdout}\n{p.stderr}")
            self.assertNotIn("never forwarded", p.stdout)
            self.assertNotIn("not the pinned venv", p.stdout + p.stderr)
            litter = [str(x) for x in EXAMPLE.rglob("*") if x.name == "build" or x.suffix == ".egg-info"]
            self.assertEqual(litter, [], "the walkthrough wrote build output into the checkout")

    def test_hook_smoke_block(self):
        script = block("smoke")
        with tempfile.TemporaryDirectory() as t:
            home = Path(t).resolve()
            env = clean_env(home, TMPDIR=str(home))
            env["PATH"] = str(ROOT / "bin") + os.pathsep + os.path.dirname(sys.executable) + os.pathsep + env["PATH"]
            p = subprocess.run(["sh", "-eu", "-c", script], env=env, cwd=str(home), capture_output=True,
                               text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertRegex(p.stdout, r"ACTIVE\s+wire-1 @myharness")


class DocPointers(unittest.TestCase):
    def test_linked(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/extending.md", readme)
        self.assertIn("extending.md", (ROOT / "docs" / "hook-protocol.md").read_text(encoding="utf-8"))
        for rel in re.findall(r"\]\(([^)#]+)\)", DOC.read_text(encoding="utf-8")):
            self.assertTrue((DOC.parent / rel).exists(), f"broken link in extending.md: {rel}")


if __name__ == "__main__":
    unittest.main()
