# Extending agent-inflight

There are three ways in. Pick the first one that fits:

| You want to | Use | Code in this repo? |
|---|---|---|
| Make a new harness (agent CLI, IDE, gateway) tag entries, heartbeat, get collision warnings and re-injection | **Hook protocol** (§1) | None. Your harness runs a command. |
| Make `inflight sessions` know whether *your harness's* session ids are ACTIVE, IDLE or ENDED | **Backend plugin** (§2) | None. A separate Python package with an entry point. |
| Ship a one-command installer that wires §1 into a harness's config | **Adapter** (§3) | v1 has no adapter plugin API. Write your own installer, or send a PR. |

Most harnesses need only §1: once it runs the hooks, the built-in
`heartbeat` backend already reports its sessions' status. Write a backend
plugin when the harness keeps its own session record (a database, a JSON file,
an API) that is more accurate than heartbeats, e.g. it knows a session ended
even when the `session-end` hook never ran.

## 1. Hook protocol (primary)

The full contract is [hook-protocol.md](hook-protocol.md) (version 1). In
short, on each lifecycle event your harness runs:

```sh
inflight hook <event> [--harness <name>] < payload.json
```

| Your harness's event | inflight event | Minimum payload |
|---|---|---|
| session created / resumed / after compaction | `session-start` | `{"session_id", "cwd", "source": "new" \| "resume" \| "compact" \| "clear"}` |
| before a file-editing or shell tool runs | `pre-tool` | `{"session_id", "cwd", "tool"}` |
| after that tool runs | `post-tool` | `{"session_id", "cwd", "tool"}` |
| working directory changed | `cwd-changed` | `{"session_id", "cwd"}` |
| session closed | `session-end` | `{"session_id", "reason"}` |

What your harness must do with the result:

- **Never treat the exit status as a verdict.** It is always 0. inflight never
  blocks or approves a tool.
- **Pass stdout to the model** when it is non-empty. Only `session-start`
  (`resume`/`compact`: the session's own open entries) and `pre-tool` (a
  collision warning) print anything. If your harness cannot add text to the
  model's context from a pre-tool hook, drop that output; the warning is a
  convenience, not a control.
- **Export the session id** as `INFLIGHT_SESSION_ID` in the agent's shell
  environment, so `inflight add` run by the agent tags entries with it.
- **Never send tool arguments.** `args` is accepted and ignored; leave it out.

Smoke test for a new wiring (scratch tracker, nothing real touched; CI runs
this block too):

<!-- smoke:start -->
```sh
export INFLIGHT_HOME="$(mktemp -d)"
echo '{"session_id":"wire-1","cwd":"'"$PWD"'","source":"new"}' | inflight hook session-start --harness myharness
inflight add --session wire-1 "wiring test"
inflight sessions        # expect: ACTIVE     wire-1 @myharness ... (heartbeat backend)
```
<!-- smoke:end -->

Wrong field names are ignored, not rejected, so a silent no-op is the failure
mode: check `inflight sessions` shows the session, then
`$INFLIGHT_HOME/inflight-state/sessions/<id>.json` for `repos` after a
`post-tool`.

If your harness uses its own field names (as Claude Code does with
`tool_name`), either translate in your hook command or add a case to
`normalize()` in `src/agent_inflight/hook.py` by PR, keyed on `--harness`.

## 2. Backend plugin (secondary)

A backend answers one question: given a session id from a tracker tag, what
is that session's state? `inflight sessions` asks every backend in order and
the first non-`None` answer wins: **enabled plugins** (allowlist order), then
the built-in `hermes`, then `heartbeat`.

### Contract (`PLUGIN_API_VERSION = 1`)

Duck-typed; your package does not import agent-inflight. Source of truth:
`src/agent_inflight/backends.py`.

| Member | Required | Returns |
|---|---|---|
| `name: str` | yes (defaults to the entry-point name) | shown in `sessions --json` as `backend` |
| `available() -> bool` | yes | `False` = skipped for this run (e.g. your harness isn't installed) |
| `lookup(session_id) -> dict \| None` | yes | `None` = not yours, next backend is asked. Keys (all optional): `status` (`ACTIVE`/`IDLE`/`ENDED`/`UNKNOWN`; omit to derive from times), `last_activity_at`, `started_at`, `ended_at` (Unix seconds), `end_reason`, `profile`, `title`, `continued_as`. `status: "UNKNOWN"` also passes to the next backend. |
| `drill(session_id, profile) -> [str]` | no | up to two commands shown as `drill :` and `resume:` |
| `lineage(session_id) -> [ids]`, `children(session_id) -> [dict]` | no | compaction ancestry / delegated children, if your harness has them |

The entry point resolves to a class (instantiated with no arguments) or an
instance. If the module defines `PLUGIN_API_VERSION` and it isn't `1`, the
plugin is refused (logged `plugin-refused`).

### Rules a plugin must follow

- **Read-only, local, fast.** `sessions`, `trim` and `audit` call `lookup` once
  per tagged entry. No network calls, no writes to the harness's data.
- **Return only status metadata.** Whatever you return is printed and may be
  pasted into a model's context. Never return prompts, transcripts, file
  contents, credentials or user data; copy an explicit field list (the example
  does) rather than passing a harness record through.
- **Don't raise.** An exception is caught and logged as `backend-error` and the
  next backend answers, but a plugin that raises on every call is just slow
  noise.

What agent-inflight guarantees in return:

- **Nothing you ship is imported until the user runs `inflight plugin enable
  <name>`.** Installed-but-not-enabled plugins are listed by name and entry
  point string only.
- **Plugins are never imported** by `add`, `done`, `check`, `inflight hook`
  (including the audit catch-up it triggers) or the Hermes plugin. A broken
  or slow plugin can't affect a harness's tool calls.
- Every `enable`/`disable` is logged to `inflight-archive/hooks.log`; the
  allowlist is `inflight-state/config.json` (0600).

### Worked example: `examples/backend-example/`

[`examples/backend-example`](../examples/backend-example) is a complete,
installable plugin (about 60 lines) for an imaginary harness that keeps its
sessions in `~/.example-harness/sessions.json`. Its `pyproject.toml` declares
the entry point:

```toml
[project.entry-points."agent_inflight.backends"]
example = "inflight_backend_example:Backend"
```

The walkthrough below installs agent-inflight and the example into a scratch
directory and enables it. It never touches your real tracker, venv or Hermes
home: `INFLIGHT_HOME` points at the scratch dir and `--no-skill --no-plugin`
skip every Hermes step. It needs network access once, for `pip` to fetch
`setuptools` to build the example. `tests/test_extending.py` runs this exact
block in CI on every push, so if it drifts from the code, CI fails.

<!-- walkthrough:start -->
```sh
REPO="${REPO:-$HOME/agent-inflight}"      # your agent-inflight checkout
cd "$(mktemp -d)"
export INFLIGHT_HOME="$PWD/home" INFLIGHT_BIN_DIR="$PWD/bin"
export EXAMPLE_HARNESS_SESSIONS="$PWD/sessions.json"

# 1. agent-inflight with its private venv, in the scratch dir
"$REPO/install.sh" --no-skill --no-plugin

# 2. copy the example (your plugin starts as this copy) and install it into
#    that venv, never the system Python
cp -R "$REPO/examples/backend-example" ./my-backend
"$INFLIGHT_HOME/inflight-state/venv/bin/python" -m pip install --quiet --disable-pip-version-check ./my-backend

# 3. the harness's own session record (here: a JSON file) and a tagged entry
cat > sessions.json <<'EOF'
{"sessions": {"demo-1": {"title": "demo from example-harness", "status": "ACTIVE", "secret_note": "never forwarded"}}}
EOF
bin/inflight add --session demo-1 "demo: example backend wired" "next: enable the plugin"

# 4. installed but not enabled: listed, not imported
bin/inflight plugin list

# 5. enable it; `sessions` now asks it first
bin/inflight plugin enable example
bin/inflight sessions
```
<!-- walkthrough:end -->

Expected output of the last three commands (dates and the entry id vary):

<!-- walkthrough-expected:start -->
```text
disabled  example              inflight_backend_example:Backend
enabled example
ACTIVE     demo-1 @example-harness  last ?
           title : demo from example-harness
           entry : YYYY-MM-DD HH:MM [session demo-1 #xxxxxx] — demo: example backend wired. next: enable the plugin
           drill : example-harness show demo-1
           resume: example-harness resume demo-1   <- ACTIVE: do not resume; coordinate first
```
<!-- walkthrough-expected:end -->

`secret_note` does not appear anywhere: the example copies only its
`FIELDS` list. `bin/inflight sessions --json` shows `"backend": "example"`.

To use it for real, install into your actual venv:
`<tracker dir>/inflight-state/venv/bin/python -m pip install <your package>`,
then `inflight plugin enable <name>`. Remove with `inflight plugin disable
<name>` and `pip uninstall`.

### Testing your own plugin

Copy the example's two layers:

1. **Unit:** call `Backend().lookup()` directly against fixture files
   (`tests/test_extending.py::ExampleBackend` does this offline).
2. **End to end:** run the walkthrough above with your package path. The
   assertion that matters is `sessions --json` → `"backend": "<your name>"`
   for an id only your backend knows.

## 3. Adapters

An adapter is an **installer**, not runtime code: it writes the §1 hook
commands into a harness's configuration so the user doesn't have to. v1 has
one built in (`inflight adapter install claude-code`, see
[adapters/claude-code](../adapters/claude-code/README.md)) and **no plugin
API for adapters**: there is no entry-point group for them, and
`inflight adapter` only knows `claude-code`.

To support another harness today:

- **Simplest:** document the hook wiring for that harness, the way
  [adapters/generic/AGENTS-snippet.md](../adapters/generic/AGENTS-snippet.md)
  does for instruction files.
- **An installer of your own** (a script or package) should keep the
  claude-code adapter's rules, which exist for real failure modes:
  - dry run by default; print the exact diff; write only with `--apply`
  - timestamped backup before any write; atomic write; keep the file mode
  - user-level config only; never project files, never managed/enterprise
    settings; if a managed policy blocks user hooks, say so and stop
  - mark your handlers (inflight's marker is the exact command string) so
    uninstall removes only those and leaves everything else byte-for-byte
  - the hook command uses the pinned launcher
    (`<tracker dir>/inflight-state/inflight`), not whatever `python3` is on PATH
- **Upstream:** a PR adding `src/agent_inflight/adapter_<harness>.py` plus
  tests in the style of `tests/test_adapter.py`. Harnesses under evaluation
  get a design note first: [adapters/openclaw.md](adapters/openclaw.md).
