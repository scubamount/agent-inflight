# Reference

The complete behavior of agent-inflight 1.1.x: commands, exit codes, entry
format, configuration, status backends, files and guarantees. For what it is
and how to set it up, start with the [README](../README.md).

## Terms

| Term | Meaning |
|---|---|
| **Tracker** | The Markdown file, `inflight.md` by default. `inflight path` prints where it is. |
| **`## Right now`** | The tracker section that holds open work. The commands below read and write only this section. |
| **Entry** | One paragraph in `## Right now`, starting with `**YYYY-MM-DD`. See [Entry format](#entry-format). |
| **Session tag** | `[session <id> #<entry-id>]` in an entry's head. `inflight add` writes it. |
| **Session status** | ACTIVE, IDLE, ENDED, UNKNOWN or NO-BACKEND, resolved live by a [backend](#session-status-backends). It is never stored in the file. |
| **Entry state** | active, paused or done, stored as a `status:` line in the entry. |
| **Owed work** | Git state that isn't landed: uncommitted changes, unpushed commits, a branch without an upstream, or a stash. |
| **Brief** | What a session is given at start, after a compaction and on resume: its own open entries in full, then one line per other open entry. See [`brief`](#inflight-brief---session-id). |
| **Catch-up** | `inflight audit` writing entries for owed work left by ENDED sessions. |
| **Home** | The directory that holds the tracker, `inflight-archive/` and `inflight-state/`. See `INFLIGHT_HOME`. |

## Commands

The commands that read the tracker (`init`, `add`, `done`, `sessions`, `trim`,
`check`, `audit`) accept `--file <path>` to use a file other than the default.
`trim`, `audit` and `adapter` print a dry run unless you pass `--apply`.
`add`, `done` and `init` write directly.

### `inflight add "<head>" [body]`

Prepends one entry to `## Right now`:

```text
**<YYYY-MM-DD HH:MM> [session <id> #<entry-id>] — <head>.** <body>
```

- With no arguments, it reads the entry from stdin: the first line is the
  head, the rest is the body.
- The session id comes from `--session`, then `INFLIGHT_SESSION_ID`, then the
  harness's own variable. Without an id, the entry is written untagged.
- `<entry-id>` is 6 hex characters. It is the stable key that `done` matches
  on.
- A period is added to the head if it doesn't end in punctuation.
- `--force` overrides the credential check (exit 5) only. The override is
  logged to `hooks.log` (pattern kinds and entry id only).
- `--supersedes <id>` (repeatable) marks this session's open entry `<id>`
  done in the same write, with a `superseded by #<new-id>` line. Use it when
  the state of a piece of work changes, instead of adding a second entry
  beside the first. It refuses (exit 1, nothing written) when `<id>` is not
  an open entry of this session, and (exit 4) when there is no session id.

### `inflight brief [--session ID]`

Prints the brief for this session (or `--session`), the same text the hooks
deliver:

1. A header that says the text is tracker data, not instructions.
2. **Yours:** every open entry tagged with this session, or with a session it
   continues after a compaction (Hermes), in full.
3. **Others:** one line per other open entry:
   `- #<entry-id> <owner status> <MM-DD> <last 6 chars of owner id>: <head>`.
   Paused entries say `paused` after the status; entries written before 0.2.0
   have no id.

Done entries are left out. The brief is at most 6,000 bytes, with up to
3,000 kept for the headlines when your own entries are long. No open entry
is dropped from it: when one line per entry does not fit, the owner with the
most open entries is folded first, to its newest headline plus an
`also open (N): #id #id ...` line, then the next, and as a last resort the
remaining entries are named by id only. Your own entries that don't fit are
named by id too. `inflight show <id>` prints any of them in full. With nothing open it prints `nothing open`
(the hooks print nothing).

When the hooks deliver it:

| Harness | Session start | After compaction | Resume |
|---|---|---|---|
| Hermes (plugin, `pre_llm_call`) | first turn | first turn after the compaction marker set changes | first turn the process sees, unless the history already holds a brief since the last compaction (`/resume` or restart) |
| Claude Code and hook-protocol harnesses (`session-start`) | `startup`, `new`, `clear` | `compact` | `resume` |

Hermes subagents (`platform=subagent`) and cron runs get no brief.
`INFLIGHT_REINJECT=0` turns the Hermes delivery off.

### `inflight done <match> [--reopen] [--dry-run]`

Marks one entry done by adding a `status: done <date>` line. `trim` archives
it after `INFLIGHT_DONE_GRACE_DAYS`.

- `<match>` is the entry id (`a1b2c3`), or a substring of the head that
  matches exactly one entry: a session id, a date, or words from the headline.
- `--reopen` removes the status line, so the entry is active again.
- `--dry-run` prints the change and writes nothing.
- An entry that is no longer in `## Right now` exits 1 and names the archive
  file it was moved to.

### `inflight wait <match> "<who or what>" | --clear [--dry-run]`

Records who or what one entry is waiting on, as a `waiting on: <text>` line
(one per entry; a new one replaces the old). Every session's brief shows it
after the headline: `(waiting on vendor support)`. Nothing polls it or clears
it automatically; whoever unblocks the work runs `--clear`. `<match>` works as
for `done`. Text that could forge an entry exits 4; credential-like text
exits 5.

### `inflight show <match>`

Prints one entry in full, from the tracker or the archive, after a line
naming the file it is in (`[tracker]` or `[inflight-<stamp>.md]`).
`<match>` is an entry id, or words from the entry's head or body. When an
entry appears in several files, the newest copy is shown. Exit 1 when nothing
matches, 2 when several entries match (they are listed).

### `inflight log [query] [-n N]`

Lists entries from the tracker and the archive, newest first, one line each:
id, file, state (`active`, `paused`, `done`, `archived`), head. With a query,
only entries whose id it is or whose text contains it (case-insensitive).
`-n` caps the lines (default 30; `0` for all). Exit 1 when nothing matches.

### `inflight sessions [--json] [--children] [--active-min N]`

Lists every tagged entry, grouped by its session. For each session it shows:

- the status;
- the harness;
- the time of the last activity;
- the end reason, for an ENDED session;
- progress, from the entry's checkbox lines;
- drill and resume commands, when the backend provides them.

| Status | Meaning | What to do |
|---|---|---|
| ACTIVE | Last activity less than `--active-min` minutes ago (default 15). | A live session owns this work. Don't edit the same files; coordinate through the tracker. |
| IDLE | Not ended, but quiet for longer than that. | Probably abandoned. Verify on disk before you take it over. |
| ENDED | The session ended. The end reason is shown. | Orphaned. Verify on disk, then take it over. |
| UNKNOWN | No backend knows this id (a typo, a pruned session, or another machine). | Treat it as unowned. |
| NO-BACKEND | No backend is available on this machine. | Tags and dates are still listed; treat the entries as unowned. |

- `--children` also lists the delegated subagent sessions of each entry's
  conversation. They are read from the backend and never stored in the file.
- `--json` emits the same data, including which backend answered for each
  session.

### `inflight trim [--apply] [--dry-run]`

Keeps the brief and the file bounded. Each run takes these steps, in order:

1. **Pause.** An active entry untouched for `INFLIGHT_STALE_DAYS` gets the
   line `status: paused (stale since <date>)`. "Touched" means the later of
   the entry's head date and its session's last activity.
2. **Archive done entries.** Done entries older than `INFLIGHT_DONE_GRACE_DAYS`
   move to the archive. So do entries paused for more than
   `INFLIGHT_PAUSED_DAYS` whose session has not been active since
   (`--paused-days`; 0 turns this off). `inflight log` and `inflight show`
   still find them.
3. **Archive old sections.** Other `## ` sections move to the archive unless
   their header (or their first 400 characters) carries a date within
   `INFLIGHT_DAYS`. A second `## Right now` section is a stale copy, and it is
   archived whole.
4. **Enforce the budgets.** While the headlines of all open entries take more
   than 5,000 bytes (so a session that owns nothing would not see every entry
   in its brief), or the file is over `INFLIGHT_MAX_BYTES`, or `## Right now`
   is over `INFLIGHT_MAX_LINES`, entries move to the archive in this order:
   1. done entries, oldest first;
   2. paused entries, longest-paused first;
   3. active entries, oldest first.

   The newest `INFLIGHT_MIN_ENTRIES` entries that aren't done are never
   archived for budget.

The dry run (the default) lists every pause and archive. `--apply` writes the
changes; `--dry-run` is accepted and wins over `--apply`. A second `--apply`
over its own output changes nothing. Flags override the
environment: `--days`, `--stale-days`, `--done-grace-days`, `--max-bytes`,
`--max-lines`, `--min-entries`.

### `inflight check [--max-bytes N]`

Lints the tracker and exits 1 if it finds anything. It checks for:

- a missing `## Right now` section, or a duplicate one;
- headlines of open entries over the brief's 5,000-byte budget;
- a file over the byte budget;
- undated bold lines above the first entry;
- unexpanded `$VAR` tags, such as `[session $HERMES_SESSION_ID]`;
- text that looks like a credential (reported by entry id, never by value);
- a tracker readable by other users.

It also prints:

- a `warn` line when it runs under a different Python than the private venv,
  because plugins installed in that venv are invisible from there;
- a `note` line when `audit.ignore_branches` or `audit.ignore_repos` is set.

### `inflight audit [--apply] [--catch-up] [--json] [--stale-min N]`

Finds owed git work and attributes it to sessions.

**Which repos.** Two sources are scanned:

- repos that hooks recorded for each session;
- the scan roots in `inflight-state/config.json` (`audit.roots`, default
  `[{"path": "~/code", "depth": 3}]`).

`--catch-up` scans only recorded repos. That is the mode `session-start` and
the Hermes cron job use.

**What counts as owed.**

| Finding | Rule |
|---|---|
| Uncommitted | `git status` shows changes. Submodules are skipped; they are audited as their own repos. |
| Unpushed | Commits on no remote-tracking ref (`rev-list <branch> --not --remotes`). A branch with no upstream is flagged `(no upstream)`. |
| Stash | Any stash entry. |
| Already on the default branch | Either every unpushed commit has a patch-id twin on the remote default branch (a rebase or cherry-pick), or the branch was squash-merged: one commit on the default branch since the merge base makes exactly the branch's net change (same paths, same resulting file contents). This is listed in its own section and is not owed. A squash edited while landing, or a branch with work added after the squash, stays unpushed. The squash search looks at no more than 500 default-branch commits. A failed or timed-out check keeps the branch unpushed. |
| Note | A branch that is ahead of a stale upstream ref, with its commits already on another remote. This is a note, not owed work. |

**Who owns it.** Each finding goes to the session that made it, never to
whoever recorded the repo last:

| Finding | Owner |
|---|---|
| Dirty file | The latest session that recorded writing it (the Hermes plugin records the files `write_file` and `patch` write). If no session recorded it: the one session that doesn't record writes (Claude Code: `tool_input` is never read) whose start to last activity spans the file's mtime. Hermes sessions never own a file by time alone. |
| Unpushed branch | The one recorder of the repo whose start to last activity spans the branch tip's commit time. |
| Stash entry | The same, by the stash entry's time. |

No candidate, or more than one, makes the finding **unattributed**: listed in
its own section, never written to the tracker.

- A recorded write counts only if it is no older than the file's last change
  minus 120 s; a stale write proves nothing about a newer edit. Failed write
  calls are not recorded.
- "Spans" means start to last activity plus 120 s. A Hermes session that
  spans the time is a rival (it may have edited through a shell, which is
  never parsed), so the finding is unattributed, not given to Claude Code.
- An untracked directory's time is the newest file inside it. A deleted file,
  or a directory with more than 5000 files, has no time and is unattributed.
- While a repo holds unattributed work, open audit entries for it stay open.

- A session that owes work in a repo and is ENDED, or idle past `--stale-min`
  (default 120 minutes, env `INFLIGHT_STALE_MIN`), gets one entry per repo,
  listing only its own findings. The entry is tagged with that session, with
  the head `audit: <repo>: owed work`, and says whether the session ended or
  since when it has been idle. Re-runs update it in place.
- Repos are compared by their canonical path: symlinks resolved and, on
  macOS, the on-disk case, so `~/Code/x` and `~/code/x` are one repo.
- When the session owes nothing in the repo any more (clean, or all of the
  work is attributed to other sessions), the entry is marked done, never
  deleted.
- Repos that an ACTIVE session recorded get no new entry. A repo that already
  has an open audit entry is re-checked on every run anyway, so the entry
  never goes on claiming work that has since been committed or pushed: it is
  closed when everything it lists is gone, and trimmed to what is left
  otherwise. Work still in the repo that can no longer be pinned on the
  entry's session (another session worked there since) stays listed, marked
  `(still in the repo; maker not provable)`. Only the `- [ ]` lines change;
  the head, `waiting on:`, `(took over ...)` and notes are kept.
- Scan-root repos that no session recorded are listed as **unowned** and never
  written to the tracker.

**Safety.** No network calls are made: refs are as of the last fetch. Every
git call goes through `safe_git()` (see [Guarantees](#guarantees)).

**Optional.** `audit.ignore_branches` takes a list of globs to hide matching
branches. `audit.ignore_repos` takes a list of path globs (`~` expands;
case-insensitive on macOS) for repos that are dirty on purpose, such as a
plugin checkout carrying live patches: they are never inspected, and open
audit entries for them close. Both are off by default; `inflight check`
prints them when set, and the audit lists the ignored repos.

`--dry-run` is accepted and wins over `--apply`.

### `inflight adapter install|uninstall|status claude-code [--apply]`

Merges the hooks into the user-level `~/.claude/settings.json` (`--settings`
selects another file). The dry run prints the exact diff. `--apply` writes a
backup first, then merges, and never changes existing hooks.
`--allow-reformat` accepts rewriting a file that isn't in the standard
2-space JSON layout. Full behavior:
[adapters/claude-code](../adapters/claude-code/README.md).

### `inflight hook <event> [--harness <name>]`

Hook protocol v1: one JSON object on stdin, and the exit status is always 0.
See [hook-protocol.md](hook-protocol.md).

### `inflight plugin list|enable|disable <name>`

Manages the allowlist for [backend plugins](extending.md#2-backend-plugin-secondary).
Nothing is imported until a plugin is enabled. Every change is logged to
`hooks.log`.

### `inflight init`, `path`, `me`, `--version`

| Command | Does |
|---|---|
| `init` | Creates the tracker from the template if it's missing. Never overwrites. |
| `path` | Prints the resolved tracker path. |
| `me` | Prints this session's `[session <id>]` tag. |
| `--version` | Prints the version. |

## Exit codes

| Code | Meaning | Commands |
|---|---|---|
| 0 | Success, or nothing to do. | all |
| 1 | `check` found problems; `done`, `wait`, `show` or `log` matched no entry; `plugin enable` named a plugin that isn't installed. | `check`, `done`, `wait`, `show`, `log`, `plugin` |
| 2 | `done`, `wait` or `show` matched more than one entry (the candidates are listed); the tracker is missing; the settings file is unreadable or in a non-standard layout; the command is unknown. | `done`, `sessions`, `adapter`, `inflight` |
| 3 | Another writer held the lock or changed the file; re-run. For `adapter install`, a managed setting blocks user hooks. | `add`, `done`, `wait`, `trim`, `audit`, `adapter` |
| 4 | `add` or `wait` refused text that would forge or corrupt an entry. | `add`, `wait` |
| 5 | `add` or `wait` refused text that looks like a credential. | `add`, `wait` |

`inflight hook` always exits 0.

## Entry format

```markdown
**2026-09-30 19:10 [session S #a1b2c3] — rollout: step 2 of 3.** What is not done, the next step, the handle.
status: paused (stale since 2026-10-03)
waiting on: the owner's pick between A and C
- [x] step one
- [ ] step two
- [~] push (blocked: waiting on review)
```

- **Boundaries.** An entry starts at a line beginning `**YYYY-MM-DD` and runs
  until the next such line. Bold text inside an entry (`**Note:** ...`) stays
  part of it. Bold lines before the first dated entry are not entries;
  `check` reports them.
- **Head.** Date, tag, then `— <thing>: <state>.` Keep it to one line.
- **Status line.** Optional: `status: done <date>` or
  `status: paused (stale since <date>)`. No status line means the entry is
  active. `done` and `trim` write this line.
- **Waiting line.** Optional: `waiting on: <who or what>`, written by
  `inflight wait`. The brief shows it after the headline. It is a note for
  other sessions, not a gate: nothing polls it.
- **Progress.** Checkbox lines are counted on every read and never stored:
  `- [ ]` is open, `- [x]` is done, `- [~]` is blocked. The example shows as
  `progress 1/3 (1 blocked)` in `sessions`. Head lines and fenced code blocks
  are never counted.
- **Taking over.** Edit the entry in place and append `(took over <old-id>)`.
  Don't add a duplicate entry.
- **Other sections.** Any other `## ` section survives `trim` only while its
  header carries a date newer than `INFLIGHT_DAYS`.

To compare entry counts under the old blank-line rule and the current one, run
`scripts/parser-parity.py FILE...`.

## Configuration

All settings are optional and read from the environment.

| Variable | Default | Meaning |
|---|---|---|
| `INFLIGHT_HOME` | `$HERMES_HOME`; else `~/.hermes` if it exists; else `~/.agent-inflight` | Directory for the tracker, `inflight-archive/` and `inflight-state/` |
| `INFLIGHT_FILE` | `<home>/inflight.md` | Tracker path |
| `INFLIGHT_SESSION_ID` | `$HERMES_SESSION_ID`, then `$CLAUDE_CODE_SESSION_ID` | Session id written into the tag |
| `INFLIGHT_MAX_BYTES` | `64000` | Soft byte cap for the whole file. Sessions read the brief, not the file |
| `INFLIGHT_MAX_LINES` | `200` | Line cap for `## Right now` |
| `INFLIGHT_MIN_ENTRIES` | `3` | Newest entries always kept, whatever the budget |
| `INFLIGHT_STALE_DAYS` | `3` | Days untouched before an active entry is paused |
| `INFLIGHT_DONE_GRACE_DAYS` | `1` | Days before a done entry is archived |
| `INFLIGHT_PAUSED_DAYS` | `7` | Days paused (owner inactive) before an entry is archived; `0` = never |
| `INFLIGHT_DAYS` | `7` | Age at which dated non-`Right now` sections are archived |
| `INFLIGHT_STALE_MIN` | `120` | `audit`: minutes idle before a session's owed work is caught up |
| `INFLIGHT_REINJECT` | `1` | Hermes plugin: `0` turns off delivery of the brief |
| `INFLIGHT_BIN_DIR` | `~/.local/bin` | `install.sh`: where to link the `inflight` command |

Settings that live in `inflight-state/config.json`: `audit.roots`,
`audit.ignore_branches` and `audit.ignore_repos` (see [`audit`](#inflight-audit---apply---catch-up---json---stale-min-n)).

## Session status backends

`inflight sessions` asks backends in order. The first one that knows the id
answers.

1. **Plugins** that you enabled with `inflight plugin enable <name>`, in
   allowlist order. A plugin is a Python package with an
   `agent_inflight.backends` entry point, installed into
   `<home>/inflight-state/venv`. Plugins load only in `sessions` and `trim`,
   never in `add`, `check` or hooks. The contract is
   `src/agent_inflight/backends.py` (`PLUGIN_API_VERSION = 1`). How to write
   one: [extending.md](extending.md).
2. **Hermes** reads `state.db` read-only across profiles and follows
   context-compression children, so a tag on a compacted session resolves to
   the session that continues the work.
3. **Heartbeat** reads the state files that `inflight hook` writes. It works
   for any harness that runs the hooks.

Without a backend, every tagged entry is still listed with status
`NO-BACKEND`. The tags and dates alone are enough to hand work between
sessions.

## Files

| Path (under home) | Mode | Content |
|---|---|---|
| `inflight.md` | 0600 | The tracker |
| `inflight.md.lock` | 0600 | `flock` target for every CLI writer |
| `inflight-archive/` | 0700 | Trimmed text, `inflight-<stamp>.md`, plus `hooks.log`. `show` and `log` read it; `add` never reuses an id found there. |
| `inflight-archive/hooks.log` | 0600 | One JSON line per hook action, plugin change or `--force`. Only whitelisted keys are written. |
| `inflight-state/` | 0700 | Everything below |
| `inflight-state/venv/` | | Private stdlib venv |
| `inflight-state/inflight` | | Launcher pinned to that venv; hooks call this |
| `inflight-state/sessions/<id>.json` | 0600 | Per-session hook state: heartbeat, repos touched, files written (Hermes), end time and reason, harness |
| `inflight-state/config.json` | 0600 | `audit.*` settings and the plugin allowlist |
| `inflight-state/last-catch-up` | 0600 | Time of the last `session-start` catch-up (rate limit) |

`~/.local/bin/inflight` is a link to the launcher.

## Guarantees

A bug that breaks one of these is in scope for [SECURITY.md](../SECURITY.md).

- **Nothing is deleted.** Trimmed text goes to
  `inflight-archive/inflight-<stamp>.md`.
- **Unfinished work is not archived for age.** Age only pauses an entry. An
  entry is archived only after `done`, or under byte-budget pressure after
  every done and paused entry.
- **Idempotent.** `trim --apply` over its own output writes nothing and
  creates no archive.
- **Race-safe writes.** Every writer (`add`, `done`, `trim`, `audit`, the
  Hermes plugin) holds an `flock` on `inflight.md.lock` and writes
  atomically. A plain editor save takes no lock. When an editor save races
  `add`, `add` retries; `done` and `trim` refuse with exit 3.
- **Private files.** Tracker and archive files are `0600`, and the archive
  directory is `0700`. `check` flags a tracker that others can read.
- **No forged entries.** `add` refuses (exit 4) a head that contains a
  newline or a session or entry tag. It also refuses a body line that starts
  a dated entry or a `## ` section. So one call can't write an entry tagged
  as another session.
- **No secrets.** `add` refuses (exit 5) text matching common credential
  shapes: API keys, GitHub, Slack and AWS tokens, private-key blocks, JWTs,
  and URLs with passwords. It names only the kind of match, never the value.
  `--force` accepts a false positive. `check` reports matching entries by id.
- **Hooks fail open.** `inflight hook` always exits 0 and never returns a
  permission decision. Tool arguments are never read, stored or logged.
- **Read-only session lookups.** `state.db` is opened with `mode=ro`.
- **Plugins are opt-in.** A backend plugin is never imported until it's on
  the allowlist.
- **Audit can't run repo code.** Every git call goes through `safe_git()`:
  - only read-only subcommands, with a 5 s timeout;
  - `core.fsmonitor=false` and `core.hooksPath=/dev/null`;
  - every filter driver the repo defines (in `.git/config` or through
    `include.path`) blanked on the command line, because a repo-local clean
    filter runs even during a plain `git status`.

  `status` never recurses into submodules (`--ignore-submodules=all`): git
  would run there with the submodule's own config, which these flags don't
  reach. Submodules are audited as their own repos.

  A hostile-repo fixture (fsmonitor, clean and process filters,
  `include.path`, hooks, a submodule with its own filter) proves that no
  marker file is written. A control arm proves that plain git would write
  them.
- **Catch-up never claims work.** Audit entries carry the dead session's tag.
  Taking one over (`(took over <id>)`) is always an explicit act.
