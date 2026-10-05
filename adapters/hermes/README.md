# Hermes adapter

`install.sh` does the Hermes wiring when it finds a Hermes home
(`$HERMES_HOME` or `~/.hermes`):

- tracker file: `<hermes-home>/inflight.md` (no config needed)
- skill: `<hermes-home>/skills/agent/inflight-tracker` (listed in
  `<available_skills>`, loaded on trigger)
- session tags: `HERMES_SESSION_ID` is in every Hermes terminal's
  environment, so `inflight add` tags entries automatically
- plugin: `<hermes-home>/plugins/agent-inflight` -> `adapters/hermes/plugin`
  (symlink). Hermes loads it once `plugins.enabled` lists `agent-inflight`.
  When a tool call hand-edits the tracker and writes a literal
  `[session $HERMES_SESSION_ID]`, the plugin replaces it with the session id
  the runtime passed to the hook, and appends any NEW `inflight check`
  findings to that tool's result. It only claims entries the call's own text
  contains (a concurrent session's entry is reported, not claimed). Hooks:
  `pre_tool_call` (observe only, never blocks) + `transform_tool_result`
  (the only hook whose return reaches the model). No tool calls, no
  subprocesses; any error leaves the result unchanged.
  Also `pre_llm_call`: on a session's first turn, the first turn after a
  context compaction (hermes-lcm in-place summaries or the built-in
  compressor), and the first turn the process sees of a resumed or restarted
  session (skipped when its history already holds a brief since the last
  compaction), it injects the
  brief into the user turn: this session's open entries in full (tags in its
  compression lineage; never a delegation parent's), one line per other open
  entry, at most 6 KB (see `inflight brief`). Subagents and cron runs get
  nothing. `INFLIGHT_REINJECT=0` turns it off.
- status backend: `inflight sessions` reads `<hermes-home>/state.db` and
  `profiles/*/state.db` read-only, and follows compression children

## Make the agent read it every session

The skill loads on trigger. To make the start/end steps unconditional, add
two lines to `<hermes-home>/SOUL.md` (or the profile's SOUL.md):

```markdown
- **Session start** (repo/infra work): read the `[inflight brief]` block the plugin injects (no block: run `inflight brief`); run `inflight sessions`. ACTIVE sibling on the same files = coordinate, don't mutate. Read the full file only when an entry's detail matters.
- **Session close:** `inflight add "<thing>: <state>" "<not done; next step>"` for anything not landed; `inflight done <id>` for resolved ones.
```

## Keep it trimmed

Pick one:

- **Hermes cron** (`./install.sh --hermes-cron` copies the job script to
  `<hermes-home>/scripts/inflight-trim.sh`), then register it once:
  `hermes cron create "0 9 * * *" --name inflight-trim --script inflight-trim.sh --no-agent --deliver local`
  Silent on success; prints the error on failure.
- **Your update script:** call `inflight trim --apply` after updating.
- **crontab:** see `adapters/generic/AGENTS-snippet.md`.

## Updating

`git -C <checkout> pull --ff-only && <checkout>/install.sh`. The CLI is a
symlink into the checkout, so the pull alone updates it; re-running
`install.sh` refreshes the skill copy.
