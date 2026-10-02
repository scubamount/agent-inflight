# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/).

## 1.2.0

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
