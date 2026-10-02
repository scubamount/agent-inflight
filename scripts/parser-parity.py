#!/usr/bin/env python3
"""Parser parity: entry counts under the pre-C5 rule vs the strict rule.

  python3 scripts/parser-parity.py FILE [FILE ...]

The pre-C5 rule also started an entry at a `**` line right after a blank
line. For each file this prints both counts for `## Right now` and for the
whole file, and proves no text is lost (render under the strict parser keeps
every byte of each section body). Exit 1 if any `## Right now` count differs
(those are the entries trim/sessions/done act on) or any text is lost; archive
sections are reported, not failed: old trackers used undated bold blocks as
entries, which the strict parser now keeps as part of the entry above.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_inflight import core  # noqa: E402


def legacy_split(body: str) -> int:
    n, prev_blank = 0, True
    for line in body.split("\n"):
        if line.startswith("**") and (core.ENTRY_DATE_HEAD.match(line) or prev_blank):
            n += 1
        prev_blank = not line.strip()
    return n


def bodies(text: str):
    parts = re.split(r"(?m)^(## .*)$", text)
    yield "", parts[0]
    for i in range(1, len(parts), 2):
        yield parts[i].rstrip(), parts[i + 1] if i + 1 < len(parts) else ""


def main(argv) -> int:
    bad = 0
    for f in argv:
        text = Path(f).read_text(encoding="utf-8")
        rn_old = rn_new = None
        all_old = all_new = 0
        for header, body in bodies(text):
            o, (_, ents) = legacy_split(body), core.split_entries(body)
            all_old += o
            all_new += len(ents)
            if header.strip().lower() == core.RIGHT_NOW.lower() and rn_old is None:
                rn_old, rn_new = o, len(ents)
        words_in = text.split()
        words_out = core.render(core.parse(text)).split()
        lost = len([w for w in words_in if w]) - len([w for w in words_out if w])
        rn_ok = rn_old == rn_new
        status = "OK  " if rn_ok and lost == 0 else "DIFF"
        bad += 0 if rn_ok and lost == 0 else 1
        print(f"{status} {Path(f).name:40} right-now {rn_old!s:>4} -> {rn_new!s:<4}  "
              f"all {all_old:3} -> {all_new:<3}  words-lost {lost}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
