# E2E: two harnesses, one repo (manual)

**Manual only.** CI does not run this. It needs real Claude Code and Hermes sessions
(/clear, /compact, quit + reopen). CI covers the same code paths with piped JSON
(`tests/test_adapter.py` WireFormat, `tests/test_hooks.py`); that substitutes for this
run and is not equivalent.

Test repo **R** = `/tmp/inflight-e2e-R` (throwaway; `git init`, one empty commit, no remote
is fine: audit treats "no upstream" as owed work). Never use a real repo.

Prereq: `inflight adapter status claude-code` → `5 inflight handler(s) installed (of 5)`.
Step owner in brackets: **[human]** drives an interactive session; **[agent]** runs a
Hermes one-shot (`hermes -z`). Headless alternative for most [human] steps: `claude -p` and
`claude -p --resume <id>`; `/compact` and `/clear` work as the prompt. Step 10 needs a
session that stays open, so use interactive `claude` there.

## Part A: Claude Code alone

1. [human] `cd /tmp/inflight-e2e-R && claude` (new session). Ask it to run `inflight me`.
   Expect: a tag with the Claude Code session id (not a Hermes id inherited from a parent
   shell), not empty, no literal `$VAR`.
2. [human] In that session: `inflight add "e2e: CC entry" "part A"`, then `inflight sessions`.
   Expect: entry tagged with the CC session; status ACTIVE (heartbeat backend).
3. [human] Ask it to edit `R/a.txt` (any content). Then `/compact`.
   Expect: after compaction, a block starting `[inflight: your open entries` containing "e2e: CC entry".
4. [human] `/clear`. In the new conversation run `inflight sessions`.
   Expect: the old CC session ENDED, reason `clear`.
5. [human] Leave `a.txt` uncommitted, quit Claude Code (`/exit`), open a new `claude` in R
   right away. Then `inflight sessions` and read § Right now.
   Expect: a catch-up audit entry for R tagged with the **old** session id ("uncommitted"),
   and none tagged with the new session.

## Part B: Hermes ↔ Claude Code handoff

6. [agent] Hermes session H: a `hermes -z` one-shot that commits `a.txt` in R and does not
   push. Record H's session id.
7. [human] New `claude` in R (session C). Expect: `inflight sessions` shows H ENDED with owed
   work and the audit entry (unpushed / no upstream).
8. [human] In C: edit the entry to add `(took over <H-id>)`, then `inflight done <entry-id>`.
9. [agent] Hermes session H2: `inflight sessions` shows the entry done; `inflight trim --apply`
   archives it after the grace period (`--dry-run` shows what is pending).
10. [human + agent] Keep an interactive C open and have it touch R (any edit). The agent runs
    a Hermes session H3 that edits R. Expect: a collision warning in H3's tool result naming
    C only (no already-ended sessions); the tool is not blocked.

Cleanup: `rm -rf /tmp/inflight-e2e-R`; `inflight done` any leftover e2e entries.
