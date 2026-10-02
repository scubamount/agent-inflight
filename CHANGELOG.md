# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/).

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
