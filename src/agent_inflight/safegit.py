"""Read-only git against repos this tool did not create.

Running git in an arbitrary repo runs that repo's config. Measured on git
2.54 with repo-local config:

  core.fsmonitor = <cmd>        runs during plain `git status`
  filter.<x>.clean/process      runs during `git status` for any modified file
                                whose attributes name the filter
                                (.gitattributes, .git/info/attributes, or a
                                config reached through include.path)

so `safe_git()` always passes, on the command line (which beats every config
file):

  -c core.fsmonitor=false  -c core.hooksPath=/dev/null
  -c filter.<x>.{clean,smudge,process}=   for every filter driver defined in
                                local/worktree scope (incl. include.path), plus
                                -c filter.<x>.required=false
  -c protocol.allow=never  -c credential.helper=  -c core.pager=cat

and env GIT_OPTIONAL_LOCKS=0 (no index refresh write), GIT_TERMINAL_PROMPT=0,
GIT_CEILING_DIRECTORIES=<parent> (a broken .git never climbs into a parent
repo), GIT_* from the caller scrubbed. Filters defined in global/system config
(git-lfs) are the user's own and stay, so LFS status stays correct.

`status` always runs with --ignore-submodules=all: git status recurses into
each submodule and runs git there with the submodule's own config, which
these -c pairs don't cover (measured: a filter in the submodule's config ran
through 0.4.0's safe_git). Submodules are audited as their own repos.

Only the subcommands in READ_ONLY run, each with a 5 s timeout, and `cwd`
must be an existing directory that contains `.git`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TIMEOUT_S = 5.0
READ_ONLY = frozenset({"status", "rev-parse", "for-each-ref", "rev-list"})
_FILTER_KEY = r"^filter\..*\.(clean|smudge|process|required)$"
_LOCAL_SCOPES = ("local", "worktree", "command")


class GitError(Exception):
    pass


def _env(repo: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat",
               GIT_CEILING_DIRECTORIES=str(repo.parent), LC_ALL="C")
    return env


def _check_repo(cwd: "str | os.PathLike[str]") -> Path:
    p = Path(cwd)
    if not p.is_dir() or not (p / ".git").exists():
        raise GitError("not a repo directory")
    return p


def _run(repo: Path, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(repo), env=_env(repo), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise GitError(type(e).__name__) from e


BASE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
        "-c", "credential.helper=", "-c", "core.pager=cat")


def neutralizers(repo: Path, timeout: float = TIMEOUT_S) -> List[str]:
    """`-c` pairs that switch off every filter driver the repo itself defines.
    `git config` reads config (and include.path files) without executing any."""
    p = _run(repo, [*BASE, "config", "--show-scope", "--null", "--get-regexp", _FILTER_KEY], timeout)
    keys: List[str] = []
    if p.returncode == 0:
        # --null: "<scope>\0<key>\n<value>\0" per record
        parts = p.stdout.decode("utf-8", "replace").split("\0")
        for scope, rec in zip(parts[0::2], parts[1::2]):
            key = rec.split("\n", 1)[0].strip()
            if key and scope in _LOCAL_SCOPES:
                keys.append(key)
    elif p.returncode != 1:  # 1 = no match; anything else: old git without --show-scope
        p = _run(repo, [*BASE, "config", "--name-only", "--get-regexp", _FILTER_KEY], timeout)
        keys = p.stdout.decode("utf-8", "replace").split() if p.returncode == 0 else []
    out: List[str] = []
    for k in dict.fromkeys(keys):
        out += ["-c", f"{k}=false" if k.endswith(".required") else f"{k}="]
    return out


def safe_git(cwd: "str | os.PathLike[str]", *args: str, timeout: float = TIMEOUT_S,
             neutral: Optional[List[str]] = None) -> str:
    """stdout of one read-only git command, or GitError. Pass `neutral` (from
    neutralizers()) to reuse it across several calls on one repo."""
    if not args or args[0] not in READ_ONLY:
        raise GitError(f"refused: {args[0] if args else '(none)'} is not a read-only subcommand")
    repo = _check_repo(cwd)
    neutral = neutralizers(repo, timeout) if neutral is None else neutral
    if args[0] == "status":
        # status recurses into submodules and runs git there with the
        # SUBMODULE's config: our -c pairs and neutralizers() cover only this
        # repo, so a submodule's own filter would run. Never recurse; a
        # submodule is audited as its own repo when found or recorded.
        args = ("status", "--ignore-submodules=all", *[a for a in args[1:] if not a.startswith("--ignore-submodules")])
    p = _run(repo, [*BASE, *neutral, *args], timeout)
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(msg[-1][:160] if msg else f"exit {p.returncode}")
    return p.stdout.decode("utf-8", "replace")
