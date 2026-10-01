# OpenClaw adapter: design note (not built)

Status: **design only.** OpenClaw is not a v1 built-in (plan non-goal). Nothing
in this repo runs inside OpenClaw. This note records how an adapter would map
OpenClaw's plugin hooks onto [hook protocol v1](../hook-protocol.md), so the
work can start from facts rather than guesses.

Sources, read 2026-10-01 (OpenClaw's own docs; primary):
[Plugin hooks](https://docs.openclaw.ai/plugins/hooks),
[Hook reference](https://docs.openclaw.ai/plugins/hooks/reference),
[Prompt and session hooks](https://docs.openclaw.ai/plugins/hooks/prompt-and-session),
[Tool call policy hooks](https://docs.openclaw.ai/plugins/hooks/tool-policy).
Nothing below was run against a live OpenClaw install. Every claim is from
those pages unless marked **unverified**.

## Shape

OpenClaw hooks are **in-process TypeScript plugins** in the Gateway
(`api.on("<hook>", handler)`), not shell commands. An adapter would be a small
OpenClaw plugin whose handlers spawn `inflight hook <event> --harness openclaw`
with the JSON payload on stdin. That keeps agent-inflight's code out of the
Gateway process, the same boundary the Hermes plugin keeps for third-party
backends.

## Event mapping

| OpenClaw hook | Kind (per docs) | inflight event | Payload from | Notes |
|---|---|---|---|---|
| `session_start` | observe | `session-start` | `ctx.sessionId`; `source`: `resume` if the event has `resumedFrom`, else `new` | Docs: "has no reason field; it can include `resumedFrom`". |
| `before_tool_call` | modify / gate | `pre-tool` | `event.toolName`, `ctx.sessionId` | **Fail-closed hook:** docs say a thrown error or timeout (15 s default) blocks the tool call. The handler must catch everything, return nothing (no `block`, no `params`), and bound the subprocess well under 15 s. Use the `matcher` option (canonical tool ids such as `exec`, `apply_patch`) to limit it to editing/shell tools. |
| `after_tool_call` | observe | `post-tool` | same | |
| `after_compaction` | observe | `session-start` with `source: compact` | `ctx.sessionId` | Re-inject text has to reach the model on the **next** turn; see open questions. |
| `before_prompt_build` | modify | (delivery only) | | Returns `prependContext`/`appendContext`. A candidate for delivering the re-inject block and collision warning. Needs conversation access (below). |
| `session_end` | observe | `session-end` | `ctx.sessionId`, `event.reason` | Reasons per docs: `new`, `reset`, `idle`, `daily`, `compaction`, `deleted`, `shutdown`, `restart`, `unknown`. `reason: compaction` is a rollover, not a dead session: map it to a lineage link, not ENDED (**unverified** which session id the successor carries). |

There is no `cwd-changed` equivalent in the catalog; `cwd` would have to come
from the tool context or the agent workspace (**unverified**: the docs
reviewed do not name a cwd/workspace field on these hook contexts).

## Permissions it would need

From the "Permissions and scope" section of the Plugin hooks page:

- A **non-bundled** plugin needs
  `plugins.entries.<id>.hooks.allowConversationAccess: true` for
  `before_prompt_build` (and other conversation hooks). The user must grant it
  in `openclaw.json`; the adapter must not write that key silently. Per
  agent-inflight's adapter rules, an installer would print the exact change
  and apply only on `--apply`.
- `before_prompt_build` is also blocked by `allowPromptInjection: false`
  (default allowed). Both permissions are needed for re-injection.
- `session_end` works **without** conversation access (metadata only). The
  adapter never needs `ctx.endedTranscript` and must not request it.
- `before_tool_call`, `after_tool_call`, `session_start` and
  `after_compaction` are not in the conversation-access list on that page.
  **Unverified** whether any other gate applies to them.

Least-privilege variant: without conversation access the adapter still gets
heartbeats, repo recording, `session-end` and catch-up, but no re-injection
and no collision warning in the model's context. That should be the default
install; conversation access opt-in.

## Open questions

1. **Compaction hook names and semantics.** The reference lists
   `before_compaction` / `after_compaction` as observe-only, and says
   `after_compaction` fires on successful engine-owned compaction even with
   `compactedCount: 0`. Unverified: whether compaction in non-embedded
   runtimes (the docs single out Codex and Copilot harness boundaries) emits
   these at all, and whether `session_end` with `reason: compaction` and
   `after_compaction` both fire for one compaction.
2. **Delivery after compaction.** `after_compaction` cannot return context.
   Candidates: stash the block and return it from the next
   `before_prompt_build`, or `api.session.workflow.enqueueNextTurnInjection`
   (docs: drained before prompt hooks on embedded and CLI paths, not on Codex
   or Copilot). Which one is reliable across runtimes is unverified.
3. **Session id stability.** Docs show both `ctx.sessionKey` and
   `ctx.sessionId`, and `sessions.create` with `parentSessionKey` /
   `succeedsParent`. Which one is stable across `/reset` and compaction, and
   which to export as `INFLIGHT_SESSION_ID` to the agent's `exec` (via
   `resolve_exec_env`, per the tool-policy page), is unverified.
4. **Backend.** Whether OpenClaw exposes a readable session store (for a
   backend plugin per [extending.md](../extending.md) §2) or only the
   Gateway API (which would mean a network call, out of bounds for a
   backend). Until known, the `heartbeat` backend covers status.
5. **Known bug history.** OpenClaw issue #5943 reported `before_tool_call`
   defined but not invoked in the tool pipeline (secondary source: the issue
   tracker, status not checked). Confirm on the target version before relying
   on `pre-tool`.
