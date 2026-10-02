# Claude Code adapter

Adds agent-inflight's hooks to your **user-level** `~/.claude/settings.json`.
It never touches project `.claude/settings.json` files or managed settings.

```bash
inflight adapter install claude-code            # dry run: prints the exact diff
inflight adapter install claude-code --apply    # backup, then merge
inflight adapter status claude-code
inflight adapter uninstall claude-code --apply  # removes only inflight's handlers
```

## What gets added

Each handler runs the pinned launcher (`<tracker dir>/inflight-state/inflight`)
as `"<launcher>" hook <event> --harness claude-code`. That command string is
the marker `uninstall` uses to find inflight's handlers.

| Claude Code event | Matcher | inflight event | Mode |
|---|---|---|---|
| `SessionStart` | `startup\|resume\|clear\|compact` | `session-start` | sync, 10 s timeout |
| `PreToolUse` | `^(Edit\|Write\|NotebookEdit\|Bash)$` (anchored: matchers are unanchored regexes) | `pre-tool` | sync, 5 s timeout |
| `PostToolUse` | same | `post-tool` | `async` |
| `CwdChanged` | none (the event has no matcher support) | `cwd-changed` | `async` |
| `SessionEnd` | none | `session-end` | sync, inside the default 1.5 s budget |

- The hook command **always exits 0** and **never** returns a
  `permissionDecision`, so it can't block or approve a tool.
- On `SessionStart` (resume/compact) and `PreToolUse` (collision warning), the
  text for the model is returned as `hookSpecificOutput.additionalContext`
  JSON. Plain stdout is delivered to the model only on `SessionStart`.
- `SessionStart` with `startup` or `clear` also runs the audit catch-up: owed
  git work of ended sessions becomes tracker entries tagged with the ended
  session. It has a 3 s budget and runs at most once every 10 minutes.

Facts verified against code.claude.com/docs/en/hooks (Claude Code 2.1.284).

## How install merges

- Existing hook groups are never changed, reordered or removed. inflight's
  groups go at the end of each event's list.
- A timestamped backup is written first:
  `settings.json.inflight-bak-<stamp>` (0600, never overwritten).
- The file's mode is kept. A new file is created 0600.
- `uninstall` leaves unrelated hooks byte-for-byte the same, provided the file
  is in the standard 2-space JSON layout. Other layouts are refused unless you
  pass `--allow-reformat`. The settings file itself is never deleted.
- Running install again changes nothing (`no change`).

## Managed settings

If a managed source sets `allowManagedHooksOnly`, `strictPluginOnlyCustomization`
covering hooks, or `disableAllHooks`, user hooks would never run. `install`
prints the diff, says which setting blocks it, and writes nothing (exit 3).
The fix is for IT to deploy the hooks through managed settings, or to allow
user hooks. inflight never edits managed settings and never works around
them. Until then, use `inflight add` by hand.

Sources checked: `/Library/Application Support/ClaudeCode/managed-settings.json`
(macOS) or `/etc/claude-code/managed-settings.json` (Linux), their
`managed-settings.d/*.json`, the `com.anthropic.claudecode` managed-preferences
plist (macOS), and `~/.claude/remote-settings.json`. That last path is the
server-managed cache as observed on one machine; it is not documented.

## What the hooks read and write

- **Read:** `session_id`, `cwd` (`new_cwd` on `CwdChanged`), `tool_name`, `source`,
  `reason`. `tool_input` (the tool's arguments) is never read, stored or logged.
- **Write:** `<tracker dir>/inflight-state/sessions/<id>.json` (0600): heartbeat,
  repos touched, ended time and reason, harness. Also one line per action in
  `inflight-archive/hooks.log` (0600, whitelisted keys only). The tracker
  itself is written only by catch-up.

## Latency

Measured with `scripts/bench-hooks.py`: 100 cold runs per event through the
pinned launcher, macOS arm64, Python 3.13. Budget: p95 < 100 ms.

| Event | p50 | p95 | max |
|---|---|---|---|
| `pre-tool` | 35.6 ms | 39.2 ms | 40.9 ms |
| `post-tool` | 35.9 ms | 40.8 ms | 47.5 ms |
