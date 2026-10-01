"""Third-party backends via Python entry points, loaded only if allowlisted.

A package advertises a backend in its pyproject.toml:

    [project.entry-points."agent_inflight.backends"]
    mybackend = "my_pkg.inflight:Backend"

Nothing it ships is imported until the user runs
`inflight plugin enable mybackend`. The allowlist lives in
<tracker dir>/inflight-state/config.json (0600); every enable/disable goes to
hooks.log. Plugins load lazily: only commands that need session status
(`sessions`, `trim`, audit) call load_backends(); `add`, `done`, `check` and
the hook fast path never do.

The entry point must resolve to a class (instantiated with no arguments) or an
object with the backend contract in backends.py. Its module may set
PLUGIN_API_VERSION; a mismatch is refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from . import safety, state

GROUP = "agent_inflight.backends"


def _entry_points() -> list:
    from importlib import metadata
    eps = metadata.entry_points()
    if hasattr(eps, "select"):  # 3.10+
        return list(eps.select(group=GROUP))
    return list(eps.get(GROUP, []))  # 3.9: dict of group -> [EntryPoint]; entry_points(group=) is a TypeError


def config_path():
    return state.state_dir() / "config.json"


def _load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def allowlist() -> List[str]:
    v = _load_config().get("plugins", [])
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _save_allowlist(names: List[str]) -> None:
    cfg = _load_config()
    cfg["plugins"] = names
    safety.ensure_private_dir(state.state_dir())
    p = config_path()
    with safety.locked(p, timeout=2.0):
        safety.write_private(p, json.dumps(cfg, indent=1, sort_keys=True) + "\n")


def load_backends() -> List[Any]:
    """Instances of the allowlisted backends that are installed, in allowlist
    order. A plugin that fails to import or has the wrong API version is
    skipped and logged, never fatal."""
    allowed = allowlist()
    if not allowed:
        return []
    from . import backends
    eps = {ep.name: ep for ep in _entry_points()}
    out = []
    for name in allowed:
        ep = eps.get(name)
        if ep is None:
            continue
        try:
            obj = ep.load()
            mod = sys.modules.get(getattr(obj, "__module__", ""), None)
            ver = getattr(mod, "PLUGIN_API_VERSION", backends.PLUGIN_API_VERSION)
            if ver != backends.PLUGIN_API_VERSION:
                state.log("plugin-refused", plugin=name, error=f"api {ver}")
                continue
            inst = obj() if isinstance(obj, type) else obj
            if not callable(getattr(inst, "lookup", None)):
                state.log("plugin-refused", plugin=name, error="no lookup()")
                continue
            if not getattr(inst, "name", None):
                inst.name = name
            out.append(inst)
        except Exception as e:
            state.log("plugin-error", plugin=name, error=type(e).__name__)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="inflight plugin", description="allowlist for backend plugins")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="installed backend plugins and whether each is enabled")
    for c in ("enable", "disable"):
        sp = sub.add_parser(c)
        sp.add_argument("name")
    args = ap.parse_args(argv)
    allowed = allowlist()
    if args.cmd in (None, "list"):
        names = sorted({ep.name for ep in _entry_points()} | set(allowed))
        installed = {ep.name: ep.value for ep in _entry_points()}
        if not names:
            print(f"no backend plugins installed (entry-point group {GROUP})")
        for n in names:
            flag = "enabled " if n in allowed else "disabled"
            print(f"{flag}  {n:<20} {installed.get(n, '(not installed)')}")
        return 0
    if args.cmd == "enable":
        if args.name not in {ep.name for ep in _entry_points()}:
            print(f"no installed plugin named {args.name!r} (see `inflight plugin list`)", file=sys.stderr)
            return 1
        if args.name not in allowed:
            _save_allowlist(allowed + [args.name])
        state.log("plugin-enable", plugin=args.name)
        print(f"enabled {args.name}")
        return 0
    if args.name in allowed:
        _save_allowlist([n for n in allowed if n != args.name])
    state.log("plugin-disable", plugin=args.name)
    print(f"disabled {args.name}")
    return 0
