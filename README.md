# agent-inflight

**A shared handoff file for AI coding agents.** It records what is still
owed across every agent session on your machine (unpushed commits, services
not yet restarted, decisions waiting on you), which session owns each item,
and whether that session is still running.

When a session ends, crashes, runs `/clear`, or gets compacted, its unfinished
work is not lost. The next session (in the same harness or a different one)
reads the file, sees the open work, and picks it up.

- **One Markdown file.** Agents and humans read and edit it directly.
- **Small CLI** (`inflight`). It writes correctly dated, session-tagged entries,
  reports whether each owning session is ACTIVE, IDLE or ENDED, and trims the
  file so it stays cheap to load into context.
- **Works with any harness.** Built-in wiring for Claude Code and Hermes. Any
  other harness (Codex, OpenCode, Cursor, and so on) works through a pasted
  instruction snippet or a small hook protocol.
- **Stdlib Python 3.9+.** No dependencies, no network, no daemon. Linux and
  macOS.

## The problem it solves

Agents keep no memory between sessions, and you often run several at once.
State that is *in flight* (done locally but not landed) lives nowhere:

| Without it | With it |
|---|---|
| A session commits, gets closed, and the push never happens. Nobody notices for days. | The session's last act is `inflight add "...: committed, NOT pushed"`. If it dies first and hooks are wired, the catch-up audit finds the unpushed commits and writes the entry for it. |
| Two sessions edit the same repo and one sweeps the other's changes into its commit. | Before an edit, the second session gets a warning: another live session touched this repo in the last 15 minutes. |
| Every new session starts with "where were we?" | The session reads `## Right now` and runs `inflight sessions`. If it gets compacted, its own entries are re-injected. |
| The notes file that fixes all this grows until it costs thousands of tokens per turn. | `inflight trim` enforces a byte and line budget. It archives text and never deletes it. |

The failures this was built from, with what each one cost:
[docs/why.md](docs/why.md).

## What it looks like

The tracker (`inflight.md`) holds one paragraph per piece of open work, newest
first:

```markdown
## Right now

**2026-10-01 17:28 [session cc-91be #fe96e6] — staging gateway: config changed, NOT restarted.** Restart after 18:00 (traffic window): systemctl restart gw

**2026-10-01 17:28 [session cc-7f3a #d536ef] — billing-api retry fix: committed, NOT pushed.** Branch fix/retry-backoff at 3f2a9c1. Tests pass locally; CI not run. Next: push, open PR.
```

`inflight sessions` resolves each tag to a live status:

```text
$ inflight sessions
ENDED      cc-91be @claude-code  last 0m ago  ended: prompt_input_exit
           entry : 2026-10-01 17:28 [session cc-91be #fe96e6] — staging gateway: config changed, NOT restarted. Restart after 18:
ACTIVE     cc-7f3a @claude-code (this session)  last 0m ago
           entry : 2026-10-01 17:28 [session cc-7f3a #d536ef] — billing-api retry fix: committed, NOT pushed. Branch fix/retry-ba

2 tagged entries, 0 untagged
```

The ENDED entry is orphaned work: its session has exited, and the restart is
still owed. Any later session can take it over. The ACTIVE entry belongs to a
live session, so other sessions leave its files alone.

## How it works

Each session follows the same loop, enforced by the skill, the hooks or the
instruction snippet:

1. **Start: read.** Read `## Right now` and run `inflight sessions`.
   - ACTIVE owner on the same files: coordinate, don't edit them.
   - IDLE or ENDED owner with open work: verify it on disk, then take it over.
2. **End: write.** For anything not landed, run
   `inflight add "<thing>: <state>" "<what is not done; next step>"`.
   The CLI adds the date, the real session id and a stable entry id.
3. **Finish: close.** `inflight done <id>` marks the entry done. `trim`
   archives it after a day.
4. **Background: bound.** `inflight trim --apply` runs on a schedule. It pauses
   stale entries and archives done ones, keeping the file under budget.

With hooks wired (Claude Code, Hermes, or any harness that follows
[the hook protocol](docs/hook-protocol.md)), these also happen automatically:

| Event | What inflight does |
|---|---|
| An edit or shell tool call | Records which repo the session touched. Warns the agent once if another live session touched the same repo in the last 15 minutes. |
| After a compaction or resume | Re-injects this session's own open entries so the agent still knows them. |
| New session start (or the daily cron job on Hermes) | Catch-up audit: owed git work that ENDED sessions left behind becomes entries, tagged with the dead session. |
| Session end | Marks the session ENDED. |

## Install

```bash
git clone https://github.com/scubamount/agent-inflight.git ~/agent-inflight
~/agent-inflight/install.sh
inflight check          # expect: OK
```

`install.sh` works offline. It creates:

- a private stdlib venv;
- the `inflight` command, as a link at `~/.local/bin/inflight`;
- the tracker, if it doesn't exist yet.

When it finds Hermes, it also adds the Hermes skill and plugin. Flags:
`--no-skill`, `--no-plugin`, `--hermes-cron`.

| | Command |
|---|---|
| Update | `git -C ~/agent-inflight pull --ff-only && ~/agent-inflight/install.sh` |
| Uninstall | `inflight adapter uninstall claude-code --apply` (if you installed the hooks), then `~/agent-inflight/uninstall.sh`. Your tracker file is kept. |

## Connect your agent

| Harness | Setup | You get |
|---|---|---|
| **Claude Code** | `inflight adapter install claude-code` prints the exact diff; add `--apply` to write it. Details: [adapters/claude-code](adapters/claude-code/README.md). | Session tags, heartbeats, collision warnings, re-injection after `/compact` and resume, catch-up audit |
| **Hermes** | `install.sh` installs the skill and plugin. Enable the plugin with `plugins.enabled: [agent-inflight]`. For trimming and catch-up, use `install.sh --hermes-cron`. Details: [adapters/hermes](adapters/hermes/README.md). | Session tags, collision warnings, re-injection after compaction and resume, catch-up from the cron job, and status read from Hermes' own session database, which follows compression lineage |
| **Codex, OpenCode, Cursor, others** | Paste [the instruction snippet](adapters/generic/AGENTS-snippet.md) into your user-level `AGENTS.md` or rules file. Add the crontab line for trimming. | The read, write and close loop, driven by the agent |
| **Any harness that can run hook commands** | Wire [hook protocol v1](docs/hook-protocol.md); see [docs/extending.md](docs/extending.md). | Everything the Claude Code adapter gives you |

## Commands

| Command | Purpose |
|---|---|
| `inflight add "<head>" [body]` | Add a dated, session-tagged entry to `## Right now` |
| `inflight sessions` | Show each entry's owner, status (ACTIVE, IDLE, ENDED) and progress |
| `inflight done <id>` | Mark an entry done (`--reopen` makes it active again) |
| `inflight trim [--apply]` | Pause stale entries; archive done and over-budget ones (dry run by default) |
| `inflight check` | Lint the file: shape, budget, untagged entries, credential-like text, permissions |
| `inflight audit [--apply]` | Find owed git work (uncommitted, unpushed, stashed) for each session |
| `inflight adapter …`, `hook …`, `plugin …` | Harness wiring, hook protocol, backend plugins |
| `inflight init`, `path`, `me` | Create the file, print its path, print this session's tag |

Every flag, exit code and environment variable:
[docs/reference.md](docs/reference.md).

## Guarantees

- **Nothing is deleted.** Trimmed text moves to `inflight-archive/`.
- **Unfinished work is never archived for age.** Age only pauses an entry.
- **Hooks never block a tool.** They always exit 0 and never make a permission
  decision.
- **No forged entries.** One `add` call cannot write an entry tagged as
  another session.
- **No secrets.** `add` refuses text that looks like a credential, and never
  echoes the value.
- **Auditing can't run repo code.** Git runs read-only, with hooks,
  fsmonitor and every repo-defined filter disabled.
- **Private files and race-safe writes.** The tracker and archive files are
  mode `0600`. Writers take a lock and write atomically.

The exact behavior and how each guarantee is tested:
[docs/reference.md § Guarantees](docs/reference.md#guarantees).

## What it is not

- **Not a task manager.** It has no priorities, assignees or due dates. Plan
  work in your issue tracker; this file holds work that is already in flight.
- **Not a log.** Resolved entries leave the file. Your history is git log plus
  the archive.
- **Not long-term memory.** It records what is owed now, not facts to keep
  forever.

## Security

The tracker is plain text that gets pasted into model context. Never put
credentials, customer data or personnel data in an entry; link to the system
of record instead. To report a vulnerability, follow [SECURITY.md](SECURITY.md).

## Documentation

| Doc | Read it for |
|---|---|
| [docs/why.md](docs/why.md) | The observed failures, the design, and why it's a Markdown file |
| [docs/reference.md](docs/reference.md) | Commands, exit codes, entry format, configuration, status backends, files, guarantees |
| [docs/hook-protocol.md](docs/hook-protocol.md) | Hook protocol v1: events, payloads, outputs |
| [docs/extending.md](docs/extending.md) | Adding a harness, writing a backend plugin, writing an adapter |
| [adapters/](adapters/) | Per-harness setup: Claude Code, Hermes, generic |
| [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) | Development setup and rules, for humans and for coding agents |

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
