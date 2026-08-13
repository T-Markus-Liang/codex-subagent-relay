# Codex Subagent Relay

Experimental, dependency-free execution relay for delegating bounded Codex tasks to compatible third-party model Providers.

Codex remains the planner and final reviewer. This relay isolates search, implementation, testing,
debugging, and documentation tasks in a provider-backed worker with strict result contracts,
side-effect protection, bounded fallback, and release gates.

It does not alter Codex account authentication, root Provider identity, or conversation history.
It is not affiliated with or endorsed by OpenAI, Codex, DeepSeek, SenseNova, OpenCode, or CLIProxyAPI.

## Install

```bash
make install-local
```

The installer creates `~/.local/bin/deepseek-worker`; add that directory to `PATH` if needed.
Use Python 3.11 or later.

## Status

This project is experimental. The offline safety gate is deterministic and enforced in CI, but live
Provider reliability depends on services outside this repository. Do not use it for unattended or
irreversible production changes until your own live soak results meet the policy in
[`docs/RELEASE_TESTING.md`](docs/RELEASE_TESTING.md).

## Release Gate

Run the credential-free release gate before every public release and in CI:

```bash
make release-gate
```

It compiles the Worker and runs deterministic tests for result-contract rejection, stream failures,
bounded fallback, write-lock contention, concurrent job isolation, and run-log redaction. It does not
call a model, contact a Provider, read task bodies, or require a Codex account.

The full offline/live test protocol and publication thresholds are in
[`docs/RELEASE_TESTING.md`](docs/RELEASE_TESTING.md). The included GitHub Actions workflow runs only
the offline gate; it never receives provider credentials.

Live Provider tests are deliberately manual because they consume quota and measure an upstream that
is outside this repository. Run `deepseek-worker --json doctor`, then launch small, disposable,
single-task read and write smokes. Record Provider, timestamp, completion rate, p50/p95 duration,
stream failure classes, fallback rate, and partial-write rate in a release report. Do not claim a
Provider SLO from fewer than 100 completed attempts per route; keep native V1/V2 results separate.

## Commands

```bash
deepseek-worker --json doctor
deepseek-worker --json catalog-build
deepseek-worker --json native-check
deepseek-worker --json native-v1-canary --provider sensenova --workdir /path/to/repo
deepseek-worker --json cliproxy-native-canary --parent-model gpt-5.6-sol --workdir /path/to/repo
deepseek-worker --json route --role implementation
deepseek-worker --json run --task-file task.md --role implementation --workdir /path/to/repo
deepseek-worker --json smoke-test --provider sensenova --workdir /path/to/repo
deepseek-worker --json stats --hours 8
```

`run` invokes OpenCode directly with `sensenova/deepseek-v4-flash`, `sensenova1/deepseek-v4-flash`, or `opencode-go/deepseek-v4-flash`. Read-heavy roles use OpenCode's `plan` agent; implementation roles use its `build` agent. The separate Codex profile-v2 bridge remains available for native canaries. Production runs do not change the Codex App root provider or create Codex task threads.

With `--provider auto`, the worker uses two bounded SenseNova routes: read-only work uses `sensenova -> sensenova1`, while write-heavy and general work use `sensenova1 -> sensenova`. The default read-only budget is 75 seconds (55s primary, 20s fallback); the write/general budget is 120 seconds (90s primary, 30s fallback). A positive `--timeout` overrides the role default. `opencode-go` remains available only when explicitly selected for diagnostics; it is excluded from automatic production fallback after slow unreliable runs. Worker subprocesses set `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=131072` without modifying the user's shell or OpenCode configuration.

A result is accepted only when OpenCode records real tool activity, emits a healthy terminal `step_finish`, and the final text contains the five-field JSON contract. Missing finish frames, `length`, `error`, `unknown`, `other`, and zero usable output are stream failures. When such a failure leaves the workspace unchanged, the Worker retries that Provider once inside its original timeout budget before normal Provider fallback. Token usage is aggregated across every attempt.

`stats --hours` reports status counts, per-provider success rates and durations, and input, cached input, cache-write, output, reasoning, request, and context Token totals without reading task bodies.

If a write attempt changes the workspace but returns an invalid contract or unhealthy stream terminal, the Worker returns `partial` and stops retry/fallback immediately. This prevents another attempt from repeating or overwriting the first write; the main model must review the existing diff before deciding what remains. For no-side-effect errors, let the one automatic fallback complete; retry the bounded job only once when it remains justified, otherwise hand it back to the main model immediately.

The installed command uses `/opt/homebrew/bin/python3` because the macOS system Python is too old for Codex's TOML configuration parsing.

## JSON contract

Successful runs return `status`, `summary`, `files_changed`, `tests`, `risks`, provider/profile metadata, bounded usage counters, duration, and non-sensitive stream diagnostics (`stream_finish_reason`, `stream_retry_count`). The parser accepts exact, fenced, or prose-wrapped JSON and normalizes common completion aliases. It also repairs only three known safe empty-list variants: `files_changed: 0`, `tests: "none"`, and `risks: "none"`. The exact five-field shape and all other types remain strict. Errors use `{"status":"error","error":"..."}` or the same structured result shape with bounded, redacted risk text.

Run metadata is appended to `~/.codex/deepseek-worker-runs.jsonl` with mode `0600`. Task bodies, model responses, credentials, and raw logs are never stored there.

`catalog-build` writes a candidate catalog that preserves the live GPT model list and appends DeepSeek. It does not modify `config.toml` or enable that catalog globally. `native-check` loads the separate `ds-native-catalog-test` profile.

`cliproxy-native-canary` uses an isolated `CODEX_HOME` and the local CLIProxyAPI canary service. It
routes the Sol parent and a Codex-safe `gpt-5.6-luna` alias through CLIProxyAPI, then discovers the
unique child from the isolated state DB and inspects its rollout. The child must read two hidden
workspace tokens through parallel shell calls, return matching tool outputs, and emit the strict
five-field JSON contract. A parent-visible JSON response alone never passes.

As of 2026-08-13, native V1 and V2 routes are experimental only. The V1 canary now uses a minimal
version-compatible isolated config and creates an isolated state DB, but the current Desktop run is
blocked at `spawn_agent` with `agent type is currently not available`; V2 also remains intermittent
on parent argument serialization and tool-output continuation. Keep the external OpenCode Worker as
production.
