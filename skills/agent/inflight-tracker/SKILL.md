---
name: inflight-tracker
description: Shared cross-session work tracker; read at start, update at end.
version: 0.1.0
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
inflight sessions                     # owner + ACTIVE/IDLE/ENDED per entry
inflight me                           # this session's tag
inflight add "<thing>: <state>" "<not done; next step; who decides>"
inflight check                        # lint; exit 1 on findings
inflight trim                         # dry run; --apply archives old entries
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
3. Resolved work: delete its entry. The tracker is state, not history.
4. Done when `inflight check` exits 0 or its only finding is the size budget.

### What a good entry says

- Headline: thing + state, e.g. `consent-gate v2: committed, NOT pushed.`
- What did NOT land: unpushed, unrestarted, unverified, waiting on a human.
- The next concrete step and the handle (SHA, path, PR, process id).
- One paragraph. Long analysis goes in a file; link the path.
- Multi-step work: optional checkbox lines under the head (`- [ ]` open,
  `- [x]` done, `- [~]` blocked + why). `inflight sessions` derives
  `done/total` from them.

## Pitfalls

- **Stale ≠ wrong, fresh ≠ right.** Entries describe; disk decides.
- **Resuming an ACTIVE session** puts two writers on one conversation. Nothing locks it.
- **Hand edits race.** `inflight add`/`trim` refuse to write (exit 3) if the
  file changed under them; re-run. Plain editor saves have no such guard.
- **Size is a per-turn cost.** Harnesses that inject the file every turn pay
  for every byte. `trim` enforces a byte + line budget and an age limit;
  archived text goes to `inflight-archive/`, nothing is deleted.
- **Undated entries are archived first** under budget pressure. Date them.
- **No secrets, no customer PII, no HR data** in entries. The file is plain
  text on disk and gets pasted into model context.

## Verification

- `inflight check` exits 0.
- `inflight sessions` lists your new entry as `(this session)`.
