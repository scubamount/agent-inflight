# Why agent-inflight exists

The design rationale. For setup, see the [README](../README.md); for exact
behavior, see [reference.md](reference.md).

## The problem

An AI coding agent has no memory between sessions. Whatever it did is in git
(if it committed) and in its transcript (which the next session doesn't read).
Everything in between — "committed but not pushed", "changed the config but
didn't restart the service", "waiting for a human to pick option B" — lives
nowhere.

That gap gets worse as soon as you run more than one session: a desktop tab,
a terminal, a background worker, a second profile. Each one can leave work
half-landed, and each one can walk into a repo another is editing right now.

These are the failures this package was built from, all observed on one
machine over several months of daily multi-session agent work:

| Failure | What it cost |
|---|---|
| Work committed locally, session closed, push never happened | Commits sat 19 deep on a laptop for days; a later session assumed they were on the remote and built on a stale base. |
| Two sessions on one repo | One session's staged edits were swept into another's commit and had to be backed out by hand. |
| "Where were we?" at every session start | Minutes of re-deriving state from git log and chat scrollback, or the human retyping it. |
| A recap that lagged reality | A session planned to rewrite a file the recap called "unfinished"; the file had shipped hours earlier. |
| The notes file that fixed the above grew unbounded | 30 KB / 447 lines, ~7.5k tokens loaded on **every** turn, with a second stale copy of the live section nobody archived. |
| Hand-written session tags | Agents wrote the literal text `[session $HERMES_SESSION_ID]` instead of the id, so the entry could never be traced back to its session. |

## The mechanism

One file, one section that matters (`## Right now`), and three rules:

1. **Read at start.** Each session gets a brief: its own entries in full and
   one line per other entry. Before touching a repo or service, check whether
   the sessions that wrote its entries are still alive.
2. **Write at end.** Anything not landed gets one dated, session-tagged
   paragraph: what state it's in, what is NOT done, the next step, the handle
   (SHA, path, PR).
3. **Keep it small.** Old entries move to an archive automatically, so the
   brief stays cheap enough to give every session.

The CLI enforces the parts agents get wrong when doing it by hand:

- `inflight add` writes the date and the real session id, never a literal
  variable name.
- `inflight sessions` turns a tag into ACTIVE / IDLE / ENDED, so "is someone
  working on this?" has an answer instead of a guess. On Hermes it follows
  context-compression, so a tag on a compacted session resolves to the
  session that continues the work.
- `inflight trim` keeps the brief's headlines and the file inside their budgets, pauses stale entries,
  never deletes (archive only), and is idempotent so it can run from any
  update script or cron without churning.
- `inflight check` catches the file drifting out of shape.

## Why a markdown file and not a database

- Agents already read and write markdown well; there is no tool to teach.
- The human can read and edit it in any editor.
- It works in every harness, including ones with no plugin system.
- Diffing and archiving are trivial.

The cost is that plain editor saves aren't locked. `add` detects a concurrent
write and retries; `done` and `trim` refuse (exit 3). Keep hand edits short.

## What it is not

- Not a task manager. No priorities, assignees, or due dates. Use your issue
  tracker for planned work; this is for state that is *in flight*.
- Not a log. Resolved entries leave the file: `trim` moves them to the archive.
  History is git log and the archive.
- Not memory. It holds what is owed now, not facts to remember forever.

## Measuring whether it works

Signals worth watching after adoption:

- Sessions that start by reading the tracker instead of asking "what are we
  working on?".
- ENDED entries with owed work getting picked up (the `(took over <id>)` suffix).
- `inflight check` staying clean; file size staying under budget.
- Fewer "I thought that was pushed" moments.
