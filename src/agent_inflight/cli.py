"""`inflight <command>` dispatcher. Stdlib only; Python >= 3.9."""
from __future__ import annotations

import sys
from typing import List, Optional

from . import __version__, paths

USAGE = f"""inflight {__version__} — shared work tracker for agent sessions

  inflight init                 create the file if missing
  inflight add "<head>" [body]  prepend a dated, session-tagged entry
  inflight sessions [--json] [--children]
                                who owns each entry; ACTIVE / IDLE / ENDED; progress
  inflight me                   print this session's tag
  inflight done <match> [--reopen] [--dry-run]
                                mark an entry done (trim archives it after a day)
  inflight trim [--apply]       pause stale entries; archive done + over-budget ones
  inflight check                lint (exit 1 on findings)
  inflight path                 print the resolved file path
  inflight audit [--apply] [--catch-up] [--json]
                                owed git work per session (dry run by default)
  inflight hook <event>         hook protocol v1: JSON on stdin, always exit 0
  inflight plugin list|enable|disable <name>
                                allowlist for backend plugins (entry points)

file: {{path}}
"""


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    if cmd == "hook":  # fast path: imports only what the hook needs
        from . import hook
        return hook.run(argv)
    if cmd == "plugin":
        from . import plugins
        return plugins.main(argv)
    if cmd == "audit":
        from . import audit
        return audit.main(argv)
    from . import entries, sessions, trim
    table = {
        "init": entries.init_main,
        "add": entries.add_main,
        "done": entries.done_main,
        "check": entries.check_main,
        "trim": trim.main,
        "sessions": sessions.main,
    }
    if cmd in table:
        return table[cmd](argv)
    if cmd == "me":
        return sessions.me_main()
    if cmd == "path":
        print(paths.inflight_file())
        return 0
    if cmd in ("--version", "version"):
        print(__version__)
        return 0
    print(USAGE.replace("{path}", str(paths.inflight_file())))
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    raise SystemExit(main())
