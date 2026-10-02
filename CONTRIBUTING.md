# Contributing

Thanks for helping. agent-inflight is small on purpose; changes that keep it
small, stdlib-only and offline are the easiest to accept.

Coding agents: read [AGENTS.md](AGENTS.md) as well. It has the same rules in
a checklist form.

## Ground rules

- **Stdlib only, Python 3.9+.** No runtime dependencies, no network calls,
  no daemon. `install.sh` must stay offline.
- **Hooks fail open.** `inflight hook` always exits 0 and never blocks a tool.
- **Git calls go through `safe_git()`.** Never run plain `git` against a repo
  the tool did not create.
- **Never weaken a guarantee to pass a test.** If a test fails, fix the code
  or explain in the PR why the test was wrong.
- **No real data in tests, fixtures, examples or issues.** No credentials,
  customer data, personnel data or real tracker contents. Use placeholders.

## Develop

```bash
make check PY=python3          # tests + shell/py syntax checks
python3 -m pip install --require-hashes --no-deps -r requirements-lint.txt
make lint PY=python3           # ruff + mypy, same as CI
```

CI runs `make check` on Linux and macOS for Python 3.9 to 3.13, plus
`make lint`. Lint tool versions are pinned with hashes in
`requirements-lint.txt`; regenerate it from `requirements-lint.in` with the
command in that file.

## Pull requests

- One change per PR, with a test that fails without it.
- Update the README, `docs/reference.md`, the other `docs/` and the skill when
  behavior changes, in the same PR.
- Bump `VERSION`, `src/agent_inflight/__init__.py` and the skill's `version:`
  together.
- Security issues: see [SECURITY.md](SECURITY.md), not a public PR.

## License

By contributing you agree that your contribution is licensed under the
Apache License 2.0 (see [LICENSE](LICENSE)), as described in section 5 of
that license.
