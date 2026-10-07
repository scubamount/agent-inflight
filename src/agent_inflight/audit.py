"""`inflight audit`: owed git work (dirty, unpushed, stashed) per session.

Two sources of repos, never mixed up:

  recorded   repos a session touched, from hook state
             (<state>/sessions/<id>.json `repos`; written by `inflight hook`
             and by the Hermes plugin after each tool call)
  roots      repos found under the configured scan roots
             (config.json `audit.roots`, default [{"path": "~/code", "depth": 3}])

How each finding is used:

  attribution  each finding goes to the session that made it (attribute()):
             a dirty file to the session that recorded writing it (Hermes
             write tools), else to the one write-blind session (Claude Code)
             whose active hours span its mtime; a commit or stash to the one
             recorder whose active hours span its time. No single answer =
             unattributed.
  catch-up   a session's attributed work in a recorded repo, once that
             session is ENDED or IDLE longer than --stale-min (default 120).
             One entry per (session, repo), tagged with that session, written
             under `## Right now` with --apply. Re-running updates the same
             entry in place; once the session owes nothing there the entry is
             marked done, never deleted. Taking the work over stays explicit
             (`(took over <id>)`); audit never writes that.
  in use     a repo some ACTIVE session recorded: skipped, no entry.
  unattributed  owed work in a recorded repo that no session provably made:
             listed only, never written.
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


def _globs(key: str) -> List[str]:
    """Opt-in glob patterns (config.json audit.<key>); empty by default.
    `inflight check` lists them so nothing is hidden silently."""
    from . import plugins
    cfg = plugins._load_config().get("audit", {})
    v = cfg.get(key, []) if isinstance(cfg, dict) else []
    return [p for p in v if isinstance(p, str) and p] if isinstance(v, list) else []


def ignore_branches() -> List[str]:
    return _globs("ignore_branches")


def ignore_repos() -> List[str]:
    return _globs("ignore_repos")


def repo_ignored(repo: str, patterns: List[str]) -> bool:
    """`repo` (canonical) matches a glob; `~` expands. Case-insensitive on
    macOS, where the filesystem is."""
    fold = (lambda s: s.lower()) if sys.platform == "darwin" else (lambda s: s)
    return any(fnmatch.fnmatchcase(fold(repo), fold(os.path.expanduser(p).rstrip("/"))) for p in patterns)


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
    """Facts about one repo. `findings` describes them; attribution splits
    them by session (see attribute())."""
    repo: str
    dirty: List[Tuple[str, str, Optional[float]]] = field(default_factory=list)  # (path, kind, mtime)
    unpushed: List[Tuple[str, str, Optional[float], bool]] = field(default_factory=list)  # (branch, n, tip, upstream)
    stashes: List[Optional[float]] = field(default_factory=list)                 # one commit time per entry
    notes: List[str] = field(default_factory=list)     # informational, never owed
    merged: List[Tuple[str, str, str]] = field(default_factory=list)  # (branch, count, default ref)
    error: Optional[str] = None

    @property
    def findings(self) -> List[str]:
        out: List[str] = []
        tracked = sum(1 for _, k, _ in self.dirty if k == "modified")
        untracked = len(self.dirty) - tracked
        if self.dirty:
            parts = [f"{tracked} modified" if tracked else "", f"{untracked} untracked" if untracked else ""]
            out.append("uncommitted: " + ", ".join(p for p in parts if p))
        for name, count, _, upstream in self.unpushed:
            out.append(f"{count} unpushed commit(s) on {name}" + ("" if upstream else " (no upstream)"))
        if self.stashes:
            k = len(self.stashes)
            out.append(f"{k} stash entr{'y' if k == 1 else 'ies'}")
        return out

    @property
    def owed(self) -> bool:
        return bool(self.dirty or self.unpushed or self.stashes)


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


SQUASH_SCAN_MAX = 500  # default-branch commits searched for a squash twin


def _raw_changes(raw: str) -> List[Tuple[str, ...]]:
    """(new mode, new blob, status, path) per line of `diff-tree -r` raw output.
    The old side is left out: the default branch may have moved the same file
    on before the squash landed."""
    out = []
    for line in raw.splitlines():
        if line.startswith(":") and "\t" in line:
            meta, path = line.split("\t", 1)
            f = meta.split()
            if len(f) >= 5:
                out.append((f[1], f[3], f[4], path))
    return sorted(out)


def _squash_merged(g: Any, default: Optional[str], branch: str) -> bool:
    """True when one commit on `default` since the merge base makes exactly
    the branch's net change (same paths, same resulting blobs): the branch
    was squash-merged and nothing was added to it after. Any git error,
    timeout, empty diff or a range over SQUASH_SCAN_MAX -> False (keep it
    as unpushed: never hide work on a failed check)."""
    if not default:
        return False
    try:
        base = g("merge-base", default, f"refs/heads/{branch}").strip()
        want = _raw_changes(g("diff-tree", "-r", "--no-renames", base, f"refs/heads/{branch}"))
        if not base or not want:
            return False
        n = g("rev-list", "--count", "--no-merges", f"{base}..{default}").strip()
        if not n.isdigit() or not 0 < int(n) <= SQUASH_SCAN_MAX:
            return False
        raw = g("log", "--no-merges", "--raw", "--no-renames", "--no-abbrev", "--no-ext-diff", "--no-textconv",
                "--format=%H", f"{base}..{default}")
    except GitError:
        return False
    blocks: List[List[str]] = []  # `log --raw` prints each commit id, then its changes
    for line in raw.splitlines():
        if not line.startswith(":"):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    return any(_raw_changes("\n".join(b)) == want for b in blocks)


def _status_paths(raw: str) -> List[Tuple[str, str]]:
    """(repo-relative path, "modified" | "untracked") from `status --porcelain=v1 -z`.
    A rename or copy is one entry; its source path (the next field) is skipped."""
    out: List[Tuple[str, str]] = []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        out.append((path, "untracked" if xy == "??" else "modified"))
        if "R" in xy or "C" in xy:
            i += 1
    return out


def _mtime(repo: str, rel: str) -> Optional[float]:
    """Last change time of a dirty path. An untracked directory's own mtime
    moves only when entries are added or removed, so it is the newest mtime
    of anything inside (bounded walk)."""
    p = os.path.join(repo, rel.rstrip("/"))
    try:
        newest = os.lstat(p).st_mtime
    except OSError:
        return None  # deleted: only a recorded write can attribute it
    if rel.endswith("/"):
        seen = 0
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and d != ".git"]
            for f in files:
                seen += 1
                if seen > 5000:
                    return None  # too big to time: a partial newest could be too early
                try:
                    newest = max(newest, os.lstat(os.path.join(root, f)).st_mtime)
                except OSError:
                    pass
    return newest


def inspect(repo: str, timeout: float = 5.0) -> RepoState:
    """Owed-work facts in one repo, each with the time attribute() needs:
    dirty paths with their mtime, unpushed branches with their tip's commit
    time, stash entries with theirs. Read-only git through safe_git, plus
    lstat."""
    rs = RepoState(repo)
    try:
        neutral = neutralizers(Path(repo), timeout)
        g = lambda *a: safe_git(repo, *a, timeout=timeout, neutral=neutral)  # noqa: E731
        for rel, kind in _status_paths(g("status", "--porcelain=v1", "-z", "--ignore-submodules=all")):
            rs.dirty.append((rel, kind, _mtime(repo, rel)))
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
                if n and n != "0" and (_already_on(g, default, name) or _squash_merged(g, default, name)):
                    rs.merged.append((_redact(name), n, (default or "").replace("refs/remotes/", "")))
                elif n and n != "0":
                    rs.unpushed.append((_redact(name), n, float(tip) if tip.isdigit() else None, bool(upstream)))
                elif upstream and "ahead" in track:
                    ahead = track.split("ahead", 1)[1].split(",")[0].strip(" ]")
                    rs.notes.append(f"{_redact(name)}: {ahead} ahead of {upstream.replace('refs/remotes/', '')}, "
                                    "all on another remote (upstream ref stale; `git fetch` to refresh)")
        if g("for-each-ref", "--format=%(refname)", "refs/stash").strip():
            out = g("rev-list", "--walk-reflogs", "--format=%ct", "refs/stash")
            rs.stashes = [float(ln) for ln in out.splitlines() if ln.strip().isdigit()]
    except GitError as e:
        rs.error = str(e)
    return rs


# ---------------------------------------------------------------- attribution

SLACK_S = 120.0  # a write lands a moment after the heartbeat that preceded it


@dataclass
class Sess:
    """What attribution knows about one session that recorded a repo."""
    sid: str
    start: Optional[float]
    end: Optional[float]               # ended_at, else last activity
    records_writes: bool               # its harness records the files it writes (Hermes)
    writes: Dict[str, float] = field(default_factory=dict)  # canonical file path -> ts

    def spans(self, t: Optional[float]) -> bool:
        return t is not None and self.start is not None and self.end is not None \
            and self.start <= t <= self.end + SLACK_S


def _writer(path: str, mtime: Optional[float], sessions: List[Sess]) -> Optional[str]:
    """Latest session that recorded writing `path` (a file, or a file under an
    untracked directory `path/`), when that write explains the current
    content: no write may be older than the file's last change by more than
    SLACK_S. A stale write (last month's) proves nothing about today's edit,
    and a path with no time (deleted, or a directory too big to walk) is
    never given to a recorded writer."""
    best: Optional[Tuple[float, str]] = None
    for s in sessions:
        for p, ts in s.writes.items():
            if p == path.rstrip("/") or (path.endswith("/") and p.startswith(path)):
                if best is None or ts > best[0]:
                    best = (ts, s.sid)
    # A deleted file has no mtime: its deletion time is unknown, so no
    # recorded write can be shown to explain it.
    if best is None or mtime is None or best[0] < mtime - SLACK_S:
        return None
    return best[1]


def _only(cands: List[Sess]) -> Optional[str]:
    return cands[0].sid if len(cands) == 1 else None


def attribute(rs: RepoState, sessions: List[Sess]) -> Tuple[Dict[str, RepoState], RepoState]:
    """Split one repo's owed work by the session that made it.

    dirty file    the latest session that recorded writing it, when that write
                  is no older than the file's last change (SLACK_S); else
                  the one session whose start..last activity spans the
                  file's mtime, if that session doesn't record writes
                  (Claude Code). A spanning Hermes session is a rival (it may
                  have edited through a shell), so it makes the file
                  unattributed rather than owned
    commit/stash  the one recorder whose start..last activity spans the
                  branch tip's / stash entry's time

    Anything else (no candidate, or several) is unattributed: listed, never
    owed by a session."""
    owned: Dict[str, RepoState] = {}
    rest = RepoState(rs.repo, notes=rs.notes, merged=rs.merged)

    def slot(sid: Optional[str]) -> RepoState:
        if sid is None:
            return rest
        return owned.setdefault(sid, RepoState(rs.repo))

    for rel, kind, mt in rs.dirty:
        sid = _writer(os.path.join(rs.repo, rel), mt, sessions)
        if sid is None:
            cands = [s for s in sessions if s.spans(mt)]
            sid = cands[0].sid if len(cands) == 1 and not cands[0].records_writes else None
        slot(sid).dirty.append((rel, kind, mt))
    for b in rs.unpushed:
        slot(_only([s for s in sessions if s.spans(b[2])])).unpushed.append(b)
    for t in rs.stashes:
        slot(_only([s for s in sessions if s.spans(t)])).stashes.append(t)
    return owned, rest


# ---------------------------------------------------------------- sessions

@dataclass
class Recorded:
    """One session's hook state, as audit reads it."""
    repos: Dict[str, float] = field(default_factory=dict)   # canonical repo -> last recorded ts
    writes: Dict[str, float] = field(default_factory=dict)  # file path -> ts (Hermes write tools)
    harness: str = ""


def recorded() -> Dict[str, Recorded]:
    """{session id: Recorded} from every hook state file."""
    out: Dict[str, Recorded] = {}
    for sid, d in state.recent(10 ** 10):
        repos = d.get("repos") or {}
        if not isinstance(repos, dict):
            continue
        rec = Recorded(harness=str(d.get("harness") or ""))
        for r, t in repos.items():
            if isinstance(r, str) and isinstance(t, (int, float)):
                c = paths.canonical(r)  # spellings recorded before 1.2.1 can differ in case
                rec.repos[c] = max(rec.repos.get(c, 0.0), float(t))
        writes = d.get("writes") or {}
        if isinstance(writes, dict):
            rec.writes = {p: float(t) for p, t in writes.items()
                          if isinstance(p, str) and isinstance(t, (int, float))}
        out[sid] = rec
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
    catch_up: List[Tuple[str, RepoState, str]] = field(default_factory=list)  # (sid, its share, status)
    in_use: List[Tuple[str, str]] = field(default_factory=list)              # (repo, active sid)
    waiting: List[Tuple[str, str]] = field(default_factory=list)             # (repo, idle sid owing work)
    pending: List[Tuple[str, str]] = field(default_factory=list)             # (repo, latest idle recorder), not inspected
    unowned: List[RepoState] = field(default_factory=list)                   # roots repo, no recorder
    unattributed: List[RepoState] = field(default_factory=list)              # recorded repo, no provable maker
    clean: int = 0
    errors: List[RepoState] = field(default_factory=list)
    owing: Dict[str, List[str]] = field(default_factory=dict)                # repo, fully attributed -> sids owing
    truncated: bool = False                                                  # hit the deadline
    noted: List[RepoState] = field(default_factory=list)                     # clean, with notes
    merged_repos: List[RepoState] = field(default_factory=list)              # any branch already on default
    ignored: List[str] = field(default_factory=list)                         # repos matching audit.ignore_repos
    checked: Dict[str, Tuple[Dict[str, RepoState], RepoState]] = field(default_factory=dict)  # repo -> attribute()

    def as_dict(self) -> Dict[str, Any]:
        def facts(r: RepoState) -> Dict[str, Any]:
            return {"repo": r.repo, "findings": r.findings}
        return {"catch_up": [{"session": s, "repo": r.repo, "findings": r.findings, "status": st}
                             for s, r, st in self.catch_up],
                "in_use": [{"repo": r, "session": s} for r, s in self.in_use],
                "waiting": [{"repo": r, "session": s} for r, s in self.waiting],
                "pending": [{"repo": r, "session": s} for r, s in self.pending],
                "ignored": self.ignored,
                "unowned": [{**facts(r), "notes": r.notes,
                             "already_on_default": [{"branch": b, "commits": n, "default": d} for b, n, d in r.merged]}
                            for r in self.unowned],
                "unattributed": [facts(r) for r in self.unattributed],
                "already_on_default": [{"repo": r.repo, "branches": [{"branch": b, "commits": n, "default": d}
                                                                     for b, n, d in r.merged]}
                                       for r in self.merged_repos],
                "notes": [{"repo": r.repo, "notes": r.notes} for r in self.noted],
                "clean": self.clean, "errors": [{"repo": r.repo, "error": r.error} for r in self.errors],
                "resolved": sorted(r for r, sids in self.owing.items() if not sids),
                "truncated": self.truncated}


def _sess(sid: str, info: Dict[str, Any], rec: Recorded) -> Sess:
    def num(k: str) -> Optional[float]:
        v = info.get(k)
        return float(v) if isinstance(v, (int, float)) and v else None
    end = num("ended_at") or num("last_activity_at")
    return Sess(sid, num("started_at"), end, rec.harness == "hermes", rec.writes)


def build(stale_min: int = STALE_MIN, use_roots: bool = True, deadline: Optional[float] = None,
          be: Any = None, tracked: Optional[set] = None) -> Plan:
    """`tracked`: repos with an open audit entry. They are re-inspected even
    while a recorder is live, so an entry never goes on claiming work that
    has since been pushed or committed; for them audit only updates or
    closes the existing entry, never adds one (plan.checked, apply())."""
    tracked = tracked or set()
    from . import backends
    be = backends.chain() if be is None else (be or None)
    rec = recorded()
    status_of: Dict[str, str] = {}
    info_of: Dict[str, Any] = {}
    for sid in rec:
        info = be.lookup(sid) if be else None
        info_of[sid] = info or {}
        status_of[sid] = classify(info, stale_min)
    recorders: Dict[str, List[str]] = {}   # repo -> sessions that recorded it, latest first
    for sid, r in rec.items():
        for repo in r.repos:
            recorders.setdefault(repo, []).append(sid)
    for repo, sids in recorders.items():
        sids.sort(key=lambda s: rec[s].repos[repo], reverse=True)
    candidates = list(dict.fromkeys(sorted(tracked) + list(recorders) + ([paths.canonical(str(p)) for r, d in roots()
                                                         for p in scan(r, d)] if use_roots else [])))
    plan = Plan()
    ignored = ignore_repos()
    for repo in candidates:
        if deadline is not None and time.monotonic() > deadline:
            plan.truncated = True
            break
        if repo_ignored(repo, ignored):
            plan.ignored.append(repo)
            plan.owing[repo] = []  # owes nothing: open entries for it close
            continue
        sids = recorders.get(repo, [])
        live = [s for s in sids if status_of[s] == "ACTIVE"]
        recheck_only = False
        if live or (not use_roots and sids and not any(status_of[s] == "DEAD" for s in sids)):
            if repo not in tracked:
                if live:
                    plan.in_use.append((repo, live[0]))
                else:
                    plan.pending.append((repo, sids[0]))  # catch-up: don't spend git calls on live recorders
                continue
            recheck_only = True  # an open entry here: verify it, never add one
        if not Path(repo).is_dir():
            continue
        rs = inspect(repo)
        if rs.error:
            plan.errors.append(rs)
            continue
        if rs.notes and not rs.owed:
            plan.noted.append(rs)
        if rs.merged:
            plan.merged_repos.append(rs)
        if not sids:
            if rs.owed:
                plan.unowned.append(rs)
            else:
                plan.clean += 1
            continue
        owned, rest = attribute(rs, [_sess(s, info_of[s], rec[s]) for s in sids])
        plan.checked[repo] = (owned, rest)
        if not rs.owed:
            plan.clean += 1
        if rest.owed:
            # Unattributed work keeps every open entry for this repo open: it
            # may be that entry's work, and closing would lose it.
            plan.unattributed.append(rest)
        else:
            plan.owing[repo] = []
        for sid, share in owned.items():
            if repo in plan.owing:
                plan.owing[repo].append(sid)
            if recheck_only:
                continue
            if status_of[sid] == "DEAD":
                plan.catch_up.append((sid, share, why_dead(info_of.get(sid) or {})))
            else:
                plan.waiting.append((repo, sid))
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
    body = [f"Found by `inflight audit` (session {why}): work this session made, by its recorded "
            "writes or its active hours; not verified by a person.",
            f"repo: {_redact(rs.repo)}"]
    body += [f"- [ ] {f}" for f in rs.findings]
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


def _with_findings(old: str, findings: List[str]) -> str:
    """`old` with its `- [ ]` finding lines replaced by `findings`, in place.
    Everything else (head and its date, provenance line, notes, `waiting on:`,
    `(took over ...)`, status) is kept as written."""
    lines = old.split("\n")
    idx = [i for i, ln in progress.body_lines(old) if ln.strip().startswith("- [ ] ")]
    if not idx:
        return old
    new = [f"- [ ] {f}" for f in findings]
    return "\n".join(lines[:idx[0]] + new + [ln for i, ln in enumerate(lines) if i > idx[0] and i not in idx])


def tracked_repos(path: Path) -> set:
    """Repos with an open audit entry: what build() re-checks even when live."""
    try:
        rn = core.right_now(core.parse(path.read_text(encoding="utf-8")))
    except OSError:
        return set()
    out = set()
    for e in (rn.entries if rn else []):
        r = _repo_of(e)
        if r and progress.lifecycle(e.text).state != "done":
            out.add(r)
    return out


UNPROVEN = " (still in the repo; maker not provable)"
_BRANCH_RE = re.compile(r"^\d+ unpushed commit\(s\) on (\S+)")


def still_owed(old: List[str], share: Optional[RepoState], rest: RepoState) -> List[str]:
    """What an open audit entry's findings are now. Its session's own share,
    plus each old finding that is still in the repo but no longer provably
    its session's (kept, marked UNPROVEN, so nothing is lost). Gone from the
    repo = dropped; empty = the entry is resolved."""
    now = list(share.findings) if share else []
    def has(prefix: str) -> bool:
        return any(f.startswith(prefix) for f in now)
    for f in old:
        base = f[:-len(UNPROVEN)] if f.endswith(UNPROVEN) else f
        if base in now:
            continue
        if base.startswith("uncommitted:") and rest.dirty and not has("uncommitted:"):
            now.append(RepoState(rest.repo, dirty=rest.dirty).findings[0] + UNPROVEN)
        elif base.endswith(("stash entry", "stash entries")) and rest.stashes \
                and not any(x.endswith(("stash entry", "stash entries")) for x in now):
            now.append(RepoState(rest.repo, stashes=rest.stashes).findings[0] + UNPROVEN)
        else:
            m = _BRANCH_RE.match(base)
            branch = m.group(1) if m else None
            for b in rest.unpushed:
                if b[0] == branch:
                    now.append(RepoState(rest.repo, unpushed=[b]).findings[0] + UNPROVEN)
    return list(dict.fromkeys(now))


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
        for e, repo in mine:
            # verify every other open audit entry against the repo as it is now
            if id(e) in kept or repo not in plan.checked or progress.lifecycle(e.text).state == "done":
                continue
            owned, rest = plan.checked[repo]
            now = still_owed(_findings_of(e.text), owned.get(e.session or ""), rest)
            kept.add(id(e))
            if not now:
                e.text = progress.set_status(e.text, "done", today)
                counts["done"] += 1
                changed = True
            elif now != _findings_of(e.text):
                new = _with_findings(e.text, now)
                if not safety.body_problems(new.split("\n", 1)[1]):
                    e.text = new
                    counts["updated"] += 1
                    changed = True
        for e, repo in mine:
            # close: the repo was inspected and this entry's session owes
            # nothing there now (clean, or the work is another session's)
            if id(e) in kept or repo not in plan.owing or e.session in plan.owing[repo]:
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


def _closable(plan: Plan, path: Path) -> bool:
    """An open audit entry whose session owes nothing in its repo now: what
    apply() would close. Read without the lock; apply() re-reads under it."""
    if not plan.owing:
        return False
    try:
        rn = core.right_now(core.parse(path.read_text(encoding="utf-8")))
    except OSError:
        return False
    for e in (rn.entries if rn else []):
        repo = _repo_of(e)
        if repo in plan.owing and e.session not in plan.owing[repo] \
                and progress.lifecycle(e.text).state != "done":
            return True
    return False


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
        path = paths.inflight_file().expanduser()
        tracked = tracked_repos(path)
        plan = build(use_roots=False, deadline=time.monotonic() + budget_s,
                     be=backends.chain(plugins=False) or False, tracked=tracked)
        if not plan.catch_up and not (plan.checked.keys() & tracked) and not _closable(plan, path):
            return {}  # nothing to write: skip the lock
        return apply(plan, path)
    except Exception as e:  # catch-up must never break a hook or the cron
        state.log("audit-error", event="catch-up", error=type(e).__name__)
        return None


# ---------------------------------------------------------------- CLI

def _print(plan: Plan, apply_mode: bool) -> None:
    print(f"inflight audit ({'apply' if apply_mode else 'dry run: nothing written'})")
    print(f"\ncatch-up: {len(plan.catch_up)} (session ended or idle past the stale limit; "
          "one entry per session and repo, its own work only)")
    for sid, rs, why in plan.catch_up:
        print(f"  [session {sid}, {why}] {_short(rs.repo)}")
        for f in rs.findings:
            print(f"      - {f}")
    if plan.waiting:
        print(f"\nnot yet (owner idle, under the stale limit): {len(plan.waiting)}")
        for repo, sid in plan.waiting:
            print(f"  {_short(repo)}  ({sid})")
    if plan.pending:
        print(f"\nnot inspected (every recorder idle under the stale limit): {len(plan.pending)}")
        for repo, sid in plan.pending:
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
    if plan.unattributed:
        print(f"\nunattributed (recorded repo, but no session provably made it; listed only, never "
              f"written): {len(plan.unattributed)}")
        for rs in plan.unattributed:
            print(f"  {_short(rs.repo)}: " + "; ".join(rs.findings))
    if plan.noted:
        print(f"\nnotes (not owed work): {len(plan.noted)}")
        for rs in plan.noted:
            for n in rs.notes:
                print(f"  {_short(rs.repo)}: {n}")
    if plan.ignored:
        print(f"\nignored (audit.ignore_repos, never inspected): {len(plan.ignored)}")
        for repo in plan.ignored:
            print(f"  {_short(repo)}")
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
    path = (args.file or paths.inflight_file()).expanduser()
    plan = build(args.stale_min, use_roots=not args.catch_up, tracked=tracked_repos(path))
    counts = None
    if args.apply:
        try:
            counts = apply(plan, path)
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
