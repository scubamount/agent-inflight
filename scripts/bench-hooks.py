#!/usr/bin/env python3
"""Hook latency: N cold runs of `inflight hook <event> --harness claude-code`
through a given launcher, against a throwaway home. Prints p50/p95/max ms.

  scripts/bench-hooks.py [--launcher PATH] [-n 100]
"""
import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launcher", default=str(ROOT / "bin" / "inflight"))
    ap.add_argument("-n", type=int, default=100)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        (home / "inflight.md").write_text("## Right now\n\n")
        repo = home / "repo"
        (repo / ".git").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "INFLIGHT_", "CLAUDE_"))}
        env.update(INFLIGHT_HOME=str(home), HERMES_ROOT=str(home))
        payload = json.dumps({"session_id": "bench-1", "cwd": str(repo), "tool_name": "Edit",
                              "tool_input": {"file_path": str(repo / "x")}})
        res = {}
        for ev in ("pre-tool", "post-tool"):
            ms = []
            for _ in range(args.n):
                t0 = time.perf_counter()
                p = subprocess.run([args.launcher, "hook", ev, "--harness", "claude-code"], input=payload,
                                   capture_output=True, text=True, env=env)
                ms.append((time.perf_counter() - t0) * 1000)
                assert p.returncode == 0, p.stderr
            ms.sort()
            res[ev] = {"n": len(ms), "p50": round(statistics.median(ms), 1),
                       "p95": round(ms[int(len(ms) * 0.95) - 1], 1), "max": round(ms[-1], 1)}
        print(json.dumps({"launcher": args.launcher, **res}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
