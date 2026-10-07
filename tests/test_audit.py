#!/usr/bin/env python3
"""PR 3 arms: safe_git, hostile-repo fixture, audit classification, catch-up
upsert/dedupe/done, redaction, Hermes repo recording. Temp homes and temp
repos only; the user's files are never read or written."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "inflight"
sys.path.insert(0, str(ROOT / "src"))

from agent_inflight import audit, core, hook, paths, progress, safegit, state  # noqa: E402

SCRUB = ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID", "INFLIGHT_SESSION_ID",
         "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "INFLIGHT_HOME", "PYTHONPATH")
GIT_ID = ("-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false",
          "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main")


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), env=env, capture_output=True, text=True,
                          check=True, timeout=30, stdin=subprocess.DEVNULL).stdout


def mkrepo(path: Path, remote: "Path | None" = None) -> Path:
    """A repo with one commit. With `remote` (a parent dir), it gets its own
    bare origin under it and main is pushed and tracked."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    if remote is not None:
        bare = remote / (str(path).replace(os.sep, "_") + ".git")
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(path, "remote", "add", "origin", str(bare))
        git(path, "push", "-q", "-u", "origin", "HEAD:main")
    return path


class World(unittest.TestCase):
    """A temp home with a tracker, a bare remote and repos in every state."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name).resolve()
        self.home = self.t / "home"
        self.home.mkdir()
        self._env = {k: os.environ.get(k) for k in SCRUB}
        for k in SCRUB:
            os.environ.pop(k, None)
        os.environ["INFLIGHT_HOME"] = str(self.home)
        os.environ["HERMES_ROOT"] = str(self.home)  # no state.db there: heartbeat backend only
        self.tracker = self.home / "inflight.md"
        self.tracker.write_text("## Right now\n\n")
        self.code = self.t / "code"
        self.code.mkdir()
        self.remote = self.t / "remotes"
        self.remote.mkdir()
        self.marks = self.t / "marks"
        self.marks.mkdir()
        self.root_cfg([{"path": str(self.code), "depth": 3}])

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._t.cleanup()

    def root_cfg(self, roots):
        d = state.state_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps({"audit": {"roots": roots}}))

    def session(self, sid: str, repos, ended: bool = False, idle_s: float = 0, harness: str = ""):
        """A write-blind session (Claude Code style) active for the hour before
        `idle_s` ago. The repos' working files are back-dated into that hour,
        as if this session wrote them: attribution reads mtimes."""
        for r in repos:
            state.update(sid, repo=str(r))
        p = state.session_path(sid)
        d = json.loads(p.read_text())
        d["heartbeat_at"] = time.time() - idle_s
        d["started_at"] = time.time() - idle_s - 3600  # began before the fixture's commits
        if harness:
            d["harness"] = harness
        if ended:
            d["ended_at"] = time.time() - idle_s
            d["end_reason"] = "end"
        p.write_text(json.dumps(d))
        if idle_s:
            when = time.time() - idle_s - 60
            for r in repos:
                for f in Path(r).rglob("*"):
                    if ".git" not in f.relative_to(r).parts:
                        os.utime(f, (when, when), follow_symlinks=False)

    def hostile(self, path: Path) -> Path:
        """Repo-local config that runs a marker script from every channel git
        status can reach. The edits keep each file's size, so git must hash
        content (that is when clean/process filters run)."""
        r = mkrepo(path, self.remote)
        (r / "d.dat").write_text("d\n")
        (r / "n.md").write_text("n\n")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "more")
        git(r, "push", "-q")
        sh = []
        for name in ("fsmonitor", "clean", "process", "hook", "textconv", "include"):
            s = self.t / f"{name}.sh"
            s.write_text(f"#!/bin/sh\ntouch {self.marks}/{name}\ncat\n")
            s.chmod(0o755)
            sh.append(str(s))
        git(r, "config", "core.fsmonitor", sh[0])
        (r / ".gitattributes").write_text("*.txt filter=evil diff=evil\n*.dat filter=pevil\n")
        git(r, "config", "filter.evil.clean", sh[1])
        git(r, "config", "filter.evil.required", "true")
        git(r, "config", "filter.pevil.process", sh[2])
        git(r, "config", "diff.evil.textconv", sh[4])
        (r / ".git" / "info").mkdir(exist_ok=True)
        (r / ".git" / "info" / "attributes").write_text("*.md filter=inc\n")
        inc = self.t / "inc.cfg"
        inc.write_text(f'[filter "inc"]\n\tclean = {sh[5]}\n')
        git(r, "config", "include.path", str(inc))
        for hk in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction"):
            h = r / ".git" / "hooks" / hk
            h.write_text(f"#!/bin/sh\ntouch {self.marks}/hook\n")
            h.chmod(0o755)
        (r / "a.txt").write_text("A\n")
        (r / "d.dat").write_text("D\n")
        (r / "n.md").write_text("N\n")
        return r

    def marks_now(self):
        return sorted(p.name for p in self.marks.iterdir())


class SafeGit(World):
    def test_hostile_repo_runs_nothing(self):
        r = self.hostile(self.code / "evil")
        for m in self.marks.iterdir():
            m.unlink()
        rs = audit.inspect(str(r))
        self.assertIsNone(rs.error)
        self.assertTrue(any(f.startswith("uncommitted") for f in rs.findings), rs.findings)
        self.assertEqual(self.marks_now(), [], "repo config executed during audit")

    def test_plain_git_would_have_run_it(self):
        """Control: the fixture really is hostile (else the test above proves nothing)."""
        r = self.hostile(self.code / "evil")
        for m in self.marks.iterdir():
            m.unlink()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "status", "--porcelain"], cwd=str(r), env=env, capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        self.assertIn("fsmonitor", self.marks_now())
        # the fixture's fsmonitor answers "nothing changed", hiding the filters; switch only it off
        subprocess.run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"], cwd=str(r), env=env,
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        for m in ("clean", "process", "include"):
            self.assertIn(m, self.marks_now())

    def hostile_submodule(self):
        """Clean outer repo; the submodule's OWN config defines a clean filter."""
        sub = mkrepo(self.t / "subsrc")
        top = mkrepo(self.code / "outer", self.remote)
        git(top, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(sub), "sub")
        git(top, "commit", "-qm", "add sub")
        s = self.t / "subclean.sh"
        s.write_text(f"#!/bin/sh\ntouch {self.marks}/subclean\ncat\n")
        s.chmod(0o755)
        inner = top / "sub"
        (inner / ".gitattributes").write_text("*.txt filter=evil\n")
        git(inner, "config", "filter.evil.clean", str(s))
        (inner / "a.txt").write_text("A\n")  # same size: forces content hashing
        for m in self.marks.iterdir():
            m.unlink()
        return top, inner

    def test_hostile_submodule_runs_nothing(self):
        top, _ = self.hostile_submodule()
        safegit.safe_git(top, "status", "--porcelain")  # caller forgot --ignore-submodules
        rs = audit.inspect(str(top))
        self.assertIsNone(rs.error)
        self.assertEqual(self.marks_now(), [], "submodule config executed during audit")

    def test_plain_git_runs_the_submodule_filter(self):
        """Control: plain status (and 0.4.0's flags without --ignore-submodules) run it."""
        top, _ = self.hostile_submodule()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "status",
                        "--porcelain"], cwd=str(top), env=env, capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        self.assertIn("subclean", self.marks_now())

    def test_submodule_audited_as_its_own_repo_safely(self):
        _, inner = self.hostile_submodule()
        rs = audit.inspect(str(inner))  # .git is a file in a submodule; still a repo
        self.assertIsNone(rs.error)
        self.assertTrue(any(f.startswith("uncommitted") for f in rs.findings), rs.findings)
        self.assertEqual(self.marks_now(), [])

    def test_refuses_write_subcommands_and_non_repos(self):
        r = mkrepo(self.code / "r")
        for bad in (("commit", "-m", "x"), ("checkout", "main"), ("config", "x.y", "z"), ("gc",), ()):
            with self.assertRaises(safegit.GitError):
                safegit.safe_git(r, *bad)
        with self.assertRaises(safegit.GitError):
            safegit.safe_git(self.code, "status")  # no .git
        with self.assertRaises(safegit.GitError):
            safegit.safe_git(self.code / "missing", "status")

    def test_neutralizers_cover_include_and_skip_global(self):
        r = self.hostile(self.code / "evil")
        n = safegit.neutralizers(r)
        pairs = dict(zip(n[1::2], [None] * len(n)))
        keys = {p.split("=", 1)[0] for p in pairs}
        self.assertIn("filter.evil.clean", keys)
        self.assertIn("filter.pevil.process", keys)
        self.assertIn("filter.inc.clean", keys)  # reached through include.path
        self.assertIn("filter.evil.required=false", pairs)

    def test_timeout_is_an_error_not_a_hang(self):
        r = mkrepo(self.code / "r")
        with self.assertRaises(safegit.GitError):
            safegit.safe_git(r, "status", timeout=0.0001)


class Classify(World):
    def build_world(self):
        r = {}
        r["dirty"] = mkrepo(self.code / "dirty", self.remote)
        (r["dirty"] / "a.txt").write_text("dirty\n")
        r["unpushed"] = mkrepo(self.code / "unpushed", self.remote)
        (r["unpushed"] / "b.txt").write_text("b\n")
        git(r["unpushed"], "add", "-A")
        git(r["unpushed"], "commit", "-qm", "local only")
        r["noup"] = mkrepo(self.code / "noup", self.remote)
        git(r["noup"], "checkout", "-q", "-b", "feature")
        (r["noup"] / "c.txt").write_text("c\n")
        git(r["noup"], "add", "-A")
        git(r["noup"], "commit", "-qm", "feature work")
        r["stash"] = mkrepo(self.code / "stash", self.remote)
        (r["stash"] / "a.txt").write_text("stashed\n")
        git(r["stash"], "stash", "-q")
        r["clean"] = mkrepo(self.code / "clean", self.remote)
        r["inuse"] = mkrepo(self.code / "inuse", self.remote)
        (r["inuse"] / "a.txt").write_text("being edited\n")
        r["hostile"] = self.hostile(self.code / "nested" / "hostile")
        r["unowned"] = mkrepo(self.code / "group" / "unowned", self.remote)
        (r["unowned"] / "a.txt").write_text("nobody's\n")
        r["deep"] = mkrepo(self.code / "a" / "b" / "c" / "toodeep")
        (r["deep"] / "a.txt").write_text("x\n")
        recorded = [r[k] for k in ("dirty", "unpushed", "noup", "stash", "clean", "hostile")]
        self.session("dead-1", recorded, ended=True)
        self.session("live-1", [r["inuse"]])
        self.session("dead-2", [r["inuse"]], ended=True)
        for m in self.marks.iterdir():
            m.unlink()
        return r

    def test_fixture_matrix(self):
        self.build_world()
        plan = audit.build()
        cu = {Path(rs.repo).name: (sid, rs.findings) for sid, rs, _ in plan.catch_up}
        self.assertEqual(set(cu), {"dirty", "unpushed", "noup", "stash", "hostile"})
        self.assertTrue(all(sid == "dead-1" for sid, _ in cu.values()))
        self.assertIn("uncommitted: 1 modified", cu["dirty"][1])
        self.assertTrue(any("1 unpushed commit(s) on main" == f for f in cu["unpushed"][1]), cu["unpushed"])
        self.assertTrue(any("(no upstream)" in f and "feature" in f for f in cu["noup"][1]), cu["noup"])
        self.assertIn("1 stash entry", cu["stash"][1])
        self.assertEqual([Path(x).name for x, _ in plan.in_use], ["inuse"])  # ACTIVE recorder wins
        self.assertEqual([Path(rs.repo).name for rs in plan.unowned], ["unowned"])
        self.assertNotIn("toodeep", json.dumps(plan.as_dict()))  # beyond depth 3
        self.assertEqual(plan.clean, 1)
        self.assertEqual(self.marks_now(), [])

    def test_dry_run_writes_nothing(self):
        self.build_world()
        before = self.tracker.read_bytes()
        p = subprocess.run([sys.executable, str(BIN), "audit"], capture_output=True, text=True,
                           env={**os.environ}, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("dry run: nothing written", p.stdout)
        self.assertIn("unowned", p.stdout)
        self.assertEqual(self.tracker.read_bytes(), before)

    def test_idle_under_stale_limit_waits(self):
        r = mkrepo(self.code / "w", self.remote)
        (r / "a.txt").write_text("x\n")
        self.session("idle-1", [r], idle_s=30 * 60)
        plan = audit.build(stale_min=120)
        self.assertEqual(plan.catch_up, [])
        self.assertEqual([s for _, s in plan.waiting], ["idle-1"])
        self.session("idle-1", [r], idle_s=3 * 3600)
        plan = audit.build(stale_min=120)
        self.assertEqual([s for s, _, _ in plan.catch_up], ["idle-1"])

    def test_catch_up_path_marks_uninspected_repos_pending(self):
        r = mkrepo(self.code / "pd", self.remote)
        (r / "a.txt").write_text("x\n")
        self.session("idle-2", [r], idle_s=30 * 60)
        plan = audit.build(use_roots=False, stale_min=120)
        self.assertEqual((plan.waiting, [s for _, s in plan.pending]), ([], ["idle-2"]))


class Attribution(World):
    def sess(self, sid, start_ago, end_ago, hermes=False, writes=None):
        now = time.time()
        return audit.Sess(sid, now - start_ago, now - end_ago, hermes, writes or {})

    def test_write_blind_needs_exactly_one_spanning_session(self):
        r = mkrepo(self.code / "a", self.remote)
        (r / "a.txt").write_text("x\n")
        rs = audit.inspect(str(r))
        one = self.sess("cc-1", 600, 0)
        owned, rest = audit.attribute(rs, [one])
        self.assertEqual((list(owned), rest.owed), (["cc-1"], False))
        owned, rest = audit.attribute(rs, [one, self.sess("cc-2", 900, 0)])  # both span: ambiguous
        self.assertEqual((owned, rest.findings), ({}, ["uncommitted: 1 modified"]))
        owned, rest = audit.attribute(rs, [self.sess("cc-3", 7200, 3600)])  # ended before the edit
        self.assertEqual((owned, rest.findings), ({}, ["uncommitted: 1 modified"]))

    def test_hermes_hours_never_attribute_a_dirty_file(self):
        r = mkrepo(self.code / "b", self.remote)
        (r / "a.txt").write_text("x\n")
        owned, rest = audit.attribute(audit.inspect(str(r)), [self.sess("h-1", 600, 0, hermes=True)])
        self.assertEqual((owned, rest.findings), ({}, ["uncommitted: 1 modified"]))

    def test_untracked_dir_owned_by_writer_of_a_file_inside(self):
        r = mkrepo(self.code / "c", self.remote)
        (r / "new").mkdir()
        (r / "new" / "f.py").write_text("x\n")
        c = paths.canonical(str(r))
        h = self.sess("h-2", 600, 0, hermes=True, writes={os.path.join(c, "new", "f.py"): time.time()})
        owned, rest = audit.attribute(audit.inspect(c), [h])
        self.assertEqual((owned["h-2"].findings, rest.owed), (["uncommitted: 1 untracked"], False))

    def test_stash_and_commit_by_time(self):
        r = mkrepo(self.code / "d", self.remote)
        (r / "a.txt").write_text("s\n")
        git(r, "stash", "-q")
        (r / "b.txt").write_text("b\n")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "local")
        rs = audit.inspect(str(r))
        owned, rest = audit.attribute(rs, [self.sess("h-3", 600, 0, hermes=True), self.sess("old", 9000, 7200)])
        self.assertEqual(owned["h-3"].findings, ["1 unpushed commit(s) on main", "1 stash entry"])
        self.assertFalse(rest.owed)

    def test_stale_write_loses_to_newer_edit(self):
        r = mkrepo(self.code / "f", self.remote)
        (r / "a.txt").write_text("today\n")
        c = paths.canonical(str(r))
        h = self.sess("h-old", 40 * 86400, 39 * 86400, hermes=True,
                      writes={os.path.join(c, "a.txt"): time.time() - 39 * 86400})
        cc = self.sess("cc-now", 600, 0)
        owned, rest = audit.attribute(audit.inspect(c), [h, cc])
        self.assertEqual((list(owned), rest.owed), (["cc-now"], False))

    def test_hermes_shell_edit_rivals_claude_code(self):
        r = mkrepo(self.code / "g", self.remote)
        (r / "a.txt").write_text("sed -i\n")
        owned, rest = audit.attribute(audit.inspect(str(r)),
                                      [self.sess("h-4", 600, 0, hermes=True), self.sess("cc-4", 600, 0)])
        self.assertEqual((owned, rest.findings), ({}, ["uncommitted: 1 modified"]))

    def test_untracked_dir_time_is_newest_file_inside(self):
        r = mkrepo(self.code / "h", self.remote)
        (r / "new").mkdir()
        (r / "new" / "f.py").write_text("x\n")
        old = time.time() - 5 * 86400
        os.utime(r / "new", (old, old))
        os.utime(r / "new" / "f.py", (old, old))
        (r / "new" / "f.py").write_text("edited today\n")  # dir mtime unchanged
        os.utime(r / "new", (old, old))
        owned, _ = audit.attribute(audit.inspect(str(r)), [self.sess("cc-5", 600, 0)])
        self.assertEqual(list(owned), ["cc-5"])

    def test_deleted_file_never_given_to_recorded_writer(self):
        r = mkrepo(self.code / "d", self.remote)
        (r / "a.txt").unlink()
        c = paths.canonical(str(r))
        h = self.sess("h-del", 40 * 86400, 39 * 86400, hermes=True,
                      writes={os.path.join(c, "a.txt"): time.time() - 39 * 86400})
        owned, rest = audit.attribute(audit.inspect(c), [h])
        self.assertEqual((owned, rest.owed), ({}, True))

    def test_untracked_dir_too_big_has_no_time(self):
        r = mkrepo(self.code / "big", self.remote)
        (r / "new").mkdir()
        for i in range(5001):
            (r / "new" / f"{i}").touch()
        self.assertIsNone(audit._mtime(str(r), "new/"))
        self.assertIsNotNone(audit._mtime(str(r), "a.txt"))

    def test_copy_status_skips_source_field(self):
        self.assertEqual(audit._status_paths("C  b.txt\0a.txt\0?? n o\0 M x\0"),
                         [("b.txt", "modified"), ("n o", "untracked"), ("x", "modified")])

    def test_writes_cap_keeps_newest(self):
        state.update("cap-1", writes=[f"/x/{i}" for i in range(state.WRITES_MAX)])
        time.sleep(0.01)
        state.update("cap-1", writes=["/x/new"])
        w = state.load("cap-1")["writes"]
        self.assertEqual(len(w), state.WRITES_MAX)
        self.assertIn("/x/new", w)

    def test_rename_is_one_dirty_path(self):
        r = mkrepo(self.code / "e", self.remote)
        git(r, "mv", "a.txt", "renamed file.txt")
        rs = audit.inspect(str(r))
        self.assertEqual([(p, k) for p, k, _ in rs.dirty], [("renamed file.txt", "modified")])


class LocalBranches(World):
    def test_old_branch_is_unattributed_not_owed(self):
        r = mkrepo(self.code / "lb", self.remote)
        git(r, "checkout", "-q", "-b", "old-backup")
        (r / "o.txt").write_text("o\n")
        git(r, "add", "-A")
        env_date = {"GIT_COMMITTER_DATE": "2020-01-01T00:00:00", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00"}
        subprocess.run(["git", *GIT_ID, "commit", "-qm", "old"], cwd=str(r), check=True,
                       env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **env_date})
        git(r, "checkout", "-q", "main")
        git(r, "checkout", "-q", "-b", "fresh")
        (r / "f.txt").write_text("f\n")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "fresh")
        rs = audit.inspect(str(r))
        self.assertEqual(len(rs.findings), 2)  # facts: both branches are unpushed
        sess = audit.Sess("s1", time.time() - 3600, time.time(), False)
        owned, rest = audit.attribute(rs, [sess])
        self.assertEqual(owned["s1"].findings, ["1 unpushed commit(s) on fresh (no upstream)"])
        self.assertEqual(rest.findings, ["1 unpushed commit(s) on old-backup (no upstream)"])


class Unpushed(World):
    def test_stale_upstream_is_a_note_not_owed_work(self):
        """hermes-agent-fork shape: main tracks origin (stale) but every commit is on `upstream`."""
        src = mkrepo(self.code / "src", self.remote)
        for i in range(3):
            (src / f"f{i}").write_text(str(i))
            git(src, "add", "-A")
            git(src, "commit", "-qm", f"c{i}")
        up = self.remote / "upstream.git"
        subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
        git(src, "remote", "add", "upstream", str(up))
        git(src, "push", "-q", "upstream", "HEAD:main")
        git(src, "fetch", "-q", "upstream")
        rs = audit.inspect(str(src))
        self.assertEqual(rs.findings, [], rs.findings)
        self.assertEqual(len(rs.notes), 1)
        self.assertIn("3 ahead of origin/main", rs.notes[0])
        self.assertIn("upstream ref stale", rs.notes[0])
        (src / "g").write_text("g")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "really local")
        rs = audit.inspect(str(src))
        self.assertEqual(rs.findings, ["1 unpushed commit(s) on main"])

    def test_ignore_branches_opt_in_and_listed_by_check(self):
        r = mkrepo(self.code / "r", self.remote)
        git(r, "checkout", "-q", "-b", "backup/old")
        (r / "x").write_text("x")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "x")
        self.assertTrue(any("backup/old" in f for f in audit.inspect(str(r)).findings))  # default: shown
        self.root_cfg([{"path": str(self.code)}])
        cfg = json.loads((state.state_dir() / "config.json").read_text())
        cfg["audit"]["ignore_branches"] = ["backup/*"]
        (state.state_dir() / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(audit.inspect(str(r)).findings, [])
        p = subprocess.run([sys.executable, str(BIN), "check"], capture_output=True, text=True,
                           env={**os.environ}, timeout=60)
        self.assertIn("audit.ignore_branches hides branches matching: backup/*", p.stdout)

    def test_ignore_repos_skips_and_closes_open_entry(self):
        r = mkrepo(self.code / "patched", self.remote)
        self.session("dead-1", [r], ended=True)
        (r / "a.txt").write_text("live patch\n")
        self.session("dead-1", [r], ended=True)
        audit.apply(audit.build(), self.tracker)
        self.assertIn("audit: ", self.tracker.read_text())
        self.root_cfg([{"path": str(self.code)}])
        cfg = json.loads((state.state_dir() / "config.json").read_text())
        cfg["audit"]["ignore_repos"] = [str(self.code).upper() + "/PATCH*"]  # glob, case-folded on macOS
        (state.state_dir() / "config.json").write_text(json.dumps(cfg))
        if sys.platform != "darwin":
            cfg["audit"]["ignore_repos"] = [str(self.code) + "/patch*"]
            (state.state_dir() / "config.json").write_text(json.dumps(cfg))
        plan = audit.build()
        self.assertEqual((plan.ignored, plan.catch_up), ([paths.canonical(str(r))], []))
        audit.apply(plan, self.tracker)
        e = [x for x in core.right_now(core.parse(self.tracker.read_text())).entries if x.session == "dead-1"]
        self.assertEqual([progress.lifecycle(x.text).state for x in e], ["done"])
        p = subprocess.run([sys.executable, str(BIN), "check"], capture_output=True, text=True,
                           env={**os.environ}, timeout=60)
        self.assertIn("audit.ignore_repos hides repos matching: ", p.stdout)

    def test_dry_run_flag_is_accepted_and_wins(self):
        r = mkrepo(self.code / "p", self.remote)
        (r / "a.txt").write_text("d\n")
        self.session("dead-1", [r], ended=True)
        before = self.tracker.read_bytes()
        p = subprocess.run([sys.executable, str(BIN), "audit", "--dry-run", "--apply"], capture_output=True,
                           text=True, env={**os.environ}, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("dry run: nothing written", p.stdout)
        self.assertEqual(self.tracker.read_bytes(), before)


class AlreadyOnDefault(World):
    """Q1 rules: patch-id twins on the default branch -> listed separately, never dropped."""

    def setUp(self):
        super().setUp()
        self.r = mkrepo(self.code / "r", self.remote)

    def branch(self, name, files):
        git(self.r, "checkout", "-q", "-b", name, "main")
        for f in files:
            (self.r / f).write_text(f + "\n")
            git(self.r, "add", "-A")
            git(self.r, "commit", "-qm", f"add {f}")
        git(self.r, "checkout", "-q", "main")
        # move main on, so picks get new parents (a same-second pick onto the same
        # parent reproduces the identical commit and proves nothing about patch-ids)
        (self.r / f"main-{name}").write_text("m\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", f"main moves on ({name})")

    def mainline(self, name):
        (self.r / f"main-{name}").write_text("m\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", f"main: {name}")

    def test_rebased_branch_listed_already_on_main(self):
        self.branch("feat", ["x1", "x2"])
        for c in git(self.r, "rev-list", "--reverse", "main..feat").split():
            git(self.r, "cherry-pick", c)  # rebase onto main: new SHAs, same patches
        git(self.r, "push", "-q")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.findings, [])
        self.assertEqual(rs.merged, [("feat", "2", "origin/main")])

    def test_squash_edited_on_landing_stays_unpushed(self):
        self.branch("sq", ["s1", "s2", "s3"])
        git(self.r, "merge", "-q", "--squash", "sq")
        (self.r / "s3").write_text("edited while landing\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "squash, edited")
        git(self.r, "push", "-q")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.merged, [])
        self.assertIn("3 unpushed commit(s) on sq (no upstream)", rs.findings)

    def test_failed_squash_check_keeps_it_unpushed(self):
        def boom(*a):
            raise safegit.GitError("timed out after 5s")
        self.assertFalse(audit._squash_merged(boom, "refs/remotes/origin/main", "x"))
        self.assertFalse(audit._squash_merged(lambda *a: "", None, "x"))

    def test_squash_merged_branch_listed_already_on_main(self):
        self.branch("sq", ["s1", "s2", "s3"])
        self.mainline("after")  # the default branch moved on before the squash landed
        git(self.r, "merge", "-q", "--squash", "sq")
        git(self.r, "commit", "-qm", "squash (#1)")
        self.mainline("later")
        git(self.r, "push", "-q")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.findings, [])
        self.assertEqual(rs.merged, [("sq", "3", "origin/main")])

    def test_work_added_after_squash_stays_unpushed(self):
        self.branch("sq", ["s1", "s2"])
        git(self.r, "merge", "-q", "--squash", "sq")
        git(self.r, "commit", "-qm", "squash")
        git(self.r, "push", "-q")
        git(self.r, "checkout", "-q", "sq")
        (self.r / "s3").write_text("late\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "late fix")
        git(self.r, "checkout", "-q", "main")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.merged, [])
        self.assertIn("3 unpushed commit(s) on sq (no upstream)", rs.findings)

    def test_unique_commits_stay_unpushed(self):
        self.branch("uniq", ["u1"])
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.merged, [])
        self.assertIn("1 unpushed commit(s) on uniq (no upstream)", rs.findings)

    def test_partial_match_stays_unpushed(self):
        self.branch("half", ["h1", "h2"])
        first = git(self.r, "rev-list", "--reverse", "main..half").split()[0]
        git(self.r, "cherry-pick", first)
        git(self.r, "push", "-q")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.merged, [])
        self.assertIn("2 unpushed commit(s) on half (no upstream)", rs.findings)

    def test_failed_check_keeps_it_unpushed(self):
        def boom(*a):
            raise safegit.GitError("timed out after 5s")
        self.assertFalse(audit._already_on(boom, "refs/remotes/origin/main", "x"))
        self.assertFalse(audit._already_on(lambda *a: "0", None, "x"))  # no default branch known

    def test_listed_in_output_never_dropped(self):
        self.branch("feat", ["x1"])
        git(self.r, "cherry-pick", git(self.r, "rev-parse", "feat").strip())
        git(self.r, "push", "-q")
        p = subprocess.run([sys.executable, str(BIN), "audit"], capture_output=True, text=True,
                           env={**os.environ}, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already on the default branch", p.stdout)
        self.assertIn("feat (1 commit(s), already on origin/main)", p.stdout)
        p = subprocess.run([sys.executable, str(BIN), "audit", "--json"], capture_output=True, text=True,
                           env={**os.environ}, timeout=60)
        d = json.loads(p.stdout)
        self.assertEqual(d["already_on_default"][0]["branches"][0]["branch"], "feat")


class Apply(World):
    def setUp(self):
        super().setUp()
        self.r = mkrepo(self.code / "proj", self.remote)
        (self.r / "a.txt").write_text("dirty\n")
        self.session("dead-1", [self.r], ended=True)

    def entries(self):
        rn = core.right_now(core.parse(self.tracker.read_text()))
        return [e for e in rn.entries if audit.HEADLINE_TAG in e.head]

    def test_upsert_once_then_unchanged(self):
        c1 = audit.apply(audit.build(), self.tracker)
        self.assertEqual(c1["added"], 1)
        mtime = self.tracker.stat().st_mtime_ns
        c2 = audit.apply(audit.build(), self.tracker)
        self.assertEqual((c2["added"], c2["updated"], c2["unchanged"]), (0, 0, 1))
        self.assertEqual(self.tracker.stat().st_mtime_ns, mtime, "second audit rewrote the file")
        es = self.entries()
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0].session, "dead-1")  # tagged with the DEAD session
        self.assertNotIn("took over", es[0].head)
        self.assertEqual(self.tracker.stat().st_mode & 0o777, 0o600)

    def test_update_in_place_when_findings_change(self):
        audit.apply(audit.build(), self.tracker)
        eid = self.entries()[0].id
        (self.r / "new.txt").write_text("n\n")
        c = audit.apply(audit.build(), self.tracker)
        self.assertEqual(c["updated"], 1)
        es = self.entries()
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0].id, eid)
        self.assertIn("1 untracked", es[0].text)

    def test_resolved_marked_done_not_deleted(self):
        audit.apply(audit.build(), self.tracker)
        git(self.r, "checkout", "-q", "--", "a.txt")
        c = audit.apply(audit.build(), self.tracker)
        self.assertEqual(c["done"], 1)
        es = self.entries()
        self.assertEqual(len(es), 1)
        self.assertEqual(progress.lifecycle(es[0].text).state, "done")
        c = audit.apply(audit.build(), self.tracker)
        self.assertEqual(c["done"], 0)  # idempotent

    def test_open_entry_rechecked_while_repo_is_in_use(self):
        """An active session in the repo used to freeze the entry; it went on
        claiming work that had been committed and pushed."""
        audit.apply(audit.build(), self.tracker)
        self.session("live-1", [self.r])  # a live session now works in the repo
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "commit the dead session's work")
        git(self.r, "push", "-q")
        plan = audit.build(tracked=audit.tracked_repos(self.tracker))
        self.assertIn(paths.canonical(str(self.r)), plan.checked)
        c = audit.apply(plan, self.tracker)
        self.assertEqual(c["done"], 1)
        self.assertEqual(progress.lifecycle(self.entries()[0].text).state, "done")

    def test_recheck_never_adds_an_entry_while_repo_is_in_use(self):
        r = mkrepo(self.code / "busy", self.remote)
        (r / "b.txt").write_text("dead session's\n")
        self.session("dead-3", [r], ended=True, idle_s=3 * 3600)  # its hour holds b.txt
        self.session("live-1", [r])
        plan = audit.build(tracked={paths.canonical(str(r))})  # re-checked, e.g. another entry is open there
        self.assertIn(paths.canonical(str(r)), plan.checked)
        audit.apply(plan, self.tracker)  # setUp's dead-1 repo is not in use: that one is added
        self.assertNotIn("[session dead-3 #", self.tracker.read_text(), "a repo in use gets no new entry")

    def test_recheck_keeps_unprovable_work_marked_and_hand_lines(self):
        audit.apply(audit.build(), self.tracker)
        e = self.entries()[0]
        text = self.tracker.read_text().replace(e.text, e.text + "\nwaiting on: Andrew decides\n(took over x-1)")
        self.tracker.write_text(text)
        self.session("live-1", [self.r])  # now two sessions span the file's hour: not provable
        c = audit.apply(audit.build(tracked=audit.tracked_repos(self.tracker)), self.tracker)
        self.assertEqual(c["updated"], 1)
        t = self.entries()[0].text
        self.assertIn("- [ ] uncommitted: 1 modified" + audit.UNPROVEN, t)
        self.assertIn("waiting on: Andrew decides", t)
        self.assertIn("(took over x-1)", t)
        self.assertEqual(self.entries()[0].head, e.head)  # date and id unchanged
        c = audit.apply(audit.build(tracked=audit.tracked_repos(self.tracker)), self.tracker)
        self.assertEqual((c["updated"], c["done"]), (0, 0))  # stable

    def test_entry_says_idle_not_ended(self):
        r = mkrepo(self.code / "idle", self.remote)
        (r / "a.txt").write_text("x\n")
        self.session("idle-9", [r], idle_s=3 * 3600)
        audit.apply(audit.build(stale_min=120), self.tracker)
        e = next(e for e in self.entries() if e.session == "idle-9")
        self.assertIn("(session idle since ", e.text)
        self.assertNotIn("ended", e.text)
        self.assertIn("(session ended ", next(e for e in self.entries() if e.session == "dead-1").text)

    def test_case_variant_spellings_are_one_repo(self):
        """macOS: ~/Code/x and ~/code/x are one directory. One entry, and an
        older entry under the other spelling is closed, not duplicated."""
        if sys.platform != "darwin":
            self.skipTest("case-insensitive filesystem")
        flipped = str(self.r).replace("/proj", "/PROJ")
        self.assertEqual(paths.canonical(flipped), paths.canonical(str(self.r)))
        self.session("dead-2", [flipped], ended=True, idle_s=2 * 3600)  # its hour holds the change
        rec = audit.recorded()
        self.assertEqual(list(rec["dead-2"].repos), [paths.canonical(str(self.r))])
        audit.apply(audit.build(), self.tracker)
        open_ = [e for e in self.entries() if progress.lifecycle(e.text).state != "done"]
        self.assertEqual(len(open_), 1, [e.head for e in self.entries()])

    def test_newer_recorder_does_not_take_over(self):
        """A later session that only recorded the repo does not inherit its
        work. Two write-blind sessions whose hours both span the change: no
        single maker, so the work is unattributed, and dead-1's entry stays
        open (it may be its work) until the repo is clean."""
        audit.apply(audit.build(), self.tracker)
        old = self.entries()[0]
        self.session("dead-3", [self.r], ended=True)  # same hour as dead-1
        plan = audit.build()
        self.assertEqual(plan.catch_up, [])
        self.assertEqual([rs.findings for rs in plan.unattributed], [["uncommitted: 1 modified"]])
        audit.apply(plan, self.tracker)
        by = {e.session: progress.lifecycle(e.text).state for e in self.entries()}
        self.assertEqual(by, {"dead-1": "active"}, old.head)
        git(self.r, "checkout", "-q", "--", "a.txt")
        audit.apply(audit.build(), self.tracker)
        self.assertEqual({e.session: progress.lifecycle(e.text).state for e in self.entries()}, {"dead-1": "done"})

    def test_recorded_write_wins_over_hours(self):
        """#12fbdf88: the session that wrote the file owns it, whoever recorded
        the repo last; another dead session's entry for that work closes."""
        audit.apply(audit.build(), self.tracker)
        self.session("herm-1", [self.r], ended=True, harness="hermes")
        state.update("herm-1", writes=[os.path.join(paths.canonical(str(self.r)), "a.txt")])
        d = json.loads(state.session_path("herm-1").read_text())
        d["ended_at"] = time.time()
        state.session_path("herm-1").write_text(json.dumps(d))
        audit.apply(audit.build(), self.tracker)
        by = {e.session: progress.lifecycle(e.text).state for e in self.entries()}
        self.assertEqual(by, {"dead-1": "done", "herm-1": "active"})

    def test_hermes_session_owns_only_its_writes(self):
        """A Hermes session that recorded the repo but wrote other files does
        not own the dirty file (it records writes, so hours don't count)."""
        r = mkrepo(self.code / "h", self.remote)
        (r / "a.txt").write_text("not mine\n")
        (r / "mine.txt").write_text("mine\n")
        self.session("herm-2", [r], ended=True, harness="hermes")
        state.update("herm-2", writes=[os.path.join(paths.canonical(str(r)), "mine.txt")])
        plan = audit.build()
        mine = [(s, rs.findings) for s, rs, _ in plan.catch_up if rs.repo == paths.canonical(str(r))]
        self.assertEqual(mine, [("herm-2", ["uncommitted: 1 untracked"])])
        self.assertIn(["uncommitted: 1 modified"],
                      [rs.findings for rs in plan.unattributed if rs.repo == paths.canonical(str(r))])

    def test_reopen_when_work_returns(self):
        audit.apply(audit.build(), self.tracker)
        git(self.r, "checkout", "-q", "--", "a.txt")
        audit.apply(audit.build(), self.tracker)
        (self.r / "a.txt").write_text("again\n")
        c = audit.apply(audit.build(), self.tracker)
        self.assertEqual(c["updated"], 1)
        es = self.entries()
        self.assertEqual(len(es), 1)
        self.assertEqual(progress.lifecycle(es[0].text).state, "active")

    def test_hand_written_entry_never_touched(self):
        self.tracker.write_text("## Right now\n\n**2026-10-01 10:00 [session dead-1 #abcdef] — audit: mine.** keep\n")
        git(self.r, "checkout", "-q", "--", "a.txt")
        audit.apply(audit.build(), self.tracker)
        self.assertIn("— audit: mine.** keep", self.tracker.read_text())
        self.assertNotIn("status: done", self.tracker.read_text())

    def test_unowned_never_written(self):
        u = mkrepo(self.code / "orphan", self.remote)
        (u / "a.txt").write_text("x\n")
        audit.apply(audit.build(), self.tracker)
        self.assertNotIn("orphan", self.tracker.read_text())

    def test_secret_branch_name_redacted(self):
        git(self.r, "checkout", "-q", "-b", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2")
        (self.r / "s.txt").write_text("s\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-qm", "x")
        audit.apply(audit.build(), self.tracker)
        text = self.tracker.read_text()
        self.assertNotIn("A1b2C3d4E5f6G7h8I9j0K1l2", text)
        self.assertIn("[redacted: GitHub token]", text)


class CatchUp(World):
    def test_session_start_runs_catch_up_once_and_fails_open(self):
        r = mkrepo(self.code / "proj", self.remote)
        (r / "a.txt").write_text("dirty\n")
        self.session("dead-1", [r], ended=True)
        rc = hook.run(["session-start"], stdin=io.StringIO(json.dumps(
            {"session_id": "new-1", "cwd": str(self.t), "source": "startup"})))
        self.assertEqual(rc, 0)
        self.assertIn("[session dead-1 #", self.tracker.read_text())
        before = self.tracker.read_bytes()
        (r / "b.txt").write_text("more\n")
        hook.run(["session-start"], stdin=io.StringIO(json.dumps({"session_id": "new-2", "source": "startup"})))
        self.assertEqual(self.tracker.read_bytes(), before, "rate limit: second start within 10 min ran again")

    def test_session_end_inside_rate_limit_window_still_caught_up(self):
        # e2e step 5: quit + reopen within 10 min of the previous catch-up.
        hook.run(["session-start"], stdin=io.StringIO(json.dumps({"session_id": "first-1", "source": "startup"})))
        self.assertTrue((state.state_dir() / "last-catch-up").exists())
        r = mkrepo(self.code / "proj2", self.remote)
        (r / "a.txt").write_text("dirty\n")
        self.session("dead-2", [r], ended=True)
        hook.run(["session-start"], stdin=io.StringIO(json.dumps(
            {"session_id": "new-3", "cwd": str(r), "source": "startup"})))
        self.assertIn("[session dead-2 #", self.tracker.read_text())

    def test_new_session_opening_in_repo_does_not_take_it_over(self):
        # e2e step 5: dead session left R dirty; a new session starts IN R.
        r = mkrepo(self.code / "proj", self.remote)
        (r / "a.txt").write_text("dirty\n")
        self.session("dead-1", [r], ended=True)
        rc = hook.run(["session-start"], stdin=io.StringIO(json.dumps(
            {"session_id": "new-1", "cwd": str(r), "source": "startup"})))
        self.assertEqual(rc, 0)
        text = self.tracker.read_text()
        self.assertIn("[session dead-1 #", text)
        self.assertNotIn("[session new-1 #", text)
        self.assertNotIn(str(r), (state.load("new-1").get("repos") or {}))

    def test_catch_up_skips_lock_when_nothing_due(self):
        r = mkrepo(self.code / "cl", self.remote)
        self.session("dead-1", [r], ended=True)  # clean repo, no open entry
        before = self.tracker.stat().st_mtime_ns
        self.assertEqual(audit.catch_up(min_interval_s=0), {})
        self.assertEqual(self.tracker.stat().st_mtime_ns, before)

    def test_catch_up_closes_entry_when_repo_cleaned(self):
        r = mkrepo(self.code / "cc", self.remote)
        self.session("dead-1", [r], ended=True)
        (r / "a.txt").write_text("d\n")
        self.session("dead-1", [r], ended=True)  # back-date the edit into its hour
        self.assertEqual(audit.catch_up(min_interval_s=0).get("added"), 1)
        git(r, "checkout", "-q", "--", "a.txt")
        self.assertEqual(audit.catch_up(min_interval_s=0).get("done"), 1)  # _closable let it take the lock
        e = [e for e in core.right_now(core.parse(self.tracker.read_text())).entries if e.session == "dead-1"]
        self.assertEqual([progress.lifecycle(x.text).state for x in e], ["done"])

    def test_catch_up_skips_scan_roots(self):
        u = mkrepo(self.code / "orphan", self.remote)
        (u / "a.txt").write_text("x\n")
        self.assertEqual(audit.catch_up(), {})
        self.assertNotIn("orphan", self.tracker.read_text())

    def test_catch_up_error_logged_not_raised(self):
        self.tracker.unlink()
        self.tracker.mkdir()  # unreadable as a file
        self.session("dead-1", [mkrepo(self.code / "p", self.remote)], ended=True)
        (self.code / "p" / "a.txt").write_text("d\n")
        self.assertIsNone(audit.catch_up())


class HermesRecording(World):
    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "inflight_plugin_rec", ROOT / "adapters" / "hermes" / "plugin" / "__init__.py")
        self.p = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.p)
        self.r = mkrepo(self.code / "proj")
        (self.r / "src").mkdir()

    def test_path_and_workdir_recorded_rate_limited(self):
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(self.r / "src" / "x.py")},
                                        result="{}", session_id="hsess-1", tool_call_id="c1")
        repos = state.load("hsess-1").get("repos", {})
        self.assertEqual(list(repos), [str(self.r)])
        self.assertEqual(state.load("hsess-1").get("harness"), "hermes")
        hb = state.load("hsess-1")["heartbeat_at"]
        self.p.on_transform_tool_result(tool_name="terminal", args={"command": "ls", "workdir": str(self.r)},
                                        result="{}", session_id="hsess-1", tool_call_id="c2")
        self.assertEqual(state.load("hsess-1")["heartbeat_at"], hb, "second write within a minute")

    def test_write_tools_record_written_files(self):
        c = paths.canonical(str(self.r))
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(self.r / "src" / "x.py")},
                                        result="{}", session_id="hsess-3", tool_call_id="c1")
        body = f"*** Begin Patch\n*** Update File: {self.r}/a.txt\n@@\n-a\n+b\n" \
               f"*** Move File: {self.r}/m.txt -> {self.r}/n.txt\n*** End Patch\n"
        self.p.on_transform_tool_result(tool_name="patch", args={"mode": "patch", "patch": body},
                                        result="{}", session_id="hsess-3", tool_call_id="c2")
        self.p.on_transform_tool_result(tool_name="terminal", args={"command": f"echo > {self.r}/t.txt"},
                                        result="{}", session_id="hsess-3", tool_call_id="c3")
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": "/tmp/not-a-repo-file.txt"},
                                        result="{}", session_id="hsess-3", tool_call_id="c4")
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": "rel.txt"},
                                        result="{}", session_id="hsess-3", tool_call_id="c5")
        for i, res in enumerate(('{"error": "refused"}', '{"success": false, "error": "hunk mismatch"}')):
            self.p.on_transform_tool_result(tool_name="patch", args={"path": str(self.r / f"fail{i}.txt")},
                                            result=res, session_id="hsess-3", tool_call_id=f"f{i}")
        link = self.t / "link"
        link.symlink_to(self.r)
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(link / "via-link.txt")},
                                        result="{}", session_id="hsess-3", tool_call_id="c6")
        w = sorted(state.load("hsess-3").get("writes", {}))
        self.assertEqual(w, sorted(os.path.join(c, f) for f in ("src/x.py", "a.txt", "m.txt", "n.txt",
                                                                 "via-link.txt")))

    def test_repeat_write_rate_limited(self):
        f = str(self.r / "rl.txt")
        for i in range(3):
            self.p.on_transform_tool_result(tool_name="write_file", args={"path": f}, result="{}",
                                            session_id="hsess-rl", tool_call_id=f"r{i}")
            if i == 0:
                first = state.load("hsess-rl")["writes"]
        self.assertEqual(state.load("hsess-rl")["writes"], first)

    @unittest.skipUnless(sys.platform == "darwin", "case-insensitive filesystem")
    def test_case_variant_leaf_canonicalized(self):
        (self.r / "README.md").write_text("x\n")
        c = paths.canonical(str(self.r))
        self.assertEqual(self.p.written_files("write_file", "", {"path": str(self.r / "README.MD")}),
                         [os.path.join(c, "README.md")])

    def test_relative_v4a_path_resolved_against_session_cwd(self):
        c = paths.canonical(str(self.r))
        self.assertEqual(self.p.written_files("patch", str(self.r), {"mode": "patch",
                         "patch": "*** Begin Patch\n*** Add File: src/new.py\n+x\n*** End Patch\n"}),
                         [os.path.join(c, "src", "new.py")])
        self.assertEqual(self.p.written_files("patch", "", {"path": "src/new.py"}), [])  # no cwd: dropped

    def test_command_text_never_parsed_and_bad_sid_ignored(self):
        self.p.on_transform_tool_result(tool_name="terminal", args={"command": f"cd {self.r} && git status"},
                                        result="{}", session_id="hsess-2", tool_call_id="c1")
        self.assertEqual(state.load("hsess-2").get("repos"), None)
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(self.r / "x")},
                                        result="{}", session_id="../evil", tool_call_id="c3")
        self.assertFalse(any(state.sessions_dir().glob("*evil*")))


if __name__ == "__main__":
    unittest.main()
