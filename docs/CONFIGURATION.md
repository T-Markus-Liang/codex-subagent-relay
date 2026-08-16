# Configuration

Codex Subagent Relay deliberately does not create Provider accounts, API keys, or proxy routes. Configure those outside this repository, then use `doctor` to prove the local contract is complete.

## Declarative Relay Policy

The tracked [`relay.toml`](../relay.toml) is the versioned, secret-free routing policy. It declares
Provider aliases, OpenCode adapter model IDs, profile names, local health ports, role families,
timeouts, and automatic fallback order. It does not accept URLs, API keys, bearer tokens, or task
text. The runtime validates this file before it accepts commands.

`relay_runtime/routing.py` is the pure policy layer for this file: it validates the declared
Provider/role/timeout policy, resolves a role to a route, and computes bounded fallback timing.
It cannot spawn OpenCode, inspect a task workspace, access job artifacts, or read credentials.
`relay_runtime/opencode_adapter.py` only turns the already-validated Provider model ID into a fixed
OpenCode command; it does not execute the command or accept arbitrary arguments.
`relay_runtime/telemetry.py` receives only aggregate status, usage, and bounded failure-category
fields for append-only operational records; it does not receive task text or raw model output.

For a local policy experiment, set `DEEPSEEK_WORKER_CONFIG=/absolute/path/to/relay.toml` for the
single command and run `doctor` first. Do not point a shared production installation at an
unreviewed policy file. New adapter types require code and release-gate coverage; configuration
cannot make the runtime execute an arbitrary command.

## Runtime Contract

The current release has three named Provider slots. They are implementation names, not a portability layer for arbitrary model IDs. The current `doctor` command requires all three slots to be present and healthy before it returns `healthy`; configure all three even if normal automatic routing uses only the two SenseNova slots.

| Slot | Required Codex profile | Required OpenCode model ID | Health endpoint checked by `doctor` |
| --- | --- | --- | --- |
| `sensenova` | `~/.codex/ds-sensenova.config.toml` | `sensenova/deepseek-v4-flash` | `http://127.0.0.1:15741/v1/health` |
| `sensenova1` | `~/.codex/ds-sensenova1.config.toml` | `sensenova1/deepseek-v4-flash` | `http://127.0.0.1:15742/v1/health` |
| `opencode-go` | `~/.codex/ds-opencode-go.config.toml` | `opencode-go/deepseek-v4-flash` | `http://127.0.0.1:15743/v1/health` |

Each profile must minimally identify the slot and model:

```toml
model_provider = "sensenova"
model = "deepseek-v4-flash"
```

The actual base URL, authentication, and Provider-specific setup belong in your private Codex/OpenCode integration. Never commit those files. The Worker invokes OpenCode directly, so OpenCode must independently resolve each model ID. A profile that merely exists does not prove the Provider can run tasks.

## Required Local Tools

1. Install Codex and make `codex` available on `PATH`.
2. Install OpenCode. Its executable defaults to `~/.opencode/bin/opencode`.
3. Configure all three Provider slots in your private OpenCode/Codex setup.
4. Keep the Codex root `model_provider` set to `openai-chatgpt`. The relay does not switch it.
5. Install this project with Python 3.11+ and run `deepseek-worker --json doctor`.

For another OpenCode location, set the path for the command that needs it:

```bash
DEEPSEEK_WORKER_OPENCODE_PATH=/absolute/path/to/opencode deepseek-worker --json doctor
```

## Reading `doctor`

`healthy` means Codex is available, OpenCode is executable, the root Provider is `openai-chatgpt`, expected profiles identify the expected model, health URLs respond, and the candidate native model catalog can be read.

`healthy` is not a live task success guarantee. Before real work, run one disposable read-only smoke against each route you plan to use. The checked-in automatic order is currently `sensenova`, then `sensenova1`, based on the latest explicit smoke evidence; change it only after a fresh comparison. `opencode-go` remains explicit-diagnostic-only.

```bash
mkdir -p /tmp/codex-subagent-relay-smoke
deepseek-worker --json smoke-test --provider sensenova --workdir /tmp/codex-subagent-relay-smoke --no-record
```

That command consumes Provider quota. Delete the disposable directory when finished.

## Native Research Boundary

The external Relay is the production executor. `native-v1-canary`, `native-v1-tool-canary`,
`native-role-bisection`, and `cliproxy-native-canary` are isolated diagnostics only. They create a temporary `CODEX_HOME` and
must leave the root `model_provider`, conversation database, and rollout history unchanged.

`native-role-bisection` starts with a built-in role, then adds only one role layer at a time. A
failure in `builtin` means Codex Agent Team registration or parent-child lifecycle failed before a
role file or third-party Provider could be responsible. A passing `spawn_agent` call alone is not
success: the gate also requires an actual parent wait, direct child JSON with its random nonce,
bridge completion, and isolated child Provider/model/role metadata. Do not use native routing for
production until its separate qualification gate passes.

Native V1 canaries set `features.plugins=false` and `features.remote_plugin=false` on their
temporary CLI invocation. Plugin discovery and plugin manifests are unrelated to Agent Team
delivery and may fail independently of the parent/child protocol. This does not modify the live
Codex configuration or disable plugins for normal Relay MCP use.

The V1 parent prompt explicitly calls the namespaced `wait_agent` tool after `spawn_agent`; a
plain-language request to wait is not enough to prove parent-side result delivery.

`native-v1-tool-canary` additionally places two random hidden values in its disposable workspace.
The child must read both with parallel shell calls, return their exact values through `wait_agent`,
and leave matching tool, rollout, bridge, and child-metadata evidence. It is still canary-only.

Run `scripts/native_soak.py` only for explicit research. It creates a fresh disposable workspace for
every native tool canary and stores only booleans, durations, fixed error categories, and aggregate
counts; hidden values, task text, raw output, workspaces, credentials, and session identifiers are
never written to its report. Promotion evidence needs at least 100 sequential strict E2E results,
at least a 95% strict-success rate, and zero observed root-config or root-state mutations. A passing
report does not switch routing or modify the compatibility manifest; native remains canary-only until
a human reviews the evidence.

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DEEPSEEK_WORKER_OPENCODE_PATH` | OpenCode executable used by the external Worker | `~/.opencode/bin/opencode` |
| `DEEPSEEK_WORKER_CLIPROXY_URL` | CLIProxyAPI V2 canary endpoint only | `http://127.0.0.1:18317/v1` |
| `DEEPSEEK_WORKER_CLIPROXY_TOKEN` | CLIProxyAPI V2 canary token only | local canary placeholder |
| `DEEPSEEK_WORKER_CLIPROXY_ALIAS` | CLIProxyAPI V2 child model alias only | `gpt-5.6-luna` |
| `DEEPSEEK_WORKER_CLIPROXY_LOG_PATH` | CLIProxyAPI V2 canary request log | user-local CLIProxyAPI path |

The `CLIPROXY` variables affect only the experimental native V2 canary. They are not required for external `run` or `launch` execution.

## Job Artifact Retention

Each Relay job stays under the private `~/.codex/deepseek-worker-jobs/` directory for inspection.
To preview expired **terminal** jobs, run:

```bash
deepseek-worker --json cleanup --retention-hours 168
```

The default is seven days. Add `--apply` only after reviewing `candidate_job_ids`. Cleanup never
deletes queued or running jobs, and skips malformed job directories instead of guessing whether they
are safe to remove. Retention is local operational hygiene; it does not alter Codex state, threads,
Provider configuration, or run-log aggregation.

Flat `*.json` records created by pre-runtime Worker releases are never silently deleted. They are
counted by `list` and reported as `legacy` artifacts. `cleanup --legacy-action archive` previews
their movement; adding `--apply` moves only verified inactive artifacts into
`~/.codex/deepseek-worker-jobs/legacy/`. It does not transform them into modern resumable jobs.

## Automatic Provider Circuit

Automatic runs persist a small private circuit state file at `~/.codex/deepseek-worker-circuit.json`, protected by a local lock and atomically replaced. After two no-side-effect failures for one Provider, that Provider is skipped for 30 seconds. A successful run clears its state. Explicit `--provider` runs bypass the circuit for diagnostics and recovery. If the state file cannot be read or written, routing fails open and continues normally. A write-side effect never triggers automatic replay or Provider fallback.

## Local Provider Leases

The Relay permits one active local execution per upstream Provider. This prevents multiple Codex
tasks or a qualification batch from overloading one route. An automatic request skips a busy route
and tries the next configured route. An explicit Provider request returns a bounded `blocked`
terminal result without contacting the Provider. Provider leases are process-local coordination,
not a distributed quota or availability guarantee.
