# AGENTS.md

Instructions for coding agents working **on this repository**. To *use*
agent-inflight in your own sessions, see the
[instruction snippet](adapters/generic/AGENTS-snippet.md) instead.

## What this repo is

A stdlib-only Python CLI (`inflight`) plus harness adapters that maintain a
shared Markdown tracker of in-flight agent work. It is not a pip package:
`install.sh` creates a private venv and runs `src/` directly.

## Layout

| Path | Contents |
|---|---|
| `bin/inflight` | Entry script; adds `src/` to the path and calls `agent_inflight.cli.main` |
| `src/agent_inflight/cli.py` | Command dispatch. `hook`, `plugin`, `audit` and `adapter` import lazily. |
| `src/agent_inflight/core.py`, `entries.py`, `progress.py` | Parser, `add`, `done`, `check`, and entry state |
| `src/agent_inflight/trim.py` | Budget, pause and archive policy (its docstring is the spec) |
| `src/agent_inflight/sessions.py`, `backends.py`, `plugins.py` | Status lookup, built-in backends, plugin allowlist |
| `src/agent_inflight/hook.py`, `state.py` | Hook protocol v1, per-session state |
| `src/agent_inflight/brief.py`, `reinject.py` | The session brief, and when Hermes delivers it |
| `src/agent_inflight/audit.py`, `safegit.py` | Owed-work audit; the only way git is run |
| `src/agent_inflight/safety.py` | Locks, atomic private writes, forge and credential checks |
| `src/agent_inflight/adapter_claude.py` | Claude Code settings merge |
| `adapters/hermes/plugin/` | Hermes plugin (in-process; no subprocesses, no tool calls) |
| `skills/agent/inflight-tracker/SKILL.md` | The agent-facing skill |
| `examples/backend-example/` | Worked backend plugin; CI installs it |
| `tests/` | One file per area; `tests/e2e/` is manual only |
| `docs/` | `reference.md` is the behavior spec for users |

## Commands

```bash
make check PY=python3      # all tests + syntax checks; must exit 0
make lint PY=python3       # ruff + mypy (install: CONTRIBUTING.md)
python3 tests/test_audit.py   # one area
```

- Never pipe `make` targets: a pipe reports the last command's exit code.
- `tests/test_extending.py` runs the shell blocks in `docs/extending.md`. It
  needs network access once, for `pip` to fetch `setuptools`.

## Rules

1. **Stdlib only, Python 3.9+.** No runtime dependencies, no network calls,
   no daemon. Code must run on 3.9 (CI tests 3.9 to 3.13), so use the
   `typing` generics (`List`, `Optional`), not `list[str]` or `X | None` at
   runtime.
2. **Hooks fail open.** `inflight hook` always exits 0, never blocks a tool,
   and never reads `tool_input`.
3. **Run git only through `safe_git()`** in `safegit.py`. Never use plain
   `subprocess` git against a repo the tool did not create.
4. **Write the tracker only through `safety.locked()` and `write_private()`**.
   Keep the file stat check that refuses with exit 3.
5. **Never weaken a guarantee to pass a test.** The guarantees are in
   `docs/reference.md#guarantees`; breaking one is a security bug.
6. **No real data** in tests, fixtures or examples: no credentials, customer
   or personnel data, and no real tracker contents.
7. **Docs ship with behavior.** When behavior changes, update
   `docs/reference.md`, the README if user-visible, the skill, and
   `docs/hook-protocol.md` for hook changes, in the same commit.
8. **Version bumps** change `VERSION`, `src/agent_inflight/__init__.py` and the
   skill's `version:` together.
9. **Every fix comes with a test** that fails without it.

## Done when

- `make check` and `make lint` exit 0.
- Relative links in every Markdown file resolve
  (`tests/test_extending.py::DocPointers` checks this).
- No tracker file (`inflight.md`) or archive is staged; `.gitignore` covers
  both.
