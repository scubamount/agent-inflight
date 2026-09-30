"""`inflight <command>` dispatcher. Stdlib only; Python >= 3.9."""
from __future__ import annotations

import sys
from typing import List, Optional

from . import __version__, entries, paths, sessions, trim

USAGE = f"""inflight {__version__} — shared work tracker for agent sessions

  inflight init                 create the file if missing
  inflight add "<head>" [body]  prepend a dated, session-tagged entry
  inflight sessions [--json]    who owns each entry; ACTIVE / IDLE / ENDED
  inflight me                   print this session's tag
  inflight trim [--apply]       archive old entries; keep within budget
  inflight check                lint (exit 1 on findings)
  inflight path                 print the resolved file path

file: {{path}}
"""


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    table = {
        "init": entries.init_main,
        "add": entries.add_main,
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
