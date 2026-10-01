---
name: inflight-tracker
description: Shared cross-session work tracker; read at start, update at end.
version: 0.6.0
author: scubamount, Hermes Agent
license: Proprietary
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Agent, Coordination, Sessions, Handoff]
    related_skills: []
---

# Inflight Tracker

One markdown file (`inflight.md`) holds what is open right now across every
agent session on the machine: unpushed commits, unrestarted services,
decisions waiting on the human, work another session left half-done. The
`inflight` CLI writes tagged entries, tells you which session owns each one
and whether it is still running, and trims the file so it stays cheap to load.

## When to Use

- Session start, before any repo/infra work: read the tracker.
- Before touching files another session might be editing.
- The user says "where were we", "what's open", "check in-flight", "resume".
- Session end, or a task finishes with anything not landed.
- Don't use for: finished work with nothing owed (that belongs in git log),
  long analysis (write a file and link it), secrets or customer data (never).

## Quick Reference

```bash
inflight path                         # which file
inflight sessions                     # owner + ACTIVE/IDLE/ENDED + progress per entry
inflight sessions --children          # + delegated subagent sessions (resume handles)
inflight hook <event> < payload.json  # harness hooks (docs/hook-protocol.md); always exit 0
inflight plugin list                  # backend plugins; `enable <name>` to allowlist
inflight audit                        # owed git work per session (dry run); --apply writes catch-up entries
inflight me                           # this session's tag
inflight add "<thing>: <state>" "<not done; next step; who decides>"
inflight check                        # lint; exit 1 on findings
inflight done <id|words>              # mark finished; trim archives it after a day
inflight trim                         # dry run; --apply pauses stale, archives done/over-budget
```

## Procedure

### Start

1. `read_file` the path from `inflight path`, section `## Right now` only.
   Done when you know every entry touching the repo/service you're about to work on.
2. `terminal(command="inflight sessions")`. Done when each relevant entry has a status.
3. Act on status:
   | Status | Do |
   |---|---|
   | ACTIVE | a sibling is working now: don't edit the same files; coordinate through the tracker |
   | IDLE / ENDED | its "not pushed / in progress" items are orphaned: drill in with the printed command, verify on disk, take over |
   | UNKNOWN / NO-BACKEND | treat as unowned; verify on disk before trusting it |
4. Treat every entry as a hypothesis. Confirm named commits/files/services on
   disk before planning on them; an entry can lag reality by hours.
5. `— audit: <repo>: owed work.` entries were written by `inflight audit`
   for a session that ended with uncommitted/unpushed/stashed work. They stay
   tagged with the dead session. To pick one up, verify on disk, append
   `(took over <old-id>)`, and finish it; audit marks it done by itself once
   the repo is clean. Audit never writes `took over`.

Don't recite the tracker back to the user. Use it.

### End

1. `terminal(command='inflight add "<thing>: <state>" "<detail>"')` for each
   piece of work with anything owed. The CLI adds the date and the
   `[session <id>]` tag. Don't hand-write the tag: a literal
   `[session $HERMES_SESSION_ID]` is untrackable and `inflight check` flags it.
   On Hermes the agent-inflight plugin rewrites such a tag to the real id
   when your own edit wrote the entry, and says so in the tool result.
2. Taking over another session's entry: edit that entry in place and append
   `(took over <old-id>)`. Don't add a duplicate.
3. Resolved work: `inflight done <id>` (the `#a1b2c3` in its tag). Trim
   archives it after a day. Deleting the entry by hand also works.
4. Done when `inflight check` exits 0 or its only finding is the size budget.

### What a good entry says

- Headline: thing + state, e.g. `consent-gate v2: committed, NOT pushed.`
- What did NOT land: unpushed, unrestarted, unverified, waiting on a human.
- The next concrete step and the handle (SHA, path, PR, process id).
  Subagent session ids need not be typed: `inflight sessions --children`
  derives them from the session backend.
- One paragraph. Long analysis goes in a file; link the path.
- Multi-step work: optional checkbox lines under the head (`- [ ]` open,
  `- [x]` done, `- [~]` blocked + why). `inflight sessions` derives
  `done/total` from them.

## Pitfalls

- **Stale ≠ wrong, fresh ≠ right.** Entries describe; disk decides.
- **Resuming an ACTIVE session** puts two writers on one conversation. Nothing locks it.
- **Hand edits race.** CLI writers lock `inflight.md.lock`; `done`/`trim`
  refuse (exit 3) if the file changed under them; re-run. Plain editor saves
  take no lock — prefer `inflight add`.
- **`add` exit 4** = headline/body would forge an entry (newline in the
  headline, a tag, or a body line starting `**YYYY-MM-DD` / `## `). Rephrase.
- **`add` exit 5** = text looks like a credential. Remove it; tell the user to
  rotate it if real. `--force` only for a confirmed false positive.
- **Size is a per-turn cost.** Harnesses that inject the file every turn pay
  for every byte. `trim` enforces a byte + line budget and an age limit;
  archived text goes to `inflight-archive/`, nothing is deleted.
- **Entries start only at `**YYYY-MM-DD`.** Bold text without a leading date
  is part of the entry above (or reported by `check` if above all entries).
- **Stale ≠ archived.** An active entry untouched for 3 days becomes
  `status: paused (stale since D)`; it is archived only under budget
  pressure, after every done entry. Remove the status line (or
  `inflight done <id> --reopen`) to reactivate.
- **No secrets, no customer PII, no HR data** in entries. The file is plain
  text on disk and gets pasted into model context.

## Verification

- `inflight check` exits 0.
- `inflight sessions` lists your new entry as `(this session)`.
