"""`inflight adapter install|uninstall claude-code`: user-level Claude Code hooks.

Only `~/.claude/settings.json` (or --settings PATH) is touched; project
`.claude/settings.json` and every managed source are out of scope and never
written. Dry run by default: prints the exact diff. With --apply:

  1. refuses when a managed source blocks user hooks (allowManagedHooksOnly,
     strictPluginOnlyCustomization covering hooks, managed disableAllHooks)
     and says what to ask IT for. Never edits managed settings.
  2. writes a timestamped backup `settings.json.inflight-bak-<stamp>` (0600)
  3. merges: existing hook groups are never changed, reordered or removed;
     inflight adds its own matcher groups at the end of each event's list.
  4. writes atomically, keeping the file's mode.

A handler is inflight's when its command is `"<launcher>" hook <event>
--harness claude-code`. That marker is the only thing `uninstall` removes, so
unrelated hooks come back byte-for-byte (for files in the standard 2-space
JSON layout; other layouts are refused unless --allow-reformat). The settings
file is never deleted: uninstalling from a file inflight created leaves `{}`.

Hooks (all `type: command`, command = the pinned launcher, absolute path):
  SessionStart  startup|resume|clear|compact  session-start   sync, 10 s
  PreToolUse    ^(Edit|Write|NotebookEdit|Bash)$  pre-tool    sync, 5 s (collision warning)
  PostToolUse   same matcher                  post-tool       async
  CwdChanged    (no matcher support)          cwd-changed     async
  SessionEnd    (all reasons)                 session-end     sync, default 1.5 s budget
The hook command always exits 0 and never emits a permission decision, so it
can't block a tool. Source: code.claude.com/docs/en/hooks.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths, state

HARNESS = "claude-code"
TOOL_MATCHER = "^(Edit|Write|NotebookEdit|Bash)$"
# (event, matcher or None, inflight event, extra handler fields)
HOOKS: List[Tuple[str, Optional[str], str, Dict[str, Any]]] = [
    ("SessionStart", "startup|resume|clear|compact", "session-start", {"timeout": 10}),
    ("PreToolUse", TOOL_MATCHER, "pre-tool", {"timeout": 5}),
    ("PostToolUse", TOOL_MATCHER, "post-tool", {"async": True}),
    ("CwdChanged", None, "cwd-changed", {"async": True}),
    ("SessionEnd", None, "session-end", {}),
]
_MARK_RE = re.compile(r"\bhook [a-z-]+ --harness " + re.escape(HARNESS) + r"$")


# ---------------------------------------------------------------- locations

def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"


def launcher() -> str:
    """The pinned launcher install.sh wrote, else this checkout's bin/inflight."""
    p = state.state_dir() / "inflight"
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    return str(Path(__file__).resolve().parents[2] / "bin" / "inflight")


def managed_sources() -> List[Path]:
    env = os.environ.get("INFLIGHT_CLAUDE_MANAGED_DIRS")
    if env is not None:
        dirs = [Path(d) for d in env.split(os.pathsep) if d]
    elif platform.system() == "Darwin":
        dirs = [Path("/Library/Application Support/ClaudeCode")]
    else:
        dirs = [Path("/etc/claude-code")]
    out: List[Path] = []
    for d in dirs:
        out.append(d / "managed-settings.json")
        try:
            out += sorted(p for p in (d / "managed-settings.d").glob("*.json") if not p.name.startswith("."))
        except OSError:
            pass
    if env is None:
        # server-managed settings cache (path observed locally, not in the docs) and the
        # macOS managed-preferences domain com.anthropic.claudecode (documented)
        out.append(Path.home() / ".claude" / "remote-settings.json")
        if platform.system() == "Darwin":
            out += [Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
                    Path(f"/Library/Managed Preferences/{os.environ.get('USER', '')}/com.anthropic.claudecode.plist")]
    return out


def _load_managed(p: Path) -> Optional[Dict[str, Any]]:
    try:
        if p.suffix == ".plist":
            import plistlib
            with open(p, "rb") as fh:
                d = plistlib.load(fh)
        else:
            d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError, Exception):
        return None


def lockdown() -> List[str]:
    """Why user-level hooks would not run, per managed source. Empty = none."""
    found: List[str] = []
    for p in managed_sources():
        d = _load_managed(p)
        if not d:
            continue
        if d.get("allowManagedHooksOnly") is True:
            found.append(f"allowManagedHooksOnly in {p}")
        spoc = d.get("strictPluginOnlyCustomization")
        if spoc is True or (isinstance(spoc, list) and "hooks" in spoc):
            found.append(f"strictPluginOnlyCustomization (hooks) in {p}")
        if d.get("disableAllHooks") is True:
            found.append(f"disableAllHooks in {p}")
    return found


# ---------------------------------------------------------------- merge

def command_for(event: str, launch: str) -> str:
    return f"{shlex.quote(launch)} hook {event} --harness {HARNESS}"


def is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and isinstance(handler.get("command"), str) \
        and bool(_MARK_RE.search(handler["command"]))


def desired(launch: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for cc_event, matcher, ev, extra in HOOKS:
        group: Dict[str, Any] = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [{"type": "command", "command": command_for(ev, launch), **extra}]
        out[cc_event] = group
    return out


def strip(settings: Dict[str, Any]) -> Dict[str, Any]:
    """`settings` without any inflight handler. Groups left empty by that are
    dropped, then event lists left empty, then an empty `hooks` object, so a
    file inflight only added to returns to its exact earlier shape."""
    s = json.loads(json.dumps(settings))
    hooks = s.get("hooks")
    if not isinstance(hooks, dict):
        return s
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                had = len(g["hooks"])
                g["hooks"] = [h for h in g["hooks"] if not is_ours(h)]
                if had and not g["hooks"]:
                    continue
            kept.append(g)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        del s["hooks"]
    return s


def merged(settings: Dict[str, Any], launch: str) -> Dict[str, Any]:
    s = strip(settings)
    hooks = s.setdefault("hooks", {})
    for ev, group in desired(launch).items():
        hooks.setdefault(ev, []).append(group)
    return s


def dump(d: Dict[str, Any]) -> str:
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- CLI

def _read(path: Path) -> Tuple[str, Dict[str, Any]]:
    if not path.exists():
        return "", {}
    raw = path.read_text(encoding="utf-8")
    d = json.loads(raw) if raw.strip() else {}
    if not isinstance(d, dict):
        raise ValueError("settings.json is not a JSON object")
    return raw, d


def _write(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(action: str, path: Path, apply: bool, allow_reformat: bool = False) -> int:
    try:
        raw, cur = _read(path)
    except (OSError, ValueError) as e:
        print(f"REFUSED: can't read {path}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    new = merged(cur, launcher()) if action == "install" else strip(cur)
    if action == "uninstall" and not path.exists():
        print(f"nothing to do: {path} does not exist")
        return 0
    new_text = dump(new)
    if raw and dump(cur) != raw and not allow_reformat:
        print(f"REFUSED: {path} is not in the standard 2-space JSON layout, so rewriting it would "
              "reformat lines inflight did not add. Re-run with --allow-reformat to accept that "
              "(a backup is still written).", file=sys.stderr)
        return 2
    if new_text == raw or (not raw and not new):
        print(f"no change: {path} already {'has' if action == 'install' else 'lacks'} the inflight hooks")
        return 0
    blocked = lockdown() if action == "install" else []
    diff = "".join(difflib.unified_diff(raw.splitlines(True), new_text.splitlines(True),
                                        fromfile=str(path), tofile=f"{path} (after {action})"))
    print(diff, end="" if diff.endswith("\n") else "\n")
    if blocked:
        print("\nREFUSED: managed settings block user-level hooks, so these would never run:", file=sys.stderr)
        for b in blocked:
            print(f"  - {b}", file=sys.stderr)
        print("Ask IT to deploy the hooks above through managed settings (or allow user hooks). "
              "inflight never edits managed settings. Until then use `inflight add` by hand.",
              file=sys.stderr)
        return 3
    if not apply:
        print(f"\ndry run: nothing written. Re-run with --apply to {action}.")
        return 0
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    if path.exists():
        stamp = time.strftime('%Y%m%d_%H%M%S')
        for n in range(100):  # O_EXCL: never overwrite an earlier backup
            bak = path.with_name(f"{path.name}.inflight-bak-{stamp}" + (f"-{n}" if n else ""))
            try:
                fd = os.open(str(bak), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                continue
        else:
            print("REFUSED: could not create a unique backup file", file=sys.stderr)
            return 2
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
        print(f"backup: {bak}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, new_text or "{}\n", mode)  # never delete the user's settings file
    state.log(f"adapter-{action}", event=HARNESS)
    print(f"{action}ed: {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight adapter",
                                 description="Install/uninstall harness hooks. Dry run unless --apply.")
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    ap.add_argument("harness", choices=(HARNESS,))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--settings", type=Path, default=None, help="settings file (default ~/.claude/settings.json)")
    ap.add_argument("--allow-reformat", action="store_true")
    args = ap.parse_args(argv)
    path = (args.settings or settings_path()).expanduser()
    if args.action == "status":
        try:
            _, cur = _read(path)
        except (OSError, ValueError) as e:
            print(f"can't read {path}: {e}")
            return 2
        n = sum(1 for g in (cur.get("hooks") or {}).values() if isinstance(g, list)
                for grp in g if isinstance(grp, dict) for h in grp.get("hooks", []) if is_ours(h))
        print(f"{path}: {n} inflight handler(s) installed (of {len(HOOKS)})")
        for b in lockdown():
            print(f"  blocked: {b}")
        return 0
    return run(args.action, path, args.apply, args.allow_reformat)
