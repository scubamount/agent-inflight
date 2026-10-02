# Hook protocol (version 1)

`HOOK_PROTOCOL_VERSION = 1` (`src/agent_inflight/hook.py`). This is the main public interface. Any harness that can run a command on lifecycle events can use agent-inflight, with no Python plugin needed. How to wire a new harness, and when a backend plugin is worth writing: [extending.md](extending.md).

```text
inflight hook <event>   < one JSON object on stdin
```

## Rules every event follows

- **Exit status is always 0.** In Claude Code, exit 2 from a `PreToolUse` hook blocks the tool, so every error is caught, logged to `hooks.log` as `hook-error` (exception class name only) and swallowed. Bad JSON, a non-object, an unknown event or a missing session id all produce exit 0 with no output.
- **Unknown fields are ignored.** Fields may be added in later versions without bumping the protocol version. Removing or changing a field bumps it.
- **stdout is plain text for the model, or nothing.** Only `session-start` (on resume or compact) and `pre-tool` (on a collision) print anything.
- **`args` (tool arguments) is accepted and never read, stored or logged.**
- **`session_id`** falls back to `$INFLIGHT_SESSION_ID`, `$HERMES_SESSION_ID`, then `$CLAUDE_CODE_SESSION_ID`. It must match `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`, with no `..`. Anything else is refused (logged as `hook-refused`), and the id is never used as a file name.

## Harness flag

`inflight hook <event> --harness <name>` records the harness name in the session state. With `claude-code`:

- Claude Code's own field names are read: `tool_name`, and `new_cwd` on `CwdChanged`.
- Text meant for the model is printed as `{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}`. Claude Code passes plain stdout to the model only on `SessionStart`.
- No permission decision is ever emitted.

## Events

| Event | Payload | Effect | stdout |
|---|---|---|---|
| `session-start` | `session_id, cwd, source` (`new`, `startup`, `resume`, `compact` or `clear`), optional `harness` | Records the session and clears any ended flag. Does **not** record `cwd`'s repo: opening a session in a repo is not touching it, and recording it would make the new session the repo's latest recorder and take over the owed work a dead session left there. Tools and `cwd-changed` record repos. On `new`/`startup`: runs the audit catch-up (recorded repos of ENDED sessions only, built-in backends only, about 3 s budget, at most every 10 min unless a session ended since the last run; writes `## Right now` entries tagged with the dead session). | On `resume`/`compact`: the session's own open entries, as the same "verify on disk" block the Hermes plugin injects |
| `pre-tool` | `session_id, cwd, tool, call_id` | Heartbeat | A collision warning, once per (session, repo), when another session with a heartbeat in the last 15 min touched the same repo |

The Hermes plugin gives Hermes sessions the same warning (same text, same once-per-(session, repo) rule). Hermes discards `pre_tool_call` return values, so the plugin appends the warning to the tool result in `transform_tool_result`: the call has already run, and the model reads the warning before it continues.
| `post-tool` | `session_id, cwd, tool, call_id` | Heartbeat. Records `cwd`'s repo as touched | none |
| `cwd-changed` | `session_id, cwd` | Heartbeat. Records the repo | none |
| `session-end` | `session_id, cwd, reason` | Sets `ended_at` and `end_reason`. Nothing else: harnesses give this hook about 1.5 s and crashes skip it, so the owed-work audit runs later as catch-up | none |
| `heartbeat` | `session_id` | Heartbeat | none |

A "repo" is the nearest ancestor of `cwd` containing `.git`. It is found by checking the filesystem only, with no git process, so a hostile repo config can't run anything here.

## Files

| Path | Mode | Content |
|---|---|---|
| `<tracker dir>/inflight-state/` | 0700 | |
| `…/sessions/<id>.json` | 0600 | `session, started_at, heartbeat_at, ended_at, end_reason, repos{path: ts}, warned[], harness` |
| `<tracker dir>/inflight-archive/hooks.log` | 0600 | One JSON line per action. Only these keys are written: `ts, action, event, session, entry_id, kinds, plugin, error` |

`hooks.log` also records every `inflight add --force` that overrides the credential check (`action: secret-force`, entry id and pattern kinds only), as an audit trail.

`<tracker dir>/sessions/` is deliberately not used: under Hermes that folder holds the harness's own transcripts.

## Status from heartbeats

The `heartbeat` backend reads these files. `ended_at` set means ENDED. A heartbeat within `--active-min` (default 15) means ACTIVE. Anything else is IDLE. Harness backends (`hermes`, plugins) answer first. Heartbeat is the fallback.
