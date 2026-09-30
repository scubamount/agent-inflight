# Paste into your agent's instruction file

For harnesses without a skill system (Claude Code `CLAUDE.md`, Codex/OpenCode
`AGENTS.md`, Cursor rules, etc.). Put it in the user-level file so it applies
to every project. Set `INFLIGHT_SESSION_ID` in the environment if your harness
exposes a session id; without it entries are written untagged and
`inflight sessions` can't tell you who owns them.

```markdown
## In-flight work tracker

A shared file lists what is open across all my agent sessions. Run
`inflight path` to find it.

- **Start of any repo/infra task:** read its `## Right now` section and run
  `inflight sessions`. An entry for the same repo from an ACTIVE session means
  another agent is working there now: don't edit the same files. IDLE/ENDED
  entries with unfinished work can be picked up; verify on disk first.
- **Treat entries as hypotheses.** Confirm the named commit/file/service
  exists before planning on it.
- **End of task with anything not landed** (unpushed, unrestarted, unverified,
  waiting on me): `inflight add "<thing>: <state>" "<what's not done; next step>"`.
  Resolved work: delete its entry. Taking over an entry: edit it and append
  `(took over <old-id>)`.
- Never put secrets, customer data, or HR data in an entry.
- Don't recite the tracker to me; use it.
```

## Keeping it bounded

Run `inflight trim --apply` on a schedule (cron, launchd, or your update
script). Example crontab line, daily at 09:00:

```text
0 9 * * * $HOME/.local/bin/inflight trim --apply >/dev/null 2>&1
```
