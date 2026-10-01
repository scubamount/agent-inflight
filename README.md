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

That creates a private venv (stdlib `venv`, offline) at
`<home>/inflight-state/venv`, links a launcher pinned to it as
`~/.local/bin/inflight`, creates the tracker if missing, and (when Hermes is
installed) adds the `inflight-tracker` skill. Update with
`git -C ~/agent-inflight pull --ff-only && ~/agent-inflight/install.sh`.
Remove with `./uninstall.sh` (your tracker file is kept).

Then wire your agent:

| Harness | Do |
|---|---|
| Hermes | Nothing required; the skill is installed, and the plugin is linked (enable it with `plugins.enabled: [agent-inflight]`). Optional: two SOUL.md lines and a trim cron, see [adapters/hermes](adapters/hermes/README.md). |
| Claude Code | `inflight adapter install claude-code` (dry run), then `--apply`: session tags, heartbeats, collision warnings, re-inject after `/compact`, catch-up. See [adapters/claude-code](adapters/claude-code/README.md). |
| Codex, OpenCode, Cursor, others | Paste [adapters/generic/AGENTS-snippet.md](adapters/generic/AGENTS-snippet.md) into your user-level instruction file; add the crontab line for trimming. If the harness can run hook commands, wire the hook protocol: [docs/extending.md](docs/extending.md). |

## Commands

| Command | Does |
|---|---|
| `inflight init` | Create the tracker from the template if missing. Never overwrites. |
| `inflight add "<head>" [body]` | Prepend `**<date time> [session <id> #<entry-id>] — <head>.** <body>` to `## Right now`. The 6-hex entry id is the stable key `done` matches on. Reads stdin if no args. Exit 3 lock/race, 4 would forge an entry, 5 looks like a credential (`--force` overrides 5 only). |
| `inflight sessions [--json] [--children]` | Owner, status, progress, and drill/resume commands for each tagged entry. `--children` lists the delegated subagent sessions of each entry's conversation, read from the backend (never stored in the file). |
| `inflight me` | Print this session's `[session <id>]` tag. |
| `inflight done <match> [--reopen] [--dry-run]` | Mark one entry done: `<match>` is its id (`a1b2c3`) or a unique substring of the head. Exit 1 no match, 2 ambiguous (lists candidates), 3 file changed. `--reopen` makes it active again. |
| `inflight trim [--apply]` | Pause active entries untouched for `INFLIGHT_STALE_DAYS`; archive done entries after a day; then, over budget, archive done → paused → active (oldest first). Active entries are never archived for age. Dry run by default; lists every pause/archive. |
| `inflight check` | Lint: missing section, duplicate `## Right now`, over budget, undated entries, unexpanded `$VAR` tags, credential-looking text, tracker readable by others. Exit 1 on findings. |
| `inflight path` | Print the resolved tracker path. |
| `inflight audit [--apply] [--catch-up] [--json]` | Owed git work (uncommitted, unpushed, branch without upstream, stash) per session. Repos come from hook state (recorded per session) plus the scan roots in `inflight-state/config.json` (`audit.roots`, default `[{"path": "~/code", "depth": 3}]`). A repo whose last recorder ENDED (or went idle past `--stale-min`, default 120) gets one entry per (session, repo), tagged with that session and updated in place on re-runs; clean again → marked done, never deleted. Repos an ACTIVE session recorded are skipped. Scan-root repos no session recorded are listed as **unowned** and never written. Unpushed = commits on **no** remote-tracking ref (`rev-list <branch> --not --remotes`); a branch merely ahead of a stale upstream ref, with its commits on another remote, is a **note**, not owed work. A branch whose every commit on no remote has a patch-id twin on the remote default branch (rebased / cherry-picked; `rev-list --cherry-pick --right-only <default>...<branch>`) is listed in a separate **already on <default>** section with its name and count, never dropped; a squash of several commits has no twin and stays unpushed, and a failed or timed-out check keeps the branch unpushed. No network calls (refs are as last fetched). Optional `audit.ignore_branches` (glob list, off by default; `inflight check` prints it when set). Dry run by default (`--dry-run` accepted, wins over `--apply`). `--catch-up` = recorded repos only (what `session-start` and the cron run). |
| `inflight adapter install\|uninstall\|status claude-code [--apply]` | Claude Code user-level hooks. Dry run prints the exact diff; `--apply` backs up and merges, never touching existing hooks; refuses under managed hook lockdown. See [adapters/claude-code](adapters/claude-code/README.md). |
| `inflight hook <event> [--harness <name>]` | Hook protocol v1: one JSON object on stdin, always exit 0. See [docs/hook-protocol.md](docs/hook-protocol.md). |
| `inflight plugin list\|enable\|disable <name>` | Allowlist for backend plugins (entry points). Nothing is imported until enabled; every change is logged to `hooks.log`. Writing one: [docs/extending.md](docs/extending.md). |

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
| `INFLIGHT_HOME` | `$HERMES_HOME`, else `~/.hermes` if it exists, else `~/.agent-inflight` | Directory for the tracker, `inflight-archive/` and `inflight-state/` (venv, launcher, per-session hook state, plugin allowlist). |
| `INFLIGHT_SESSION_ID` | `$HERMES_SESSION_ID`, then `$CLAUDE_CODE_SESSION_ID` | Id written into the tag. |
| `INFLIGHT_DAYS` | `7` | Non-`Right now` sections dated older than this are archived. |
| `INFLIGHT_STALE_DAYS` | `3` | An active entry untouched this long (head date and its session's last activity) becomes `paused`. |
| `INFLIGHT_DONE_GRACE_DAYS` | `1` | Done entries are archived after this. |
| `INFLIGHT_REINJECT` | `1` | Hermes plugin: `0` turns off post-compaction re-injection. |
| `INFLIGHT_MAX_BYTES` | `24000` (~6k tokens) | Byte budget for the file. |
| `INFLIGHT_MAX_LINES` | `80` | Line budget for `## Right now`. |
| `INFLIGHT_MIN_ENTRIES` | `3` | Newest entries always kept, whatever the budget. |

## Session status backends

`inflight sessions` needs to know whether a session id is still alive. It
asks backends in order; the first that knows the id answers:

1. **Plugins** you enabled with `inflight plugin enable <name>`, in that
   order. A plugin is a Python package with an `agent_inflight.backends`
   entry point installed into `<home>/inflight-state/venv`; it is never
   imported until enabled, and only by `sessions`/`trim` (never by `add`,
   `check` or hooks). Contract: `src/agent_inflight/backends.py`
   (`PLUGIN_API_VERSION = 1`). How to write one, with a worked example
   package (`examples/backend-example`) that CI installs and runs:
   [docs/extending.md](docs/extending.md).
2. **Hermes**: reads `state.db` read-only across profiles, follows
   context-compression children.
3. **Heartbeat**: state files written by `inflight hook`, for any harness that
   runs the hooks.

Without a backend the command still lists every tagged entry with status
`NO-BACKEND`; the tags and dates alone are enough to hand work between sessions.

## Guarantees

- **Nothing is deleted.** Trimmed text goes to `inflight-archive/inflight-<stamp>.md`.
- **Unfinished work is not archived for age.** Age pauses an entry; only `done`, or the byte budget, archives it.
- **Idempotent.** `trim --apply` over its own output writes nothing and no archive.
- **Race-safe writes.** Every writer (`add`, `done`, `trim`, the Hermes plugin)
  holds an `flock` on `inflight.md.lock` and writes atomically. `add` retries
  when an unlocked writer (an editor) races it; `done`/`trim` refuse (exit 3).
- **Private files.** Tracker and archive files are `0600`, the archive dir
  `0700`. `check` flags a tracker readable by others.
- **No forged entries.** `add` refuses (exit 4) a headline with a newline or a
  session/entry tag, and a body line that starts a dated entry or `## `
  section, so one call can't write an entry tagged as another session.
- **No secrets.** `add` refuses (exit 5) text matching common credential
  shapes (API keys, GitHub/Slack/AWS tokens, private-key blocks, JWTs, URLs
  with passwords) and names only the kind, never the value. `--force` for a
  false positive. `check` reports matching entries by id.
- **Read-only session lookups.** `state.db` is opened with `mode=ro`.
- **Audit can't run repo code.** Every git call goes through `safe_git()`:
  read-only subcommands only, 5 s timeout, `core.fsmonitor=false`,
  `core.hooksPath=/dev/null`, and every filter driver the repo itself defines
  (`.git/config`, `include.path`) blanked on the command line, because a
  repo-local clean filter runs during a plain `git status`. `status` never
  recurses into submodules (`--ignore-submodules=all`): git would run there
  with the submodule's own config, which these flags don't reach; submodules
  are audited as their own repos when found or recorded. A hostile-repo
  fixture test (fsmonitor, clean/process filters, include.path, hooks, a
  submodule with its own filter) proves
  no marker is written, and a control arm proves plain git would write them.
- **Catch-up never claims work.** Audit entries carry the dead session's tag.
  Taking one over (`(took over <id>)`) is always an explicit act.

## Security

The tracker is plain text that gets pasted into model context. Never put
credentials, customer data, or HR/personnel data in an entry. Link to the
system of record instead.

## Develop

```bash
make test        # CLI arms (real CLI via subprocess, temp home) + plugin unit tests
                 # test_extending runs docs/extending.md's walkthrough (pip; needs network, required under CI=true)
```
