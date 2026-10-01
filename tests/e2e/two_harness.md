# E2E: two harnesses, one repo (manual; results go in the execution report)

**Manual only.** CI does not run this. It needs real interactive Claude Code and Hermes
sessions (/clear, /compact, quit + reopen), and those can't be driven headless. CI covers
the same code paths with piped JSON (`tests/test_adapter.py` WireFormat, `tests/test_hooks.py`),
but that is a substitute for this e2e run, not an equivalent.

Test repo **R** = `/tmp/inflight-e2e-R` (throwaway; `git init`, one empty commit, no remote
is fine — audit treats "no upstream" as owed work). Never use a real repo.

Prereq: `inflight adapter status claude-code` → `5 inflight handler(s) installed (of 5)`.
Paste each step's output (or a screenshot) back to the agent. Step owner in brackets.

## Part A — Claude Code alone (Gate B live checks)

1. [Andrew] `cd /tmp/inflight-e2e-R && claude` (new session). Ask it to run `inflight me`.
   Expect: a tag containing `$CLAUDE_CODE_SESSION_ID`, not empty, no literal `$VAR`.
2. [Andrew] In that session: `inflight add "e2e: CC entry" "part A"`, then `inflight sessions`.
   Expect: entry tagged with the CC session; status ACTIVE (heartbeat backend).
3. [Andrew] Ask it to edit `R/a.txt` (any content). Then `/compact`.
   Expect: after compaction, a block starting `[inflight: your open entries` containing "e2e: CC entry".
4. [Andrew] `/clear`. In the new conversation run `inflight sessions`.
   Expect: the old CC session ENDED, reason `clear`.
5. [Andrew] Leave `a.txt` uncommitted, quit Claude Code (`/exit`), open a new `claude` in R.
   Then `inflight sessions` and read § Right now.
   Expect: a catch-up audit entry for R tagged with the **old** session id ("uncommitted").

## Part B — Hermes ↔ Claude Code handoff (plan Phase 4)

6. [agent] Hermes session H: `hermes -z` one-shot that commits `a.txt` in R and does not push.
   Agent records H's session id.
7. [Andrew] New `claude` in R. Expect: `inflight sessions` shows H ENDED with owed work and the
   audit entry (unpushed / no upstream).
8. [Andrew] In C: edit the entry to add `(took over <H-id>)`, then `inflight done <entry-id>`.
9. [agent] Hermes session H2: `inflight sessions` shows the entry done; `inflight trim --apply`
   archives it after the grace period (agent may use `--dry-run` to show what is pending).
10. [Andrew + agent] Keep C open and touch R (any edit). Agent runs a Hermes session H3 that
    edits R. Expect: collision warning naming C, delivered as context, tool not blocked.

Pre-checks with piped JSON were run before this script (see report); they are not the e2e.
Cleanup: `rm -rf /tmp/inflight-e2e-R`; `inflight done` any leftover e2e entries.
