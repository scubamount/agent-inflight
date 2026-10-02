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

from agent_inflight import audit, core, hook, progress, safegit, state  # noqa: E402

SCRUB = ("HERMES_HOME", "HERMES_ROOT", "INFLIGHT_FILE", "HERMES_SESSION_ID", "INFLIGHT_SESSION_ID",
         "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "INFLIGHT_HOME", "PYTHONPATH")
GIT_ID = ("-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false",
          "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main")


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), env=env, capture_output=True, text=True,
                          check=True, timeout=30).stdout


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

    def session(self, sid: str, repos, ended: bool = False, idle_s: float = 0):
        for r in repos:
            state.update(sid, repo=str(r))
        p = state.session_path(sid)
        d = json.loads(p.read_text())
        d["heartbeat_at"] = time.time() - idle_s
        if ended:
            d["ended_at"] = time.time() - idle_s
            d["end_reason"] = "end"
        p.write_text(json.dumps(d))

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
        subprocess.run(["git", "status", "--porcelain"], cwd=str(r), env=env, capture_output=True, timeout=30)
        self.assertIn("fsmonitor", self.marks_now())
        # the fixture's fsmonitor answers "nothing changed", hiding the filters; switch only it off
        subprocess.run(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"], cwd=str(r), env=env,
                       capture_output=True, timeout=30)
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
                        "--porcelain"], cwd=str(top), env=env, capture_output=True, timeout=30)
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

    def test_rebased_branch_listed_already_on_main(self):
        self.branch("feat", ["x1", "x2"])
        for c in git(self.r, "rev-list", "--reverse", "main..feat").split():
            git(self.r, "cherry-pick", c)  # rebase onto main: new SHAs, same patches
        git(self.r, "push", "-q")
        rs = audit.inspect(str(self.r))
        self.assertEqual(rs.findings, [])
        self.assertEqual(rs.merged, [("feat", "2", "origin/main")])

    def test_squash_of_several_commits_stays_unpushed(self):
        self.branch("sq", ["s1", "s2", "s3"])
        git(self.r, "merge", "-q", "--squash", "sq")
        git(self.r, "commit", "-qm", "squash")
        git(self.r, "push", "-q")
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

    def test_command_text_never_parsed_and_bad_sid_ignored(self):
        self.p.on_transform_tool_result(tool_name="terminal", args={"command": f"cd {self.r} && git status"},
                                        result="{}", session_id="hsess-2", tool_call_id="c1")
        self.assertEqual(state.load("hsess-2").get("repos"), None)
        self.p.on_transform_tool_result(tool_name="write_file", args={"path": str(self.r / "x")},
                                        result="{}", session_id="../evil", tool_call_id="c3")
        self.assertFalse(any(state.sessions_dir().glob("*evil*")))


if __name__ == "__main__":
    unittest.main()
