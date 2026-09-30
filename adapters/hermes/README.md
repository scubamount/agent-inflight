# Hermes adapter

`install.sh` does the Hermes wiring when it finds a Hermes home
(`$HERMES_HOME` or `~/.hermes`):

- tracker file: `<hermes-home>/inflight.md` (no config needed)
- skill: `<hermes-home>/skills/agent/inflight-tracker` (listed in
  `<available_skills>`, loaded on trigger)
- session tags: `HERMES_SESSION_ID` is in every Hermes terminal's
  environment, so `inflight add` tags entries automatically
- status backend: `inflight sessions` reads `<hermes-home>/state.db` and
  `profiles/*/state.db` read-only, and follows compression children

## Make the agent read it every session

The skill loads on trigger. To make the start/end steps unconditional, add
two lines to `<hermes-home>/SOUL.md` (or the profile's SOUL.md):

```markdown
- **Session start** (repo/infra work): read `inflight.md` § Right now; run `inflight sessions`. ACTIVE sibling on the same files = coordinate, don't mutate.
- **Session close:** `inflight add "<thing>: <state>" "<not done; next step>"` for anything not landed; delete resolved entries.
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
