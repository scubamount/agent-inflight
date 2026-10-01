# agent-inflight

A shared "what's open right now" file for AI coding agents, plus a small CLI
that keeps it honest: every entry is dated and tagged with the session that
wrote it, `inflight sessions` tells you whether that session is still
running, and `inflight trim` keeps the file small enough to load every time.

Stdlib Python ≥ 3.9. No dependencies, no network, no daemon.

## Why

Agents forget everything between sessions, and you often run several at once.
The usual result:

- A session commits work, runs out of context or gets closed, and the push
  never happens. Nobody remembers it exists until something breaks.
- Two sessions edit the same repo at the same time and clobber each other.
- Every new session starts with "what were we doing?" and re-derives state
  from git log, or you type it again.
- A notes file that fixes this grows forever until it costs thousands of
  tokens on every turn and nobody reads it.

agent-inflight is the smallest mechanism that closes those gaps. Longer
version with the failure modes it was built from: [docs/why.md](docs/why.md).

## What it looks like

```markdown
## Right now

**2026-09-30 14:05 [session 20260930_130520_a7dc5f] — billing-api retry fix: committed, NOT pushed.**
Branch fix/retry-backoff at 3f2a9c1. Tests pass locally; CI not run. Next: push + open PR.

**2026-09-29 18:40 [session 20260929_101122_9be004] — staging gateway: config changed, NOT restarted.**
Restart with `systemctl restart gw` after 18:00 (traffic window).
```

```text
$ inflight sessions
ACTIVE     20260930_130520_a7dc5f @default (this session)  last 2m ago
           entry : 2026-09-30 14:05 [session 20260930_130520_a7dc5f] — billing-api retry fix: ...
ENDED      20260929_101122_9be004 @default  last 20h ago  ended: user_close
           entry : 2026-09-29 18:40 [session 20260929_101122_9be004] — staging gateway: ...
           drill : lcm_load_session(session_id='20260929_101122_9be004')  |  session_search(...)
           resume: hermes --resume 20260929_101122_9be004
```

The ENDED entry is orphaned work: its session is gone and the restart is
still owed. The next agent picks it up.

## Install

```bash
git clone git@github.com:scubamount/agent-inflight.git ~/agent-inflight
~/agent-inflight/install.sh            # add --hermes-cron on Hermes for the daily trim job
```

That links `inflight` into `~/.local/bin`, creates the tracker if missing,
and (when Hermes is installed) adds the `inflight-tracker` skill. Update with
`git -C ~/agent-inflight pull --ff-only && ~/agent-inflight/install.sh`.
Remove with `./uninstall.sh` (your tracker file is kept).

Then wire your agent:

| Harness | Do |
|---|---|
| Hermes | Nothing required; the skill is installed, and the plugin is linked (enable it with `plugins.enabled: [agent-inflight]`). Optional: two SOUL.md lines and a trim cron, see [adapters/hermes](adapters/hermes/README.md). |
| Claude Code, Codex, OpenCode, Cursor, others | Paste [adapters/generic/AGENTS-snippet.md](adapters/generic/AGENTS-snippet.md) into your user-level instruction file; add the crontab line for trimming. |

## Commands

| Command | Does |
|---|---|
| `inflight init` | Create the tracker from the template if missing. Never overwrites. |
| `inflight add "<head>" [body]` | Prepend `**<date time> [session <id> #<entry-id>] — <head>.** <body>` to `## Right now`. The 6-hex entry id is the stable key `done` matches on. Reads stdin if no args. |
| `inflight sessions [--json] [--children]` | Owner, status, progress, and drill/resume commands for each tagged entry. `--children` lists the delegated subagent sessions of each entry's conversation, read from the backend (never stored in the file). |
| `inflight me` | Print this session's `[session <id>]` tag. |
| `inflight trim [--apply]` | Archive entries past the age limit, then oldest-first until under budget. Dry run by default. |
| `inflight check` | Lint: missing section, duplicate `## Right now`, over budget, undated entries, unexpanded `$VAR` tags. Exit 1 on findings. |
| `inflight path` | Print the resolved tracker path. |

## Entry format

An entry starts at a line beginning `**YYYY-MM-DD` and runs to the next one.
Bold text inside an entry (`**Note:** ...`) stays part of it. Bold lines
before the first dated entry are not entries; `inflight check` reports them.
`scripts/parser-parity.py FILE...` compares entry counts under the old
blank-line rule and the current one.

## Progress (optional)

Checkbox lines in an entry body are counted on every read; nothing is stored:

```markdown
**2026-09-30 19:10 [session S] — rollout.** prose
- [x] step one
- [ ] step two
- [~] push (blocked: waiting on review)
```

`inflight sessions` shows `state : active  progress 1/3 (1 blocked)`.
Head lines and fenced code blocks are never counted.

## Configuration

All optional, via environment:

| Variable | Default | Meaning |
|---|---|---|
| `INFLIGHT_FILE` | `<home>/inflight.md` | Tracker path. |
| `INFLIGHT_HOME` | `$HERMES_HOME`, else `~/.hermes` if it exists, else `~/.agent-inflight` | Directory for the tracker and `inflight-archive/`. |
| `INFLIGHT_SESSION_ID` | `$HERMES_SESSION_ID`, then `$CLAUDE_SESSION_ID` | Id written into the tag. |
| `INFLIGHT_DAYS` | `7` | Entries dated older than this are archived. |
| `INFLIGHT_MAX_BYTES` | `24000` (~6k tokens) | Byte budget for the file. |
| `INFLIGHT_MAX_LINES` | `80` | Line budget for `## Right now`. |
| `INFLIGHT_MIN_ENTRIES` | `3` | Newest entries always kept, whatever the budget. |

## Session status backends

`inflight sessions` needs to know whether a session id is still alive.
Built in: **Hermes** (reads `state.db` read-only across profiles, follows
context-compression children). Without a backend the command still lists
every tagged entry with status `NO-BACKEND`; the tags and dates alone are
enough to hand work between sessions. Adding a backend = one class with
`available()`, `lookup(id)`, `drill(id, profile)` in
`src/agent_inflight/sessions.py`.

## Guarantees

- **Nothing is deleted.** Trimmed text goes to `inflight-archive/inflight-<stamp>.md`.
- **Idempotent.** `trim --apply` over its own output writes nothing and no archive.
- **Race-safe writes.** `add` and `trim` write atomically and refuse (exit 3)
  if another session changed the file between read and write.
- **Read-only session lookups.** `state.db` is opened with `mode=ro`.

## Security

The tracker is plain text that gets pasted into model context. Never put
credentials, customer data, or HR/personnel data in an entry. Link to the
system of record instead.

## Develop

```bash
make test        # CLI arms (real CLI via subprocess, temp home) + plugin unit tests
```
