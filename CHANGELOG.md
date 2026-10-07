# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/).

## 1.6.0

### Fixed

- The brief no longer leaves out open entries. When the one-line list did
  not fit, the tail was cut to `(N more; ...)`: on the audited machine 11 of
  38 open entries were not in any session's brief. Now the owner with the
  most open entries folds first, to its newest headline plus
  `also open (N): #id ...`, and as a last resort entries are named by id.
  Every open entry is named in every brief, still within 6,000 bytes.
- Audit entries no longer go stale while their repo is in use. A repo with
  an ACTIVE recorder used to be skipped, so its open audit entry kept
  claiming work that had been committed or pushed (one listed 16 stashes
  when 1 was left). Open entries are now re-checked on every run: closed when
  their work is gone, trimmed when part of it is. Work that can no longer be
  pinned on the entry's session stays listed and marked; hand-added lines
  are kept. The re-check never adds an entry for a repo in use.
- Collision warnings: no warning about a Hermes session that wrote no file
  in the repo (it only read there), and the Hermes plugin warns only on tools
  that can change files. Replaying the 126 warnings in this machine's
  state.db through the new rules leaves 4.


Fixes from a usage audit of one machine (5 days, 17 sessions).

### Fixed

- `inflight audit` no longer reports squash-merged branches as unpushed
  work. A branch counts as already on the default branch when one commit
  there, since the merge base, makes exactly the branch's net change. On the
  audited machine every catch-up finding for one repo (9 branches) was a
  squash-merged PR.
- Collision warnings skip a Hermes session's own family (its subagents, its
  parent, siblings under the same root). 76% of the warnings on the audited
  machine (84 of 111) were a session being warned about its own subagents.

### Added

- `inflight add --supersedes <id>`: closes this session's open entry `<id>`
  in the same write, with a `superseded by` line. One session had 16 open
  entries, 13 of them earlier states of the same work, which pushed the
  brief over its budget.
- `inflight trim` archives entries paused longer than `INFLIGHT_PAUSED_DAYS`
  (default 7, `--paused-days`, 0 = never) whose session has not been active
  since. Before, paused entries were archived only under budget pressure.


### Added

- `audit.ignore_repos` in `config.json`: path globs for repos that are dirty
  on purpose (a plugin checkout carrying live patches). They are never
  inspected, open audit entries for them close, and `inflight check` and the
  audit list them. `--json` gains `ignored`.

### Removed

- `inflight sessions --me`: use `inflight me`.

## 1.3.0

### Changed

- `inflight audit` attributes each finding to the session that made it, not
  to whichever session recorded the repo last. A dirty file goes to the
  session that recorded writing it (the Hermes plugin now records the files
  `write_file` and `patch` write), else to the one Claude Code session whose
  active hours span the file's mtime. An unpushed branch or stash entry goes
  to the one recorder whose active hours span its time. Anything else is
  **unattributed**: listed, never written. Catch-up entries list only their
  session's own work, one per (session, repo).
- An entry closes when its session owes nothing in the repo any more, not
  only when the repo is clean. While the repo holds unattributed work, its
  open entries stay open.
- `--json`: `pending` lists repos not inspected because every recorder is
  idle under the stale limit (they were in `waiting` before).
- The "older local branches" section is gone: a branch no session made is
  unattributed.
- `--json`: new `unattributed`; `local_branches` removed.

## 1.2.3

### Fixed

- `inflight trim --dry-run` is accepted (it was rejected as an unknown
  argument). The dry run stays the default, and `--dry-run` wins over
  `--apply`, as in `audit`.
- Docs: the Hermes delivery table and adapter README now say a restart gets
  no new brief when the history already holds one (true since 1.2.1).

### Changed (no change in output)

- The 15-minute active window has one source, `state.ACTIVE_MIN`. Session
  status, the brief, `audit` and the collision warning all read it.
- Entry-id parsing (`core.normalize_id`) and the plain-text head
  (`Entry.label`) each have one implementation instead of two and six.
- Removed an unused constant. AGENTS.md lists every module.
- Tests: raw `git` calls in the audit tests close stdin, as `safe_git`
  does. A test run with an open stdin pipe (a pipeline, some agent shells)
  hung on the hostile fixture's filters and left orphaned processes.

## 1.2.2

### Fixed

- Compaction markers and briefs inside tool results no longer count. A grep
  or log read that printed `[CONTEXT COMPACTION` (common in sessions that
  debug Hermes itself) made the plugin think a compaction had happened after
  the last brief, so 1.2.1 still re-sent the brief after a restart.

## 1.2.1

Fixes found by watching a long-running Hermes session through a day of
update cycles.

### Fixed

- **One repo, two spellings.** On macOS `~/Code/x` and `~/code/x` are the
  same directory, but repos were recorded as written, so two sessions in one
  repo were not warned about each other and `audit` wrote two entries for
  it. Repos are now recorded and compared by canonical path (symlinks
  resolved, on-disk case). Entries written under the old spelling are matched
  by their `repo:` line; when a newer session owns the repo, the older entry
  is marked done.
- **Brief re-sent after every restart.** A Hermes restart forgot which
  sessions had their brief, so each restart sent the whole brief again,
  labelled `compaction`. The plugin now checks the history: a brief after the
  last compaction marker means the session still has it. Otherwise it is
  sent, labelled `resume`.
- **"After this session ended" on sessions that hadn't.** Audit entries now
  say `session ended <time>` or `session idle since <time>`.
- **Old local branches counted as owed work.** A branch with no upstream
  whose last commit predates the owning session is listed on its own line,
  not as an open checkbox.


Read the history back, and say what an entry is waiting on.

`trim` never deletes, but nothing read the archive: `inflight done` on an
archived id answered "no entry matches", as if the entry never existed. On
the author's machine the archive held 25 files and 88 entries that no command
could reach.

### Added

- `inflight show <id|words>` prints one entry in full, open or archived, and
  names the file it is in. The newest copy wins when an entry was archived
  more than once.
- `inflight log [query] [-n N]` lists the tracker and the archive as one
  history, newest first, and searches it.
- `inflight wait <id> "<who or what>"` writes an optional `waiting on:` line.
  Every session's brief shows it after the headline; `--clear` removes it.
  It is a note, not a gate: nothing polls it.

### Changed

- `done` and `wait` on an entry that has been archived say which archive
  file holds it.
- `add` never gives a new entry an id that an archived entry already has.


Sessions get a brief instead of the whole tracker.

Before, every session read the whole tracker at start, and the file kept
outgrowing its 24 KB budget: the tracker was over budget 28 times in one day
of real use. Now each session gets the **brief**: its own open entries in full,
then one line per other open entry, at most 6 KB. With 22 open entries, this
cut the start-of-session read from 24 KB to 3.6 KB.

### Added

- `inflight brief` prints the brief for this session.
- The Hermes plugin injects the brief on a session's first turn. It already
  re-injected after a compaction or resume, and those deliveries now carry
  the brief too. Subagents and cron runs get none.
- Hook protocol `session-start` prints the brief for every source
  (`startup`, `new`, `clear`, `compact`, `resume`), and prints nothing when
  no entry is open. Claude Code receives it as `additionalContext`.
- `inflight check` reports when the headlines of open entries go over the
  brief's 5,000-byte budget.

### Changed

- `inflight trim` keeps all open entries' headlines inside the brief's
  budget, so a session that owns nothing still sees every entry.
- The file budget is now a soft cap: `INFLIGHT_MAX_BYTES` defaults to 64,000
  (was 24,000) and `INFLIGHT_MAX_LINES` to 200 (was 80).
- The re-inject block is replaced by the brief. Its header changed from
  `[inflight: your open entries, re-read after …]` to `[inflight brief: …]`.
- The skill, the instruction snippet, the Hermes SOUL.md lines and the
  template tell agents to read the brief and open the full file only when an
  entry's detail matters.

## 1.0.1

Documentation only. The README is now a landing page, with the full detail
moved to `docs/reference.md`. Adds `AGENTS.md` for coding agents that
contribute to the repo. The instruction snippet and template close entries
with `inflight done`.

## 1.0.0

First public release.
