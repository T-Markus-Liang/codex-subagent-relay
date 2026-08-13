---
name: codex-subagent-relay
description: Route bounded repository exploration, implementation, testing, debugging, refactoring, logs, and documentation to the local Codex Subagent Relay through relay_doctor, relay_launch, relay_status, relay_cancel, and relay_stats. Use when execution work should be delegated to DeepSeek while Codex retains planning, decisions, and final review.
---

# Codex Subagent Relay

Use the MCP tools rather than changing Codex Provider settings or invoking native third-party
Subagents for production work. Keep the root Codex identity and conversation history untouched.

1. Call relay_doctor once before the first production task in a conversation. Do not expose its
   internal configuration or any credentials.
2. For eligible work, call relay_launch with a bounded role, existing absolute workdir, scope,
   protected files, acceptance command, and strict final-result requirement. Do not include secrets.
3. Store the returned job ID. Call relay_status for that same job until it is terminal. Do not
   submit a duplicate launch while it is running.
4. For success, inspect the actual diff and named validation before accepting the result. For
   partial, inspect existing changes and do not auto-retry. For a no-side-effect error, one
   bounded retry may be appropriate.
5. Use relay_cancel only for an obsolete running task. Cancellation with filesystem changes is a
   review handoff, never an automatic retry.
6. Use relay_stats for aggregate operational health only. It is separate from Codex and Provider
   billing/token ledgers.

Keep planning, architecture, sensitive actions, irreversible approval, and final review in the main
Codex context. Native Agent Team/CLIProxyAPI remains canary-only until its independent promotion
gate passes.
