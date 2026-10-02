"""Example agent-inflight backend: session status from a harness's JSON file.

Imagine a harness ("example-harness") that keeps its sessions in
`~/.example-harness/sessions.json`:

    {"sessions": {"<session id>": {"title": "...", "started_at": 1759300000.0,
                                   "last_activity_at": 1759300600.0,
                                   "ended_at": null, "end_reason": null}}}

This backend answers `inflight sessions` for those ids. It is read-only, makes
no network calls, never raises out of a method, and ignores a file larger
than 1 MiB. Set EXAMPLE_HARNESS_SESSIONS to read another path.

Contract (agent-inflight PLUGIN_API_VERSION 1, duck-typed; no import of
agent_inflight needed):
  name: str
  available() -> bool
  lookup(session_id) -> dict | None
  optional: drill(session_id, profile) -> [str]
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_API_VERSION = 1
MAX_BYTES = 1 << 20
# Only these keys are passed back to agent-inflight; anything else in the
# harness file (prompts, paths, tokens) is dropped here, never forwarded.
FIELDS = ("status", "title", "started_at", "last_activity_at", "ended_at", "end_reason")
STATUSES = ("ACTIVE", "IDLE", "ENDED", "UNKNOWN")


def sessions_file() -> Path:
    env = os.environ.get("EXAMPLE_HARNESS_SESSIONS")
    return Path(env).expanduser() if env else Path.home() / ".example-harness" / "sessions.json"


class Backend:
    name = "example"

    def available(self) -> bool:
        return sessions_file().is_file()

    def _sessions(self) -> Dict[str, Any]:
        p = sessions_file()
        try:
            if p.stat().st_size > MAX_BYTES:
                return {}
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        s = data.get("sessions") if isinstance(data, dict) else None
        return s if isinstance(s, dict) else {}

    def lookup(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._sessions().get(session_id)
        if not isinstance(row, dict):
            return None  # not ours: the next backend in the chain is asked
        out: Dict[str, Any] = {}
        for k in FIELDS:
            v = row.get(k)
            if v is None:
                continue
            if k == "status" and v not in STATUSES:
                continue  # let agent-inflight derive it from the times
            if k in ("started_at", "last_activity_at", "ended_at") and not isinstance(v, (int, float)):
                continue
            out[k] = v if not isinstance(v, str) else v[:200]
        out["profile"] = "example-harness"
        return out

    def drill(self, session_id: str, profile: str) -> List[str]:
        return [f"example-harness show {session_id}", f"example-harness resume {session_id}"]
