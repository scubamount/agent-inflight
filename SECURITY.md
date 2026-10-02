# Security policy

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
<https://github.com/scubamount/agent-inflight/security/advisories/new>

Do not open a public issue, pull request or discussion for a suspected
vulnerability. Include the version (`inflight --version`), your OS and Python
version, and the smallest reproduction you have. Never include real
credentials, tracker contents or session transcripts; use placeholders.

You should get an acknowledgement within 7 days. Fixes ship on `main` and in
the next tagged release (`git tag`); the advisory is published after a fix is
available.

## Supported versions

Only the latest tagged release and `main` get security fixes.

## Scope

In scope, because the tool promises them (see [Guarantees](docs/reference.md#guarantees)):

- Code execution from an audited repository (`safe_git()` bypass: hooks,
  fsmonitor, filter drivers, `include.path`, submodules).
- Writing an entry tagged as another session (forged entries).
- A credential reaching the tracker, `hooks.log` or a session state file
  despite the secret check, or a matched value being echoed.
- Tracker, archive or state files created readable by other users.
- A hook that blocks a tool call or exits non-zero.
- A backend plugin imported without being on the allowlist.

Out of scope: anything that needs an attacker who can already write to your
home directory, your agent's settings, or the private venv.
