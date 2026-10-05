"""`inflight audit`: owed git work (dirty, unpushed, stashed) per session.

Two sources of repos, never mixed up:

  recorded   repos a session touched, from hook state
             (<state>/sessions/<id>.json `repos`; written by `inflight hook`
             and by the Hermes plugin after each tool call)
  roots      repos found under the configured scan roots
             (config.json `audit.roots`, default [{"path": "~/code", "depth": 3}])

How each finding is used:

  catch-up   a recorded repo with owed work whose LAST recording session is
             ENDED, or IDLE longer than --stale-min (default 120). One entry
             per repo, tagged with the DEAD session's id, written
             under `## Right now` with --apply. Re-running updates the same
             entry in place; once the repo is clean the entry is marked done,
             never deleted. Taking the work over stays explicit
             (`(took over <id>)`); audit never writes that.
  in use     a repo some ACTIVE session recorded: skipped, no entry.
  unowned    a roots repo with owed work that no session recorded: listed
             only. It is never tagged to a session and never written.

All git runs through safegit.safe_git (repo config can't execute). Text that
looks like a credential (branch names) is replaced with `[redacted: <kind>]`.
Dry run is the default; nothing is written without --apply.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import core, paths, progress, safety, state
from .safegit import GitError, neutralizers, safe_git

DEFAULT_ROOTS = [{"path": "~/code", "depth": 3}]
STALE_MIN = 120
HEADLINE_TAG = "audit:"
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "site-packages"}


# ---------------------------------------------------------------- config

def roots() -> List[Tuple[Path, int]]:
    from . import plugins  # config.json lives with the plugin allowlist
    cfg = plugins._load_config().get("audit", {})
    raw = cfg.get("roots", DEFAULT_ROOTS) if isinstance(cfg, dict) else DEFAULT_ROOTS
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, str):
            r = {"path": r}
        p = r.get("path") if isinstance(r, dict) else None
        if isinstance(p, str):
            depth = r.get("depth", 3)
            out.append((Path(os.path.expanduser(p)), depth if isinstance(depth, int) else 3))
    return out


def ignore_branches() -> List[str]:
    """Opt-in glob patterns (config.json audit.ignore_branches); empty by default.
    `inflight check` lists them so nothing is hidden silently."""
    from . import plugins
    cfg = plugins._load_config().get("audit", {})
    v = cfg.get("ignore_branches", []) if isinstance(cfg, dict) else []
    return [p for p in v if isinstance(p, str) and p] if isinstance(v, list) else []


def scan(root: Path, depth: int) -> Iterator[Path]:
    """Directories holding `.git` under root, down to `depth` levels. Does not
    descend into a repo once found. Filesystem only, no git."""
    try:
        root = root.resolve()
    except OSError:
        return
    if not root.is_dir():
        return
    stack = [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        if (d / ".git").exists():
            yield d
            continue
        if lvl >= depth:
            continue
        try:
            kids = sorted((e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)),
                          key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for e in kids:
            if not e.name.startswith(".") and e.name not in _SKIP_DIRS:
                stack.append((Path(e.path), lvl + 1))


# ---------------------------------------------------------------- git facts

@dataclass
class RepoState:
    repo: str
    findings: List[str] = field(default_factory=list)  # owed work
    notes: List[str] = field(default_factory=list)     # informational, never owed
    merged: List[Tuple[str, str, str]] = field(default_factory=list)  # (branch, count, default ref)
    unshared: List[Tuple[str, str]] = field(default_factory=list)  # (branch, count): no upstream, older than the owner
    error: Optional[str] = None

    @property
    def owed(self) -> bool:
        return bool(self.findings)


def _redact(text: str) -> str:
    kinds = safety.secret_kinds(text)
    return f"[redacted: {', '.join(kinds)}]" if kinds else text


def _default_ref(g: Any) -> Optional[str]:
    """The remote default branch as last fetched, or None. No network."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/remotes/upstream/HEAD", "refs/remotes/upstream/main", "refs/remotes/upstream/master"):
        try:
            g("rev-parse", "--verify", "--quiet", ref)
            return ref
        except GitError:
            continue
    return None


def _already_on(g: Any, default: Optional[str], branch: str) -> bool:
    """True only when every commit of `branch` missing from `default` has a
    patch-id twin on `default` (rebased / cherry-picked). A squash of several
    commits has no twin, so it stays unpushed. Any git error or timeout ->
    False (keep it as unpushed: never hide work on a failed check)."""
    if not default:
        return False
    try:
        out = g("rev-list", "--count", "--cherry-pick", "--right-only", "--no-merges",
                f"{default}...refs/heads/{branch}").strip()
    except GitError:
        return False
    return out == "0"


def inspect(repo: str, timeout: float = 5.0, since: Optional[float] = None) -> RepoState:
    """Owed work in one repo. `since` is the owning session's start: a branch
    with no upstream whose newest commit is older than that was not made by
    the session, so it goes to `unshared` (listed, not owed). Without `since`
    every unpushed branch is owed."""
    rs = RepoState(repo)
    try:
        neutral = neutralizers(Path(repo), timeout)
        g = lambda *a: safe_git(repo, *a, timeout=timeout, neutral=neutral)  # noqa: E731
        dirty = [ln for ln in g("status", "--porcelain=v1", "--ignore-submodules=all").splitlines() if ln.strip()]
        tracked = sum(1 for ln in dirty if not ln.startswith("??"))
        untracked = len(dirty) - tracked
        if tracked or untracked:
            parts = [f"{tracked} modified" if tracked else "", f"{untracked} untracked" if untracked else ""]
            rs.findings.append("uncommitted: " + ", ".join(p for p in parts if p))
        has_remote = bool(g("for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes").strip())
        if has_remote:
            ignore = ignore_branches()
            default = _default_ref(g)
            for line in g("for-each-ref",
                          "--format=%(refname:short)\t%(upstream)\t%(upstream:track)\t%(committerdate:unix)",
                          "refs/heads").splitlines():
                name, upstream, track, tip = (line.split("\t") + ["", "", ""])[:4]
                if not name or any(fnmatch.fnmatchcase(name, pat) for pat in ignore):
                    continue
                # Unpushed = on NO remote-tracking ref. Ahead-of-upstream alone overcounts
                # when the upstream ref is stale but the commits are on another remote.
                n = g("rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes").strip()
                if n and n != "0" and _already_on(g, default, name):
                    rs.merged.append((_redact(name), n, (default or "").replace("refs/remotes/", "")))
                elif n and n != "0" and not upstream and since is not None and tip.isdigit() \
                        and int(tip) < since:
                    rs.unshared.append((_redact(name), n))
                elif n and n != "0":
                    rs.findings.append(f"{n} unpushed commit(s) on {_redact(name)}"
                                       + ("" if upstream else " (no upstream)"))
                elif upstream and "ahead" in track:
                    ahead = track.split("ahead", 1)[1].split(",")[0].strip(" ]")
                    rs.notes.append(f"{_redact(name)}: {ahead} ahead of {upstream.replace('refs/remotes/', '')}, "
                                    "all on another remote (upstream ref stale; `git fetch` to refresh)")
        if g("for-each-ref", "--format=%(refname)", "refs/stash").strip():
            n = g("rev-list", "--walk-reflogs", "--count", "refs/stash").strip()
            rs.findings.append(f"{n} stash entr{'y' if n == '1' else 'ies'}")
    except GitError as e:
        rs.error = str(e)
    return rs


# ---------------------------------------------------------------- sessions

def recorded() -> Dict[str, Dict[str, float]]:
    """{session id: {repo: last recorded ts}} from every hook state file."""
    out: Dict[str, Dict[str, float]] = {}
    for sid, d in state.recent(10 ** 10):
        repos = d.get("repos") or {}
        if not isinstance(repos, dict):
            continue
        merged: Dict[str, float] = {}
        for r, t in repos.items():
            if isinstance(r, str) and isinstance(t, (int, float)):
                c = paths.canonical(r)  # spellings recorded before 1.2.1 can differ in case
                merged[c] = max(merged.get(c, 0.0), float(t))
        out[sid] = merged
    return out


def classify(info: Optional[Dict[str, Any]], stale_min: int, active_min: int = state.ACTIVE_MIN) -> str:
    """ACTIVE / DEAD (ended, or idle past stale_min) / IDLE (too recent to call)."""
    from .sessions import status
    st = status(info, active_min, True)
    if st == "ACTIVE":
        return "ACTIVE"
    if st == "ENDED":
        return "DEAD"
    if st == "IDLE":
        last = (info or {}).get("last_activity_at") or (info or {}).get("started_at") or 0
        return "DEAD" if time.time() - last >= stale_min * 60 else "IDLE"
    return "UNKNOWN"


@dataclass
class Plan:
    catch_up: List[Tuple[str, RepoState, str]] = field(default_factory=list)  # (sid, state, status)
    in_use: List[Tuple[str, str]] = field(default_factory=list)              # (repo, active sid)
    waiting: List[Tuple[str, str]] = field(default_factory=list)             # (repo, idle sid)
    unowned: List[RepoState] = field(default_factory=list)
    clean: int = 0
    errors: List[RepoState] = field(default_factory=list)
    resolved: List[str] = field(default_factory=list)                        # repos now clean (canonical)
    truncated: bool = False                                                  # hit the deadline
    noted: List[RepoState] = field(default_factory=list)                     # clean, with notes
    merged_repos: List[RepoState] = field(default_factory=list)              # any branch already on default
    unshared_repos: List[RepoState] = field(default_factory=list)               # older local-only branches

    def as_dict(self) -> Dict[str, Any]:
        return {"catch_up": [{"session": s, "repo": r.repo, "findings": r.findings, "status": st}
                             for s, r, st in self.catch_up],
                "in_use": [{"repo": r, "session": s} for r, s in self.in_use],
                "waiting": [{"repo": r, "session": s} for r, s in self.waiting],
                "unowned": [{"repo": r.repo, "findings": r.findings, "notes": r.notes,
                             "already_on_default": [{"branch": b, "commits": n, "default": d} for b, n, d in r.merged]}
                            for r in self.unowned],
                "already_on_default": [{"repo": r.repo, "branches": [{"branch": b, "commits": n, "default": d}
                                                                     for b, n, d in r.merged]}
                                       for r in self.merged_repos],
                "local_branches": [{"repo": r.repo, "branches": [{"branch": b, "commits": n} for b, n in r.unshared]}
                                   for r in self.unshared_repos],
                "notes": [{"repo": r.repo, "notes": r.notes} for r in self.noted],
                "clean": self.clean, "errors": [{"repo": r.repo, "error": r.error} for r in self.errors],
                "resolved": self.resolved, "truncated": self.truncated}


def build(stale_min: int = STALE_MIN, use_roots: bool = True, deadline: Optional[float] = None,
          be: Any = None) -> Plan:
    from . import backends
    be = backends.chain() if be is None else (be or None)
    rec = recorded()
    status_of: Dict[str, str] = {}
    info_of: Dict[str, Any] = {}
    for sid in rec:
        info = be.lookup(sid) if be else None
        info_of[sid] = info or {}
        status_of[sid] = classify(info, stale_min)
    # owner of each recorded repo: an ACTIVE recorder wins; else the latest recorder
    owner: Dict[str, Tuple[str, float]] = {}
    active: Dict[str, str] = {}
    for sid, repos in rec.items():
        for repo, ts in repos.items():
            if status_of[sid] == "ACTIVE":
                active[repo] = sid
            if repo not in owner or ts > owner[repo][1]:
                owner[repo] = (sid, ts)
    candidates = list(dict.fromkeys(list(owner) + ([paths.canonical(str(p)) for r, d in roots() for p in scan(r, d)]
                                                   if use_roots else [])))
    plan = Plan()
    for repo in candidates:
        if deadline is not None and time.monotonic() > deadline:
            plan.truncated = True
            break
        if repo in active:
            plan.in_use.append((repo, active[repo]))
            continue
        if not use_roots and status_of[owner[repo][0]] != "DEAD":
            plan.waiting.append((repo, owner[repo][0]))  # catch-up: don't spend git calls on live owners
            continue
        if not Path(repo).is_dir():
            continue
        started = info_of.get(owner[repo][0], {}).get("started_at") if repo in owner else None
        rs = inspect(repo, since=started if isinstance(started, (int, float)) else None)
        if rs.error:
            plan.errors.append(rs)
            continue
        if repo in owner:
            sid = owner[repo][0]
            if not rs.owed:
                plan.clean += 1
                plan.resolved.append(repo)
            elif status_of[sid] == "DEAD":
                plan.catch_up.append((sid, rs, why_dead(info_of.get(sid) or {})))
            else:
                plan.waiting.append((repo, sid))
        elif rs.owed:
            plan.unowned.append(rs)
        else:
            plan.clean += 1
        if rs.notes and not rs.owed:
            plan.noted.append(rs)
        if rs.merged:
            plan.merged_repos.append(rs)
        if rs.unshared:
            plan.unshared_repos.append(rs)
    return plan


def why_dead(info: Any) -> str:
    """`ended 10-01 22:48` or `idle since 10-01 22:48`: what the entry says
    about its session. Idle past the stale limit is not ended; the session may
    come back, and the entry must not claim otherwise."""
    ended = info.get("ended_at")
    if isinstance(ended, (int, float)) and ended:
        return f"ended {datetime.fromtimestamp(ended):%m-%d %H:%M}"
    last = info.get("last_activity_at") or info.get("started_at")
    if isinstance(last, (int, float)) and last:
        return f"idle since {datetime.fromtimestamp(last):%m-%d %H:%M}"
    return "idle"


# ---------------------------------------------------------------- tracker writes

def entry_id(sid: str, repo: str) -> str:
    return hashlib.sha1(f"audit|{sid}|{repo}".encode("utf-8", "replace")).hexdigest()[:8]


def _short(repo: str) -> str:
    home = str(Path.home())
    return "~" + repo[len(home):] if repo.startswith(home + os.sep) else repo


def render_entry(sid: str, rs: RepoState, stamp: str, why: str = "idle", eid: str = "") -> str:
    where = _redact(_short(rs.repo))
    head = f"**{stamp} [session {sid} #{eid or entry_id(sid, rs.repo)}] — {HEADLINE_TAG} {where}: owed work.**"
    body = [f"Found by `inflight audit` (session {why}); not verified by a person.",
            f"repo: {_redact(rs.repo)}"]
    body += [f"- [ ] {f}" for f in rs.findings]
    if rs.unshared:
        body.append("Older local branches, no upstream, last commit before this session started "
                    "(listed, not owed): " + ", ".join(f"{b} ({n})" for b, n in rs.unshared))
    body.append("Next: verify on disk, then push / commit / drop. Taking it over: append `(took over "
                f"{sid})` here.")
    return head + "\n" + "\n".join(body)


_REPO_LINE = re.compile(r"^repo: (.+)$", re.M)


def _repo_of(e: core.Entry) -> Optional[str]:
    """Canonical repo of an entry audit wrote, from its `repo:` line."""
    if HEADLINE_TAG not in e.head:
        return None
    m = _REPO_LINE.search(e.text)
    return paths.canonical(m.group(1).strip()) if m else None


def _findings_of(text: str) -> List[str]:
    return [ln.strip()[6:] for _, ln in progress.body_lines(text) if ln.strip().startswith("- [ ] ")]


def apply(plan: Plan, path: Path, today: Optional[date] = None) -> Dict[str, int]:
    """Upsert catch-up entries; mark resolved ones done. One locked write, or none."""
    today = today or date.today()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {"added": 0, "updated": 0, "done": 0, "unchanged": 0}
    if not path.exists():
        return counts
    with safety.locked(path):
        sections = core.parse(path.read_text(encoding="utf-8"))
        rn = core.right_now(sections)
        if rn is None:
            rn = core.Section(core.RIGHT_NOW)
            sections.insert(1 if sections and sections[0].header == "" else 0, rn)
        changed = False
        # audit's own entries, by (session, canonical repo): one repo spelled two
        # ways (case, symlink) is one repo, and keeps its existing entry id
        mine = [(e, _repo_of(e)) for e in rn.entries]
        mine = [(e, r) for e, r in mine if r]
        kept: set = set()
        for sid, rs, why in plan.catch_up:
            if not state.valid_sid(sid) or any(c in rs.repo for c in "\r\n"):
                continue
            cur = next((e for e, r in mine if r == rs.repo and e.session == sid), None)
            new = render_entry(sid, rs, stamp, why, eid=(cur.id or "") if cur else "")
            if safety.body_problems(new.split("\n", 1)[1]):
                continue  # never write text that could forge an entry
            if cur is None:
                cur = core.Entry(new)
                rn.entries.insert(0, cur)
                counts["added"] += 1
                changed = True
            elif _findings_of(cur.text) == rs.findings and progress.lifecycle(cur.text).state != "done":
                counts["unchanged"] += 1
            else:
                cur.text = new  # same id, fresh findings, back to active
                counts["updated"] += 1
                changed = True
            kept.add(id(cur))
        live = {rs.repo for _, rs, _ in plan.catch_up}
        resolved = set(plan.resolved)
        for e, repo in mine:
            # close: the repo is clean, or another entry now holds its owed work
            if id(e) in kept or (repo not in resolved and repo not in live):
                continue
            if progress.lifecycle(e.text).state != "done":
                e.text = progress.set_status(e.text, "done", today)
                counts["done"] += 1
                changed = True
        if changed:
            safety.write_private(path, core.render(sections))
    if changed:
        state.log("audit-apply", event="audit", kinds=[f"{k}={v}" for k, v in counts.items() if v])
    return counts


def _ended_since(ts: float) -> bool:
    """A session state file changed after `ts` and records an end after it.
    Stats only files touched since `ts`, so it stays cheap."""
    for _sid, d in state.recent(max(0.0, time.time() - ts) + 1):
        e = d.get("ended_at")
        if isinstance(e, (int, float)) and e > ts:
            return True
    return False


def catch_up(budget_s: float = 3.0, min_interval_s: float = 600.0) -> Optional[Dict[str, int]]:
    """The session-start / cron path: recorded repos only, --apply, bounded
    time, at most once per `min_interval_s` (stamp file). Never raises."""
    try:
        stamp = state.state_dir() / "last-catch-up"
        try:
            last = stamp.stat().st_mtime
            # Rate limit, unless a session ended since the last run: that end
            # is exactly what catch-up exists for (quit + reopen within the
            # interval must still see the dead session's owed work).
            if time.time() - last < min_interval_s and not _ended_since(last):
                return None
        except OSError:
            pass
        safety.ensure_private_dir(state.state_dir())
        safety.write_private(stamp, f"{time.time():.0f}\n")
        from . import backends
        # built-in backends only: this runs inside `inflight hook`, which never imports plugins.
        # A session only a plugin knows is UNKNOWN here, so it is never written.
        plan = build(use_roots=False, deadline=time.monotonic() + budget_s,
                     be=backends.chain(plugins=False) or False)
        if not plan.catch_up and not plan.resolved:  # nothing to write: skip the lock
            return {}
        return apply(plan, paths.inflight_file().expanduser())
    except Exception as e:  # catch-up must never break a hook or the cron
        state.log("audit-error", event="catch-up", error=type(e).__name__)
        return None


# ---------------------------------------------------------------- CLI

def _print(plan: Plan, apply_mode: bool) -> None:
    print(f"inflight audit ({'apply' if apply_mode else 'dry run: nothing written'})")
    print(f"\ncatch-up: {len(plan.catch_up)} (session ended or idle past the stale limit; "
          "one entry per repo)")
    for sid, rs, why in plan.catch_up:
        print(f"  [session {sid}, {why}] {_short(rs.repo)}")
        for f in rs.findings:
            print(f"      - {f}")
    if plan.waiting:
        print(f"\nnot yet (owner idle, under the stale limit): {len(plan.waiting)}")
        for repo, sid in plan.waiting:
            print(f"  {_short(repo)}  ({sid})")
    print(f"\nin use by an ACTIVE session (skipped): {len(plan.in_use)}")
    for repo, sid in plan.in_use:
        print(f"  {_short(repo)}  ({sid})")
    print(f"\nunowned (scan roots, no recorded session; listed only, never written): {len(plan.unowned)}")
    for rs in plan.unowned:
        print(f"  {_short(rs.repo)}: " + "; ".join(rs.findings))
        for n in rs.notes:
            print(f"      note: {n}")
    if plan.merged_repos:
        total = sum(len(rs.merged) for rs in plan.merged_repos)
        print(f"\nalready on the default branch (commits on no remote, every one patch-equivalent to the "
              f"default branch; not owed work): {total} branch(es)")
        for rs in plan.merged_repos:
            print(f"  {_short(rs.repo)}:")
            for b, c, d in rs.merged:
                print(f"      {b} ({c} commit(s), already on {d})")
    if plan.unshared_repos:
        total = sum(len(rs.unshared) for rs in plan.unshared_repos)
        print(f"\nolder local branches (no upstream, last commit before the owning session started; "
              f"not owed work): {total} branch(es)")
        for rs in plan.unshared_repos:
            print(f"  {_short(rs.repo)}: " + ", ".join(f"{b} ({n})" for b, n in rs.unshared))
    if plan.noted:
        print(f"\nnotes (not owed work): {len(plan.noted)}")
        for rs in plan.noted:
            for n in rs.notes:
                print(f"  {_short(rs.repo)}: {n}")
    if plan.errors:
        print(f"\nskipped on error: {len(plan.errors)}")
        for rs in plan.errors:
            print(f"  {_short(rs.repo)}: {rs.error}")
    print(f"\nclean: {plan.clean}")
    if plan.truncated:
        print("(stopped at the time limit; re-run for the rest)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inflight audit",
        description="Owed git work per session. Dry run unless --apply. Exit 0 always on a completed "
                    "audit (findings are not errors).")
    ap.add_argument("--apply", action="store_true", help="write catch-up entries / mark resolved ones done")
    ap.add_argument("--dry-run", action="store_true", help="the default; accepted for clarity (overrides --apply)")
    ap.add_argument("--catch-up", action="store_true",
                    help="recorded repos only (skip the scan roots): the session-start / cron path")
    ap.add_argument("--stale-min", type=int, default=paths.env_int("INFLIGHT_STALE_MIN", STALE_MIN),
                    help=f"an IDLE session counts as gone after this many minutes (default {STALE_MIN})")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.dry_run:
        args.apply = False
    plan = build(args.stale_min, use_roots=not args.catch_up)
    counts = None
    if args.apply:
        try:
            counts = apply(plan, (args.file or paths.inflight_file()).expanduser())
        except safety.LockTimeout as e:
            print(f"REFUSED: {e}; re-run", file=sys.stderr)
            return 3
    if args.json:
        print(json.dumps({**plan.as_dict(), "written": counts}, indent=1))
    else:
        _print(plan, args.apply)
        if counts is not None:
            print("\nwritten: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0
