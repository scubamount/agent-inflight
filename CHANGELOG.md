# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/).

## 1.1.0

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
