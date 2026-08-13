# Codex Subagent Relay

[![Release Gate](https://github.com/T-Markus-Liang/codex-subagent-relay/actions/workflows/release-gate.yml/badge.svg)](https://github.com/T-Markus-Liang/codex-subagent-relay/actions/workflows/release-gate.yml)

Experimental, dependency-free execution relay for delegating bounded Codex tasks to compatible third-party model Providers. Relay version 0.10.2.

Codex remains the planner and final reviewer. This relay isolates search, implementation, testing,
debugging, and documentation tasks in a Provider-backed worker with strict result contracts,
side-effect protection, bounded fallback, and release gates.

It does not alter Codex account authentication, root Provider identity, or conversation history.
It is not affiliated with or endorsed by OpenAI, Codex, DeepSeek, SenseNova, OpenCode, or CLIProxyAPI.

## What It Does

- Routes automatic work to the currently qualified/healthy Provider first and uses the other SenseNova route as bounded fallback. The checked-in order is currently `sensenova -> sensenova1` because the latest explicit smoke showed `sensenova` healthy while `sensenova1` returned upstream stream errors. Re-run explicit smoke before changing this order.
- Requires real tool activity, a healthy terminal stream, a strict five-field JSON result, and a matching workspace diff before accepting a result.
- Retries a stream failure once, then uses bounded Provider fallback only when the workspace is unchanged.
- Stops on a partial write rather than replaying the task with another Provider.
- Keeps native Codex Agent Team integrations canary-only.

It does **not** provision Provider accounts, API keys, OpenCode, Codex profiles, local bridges, or a third-party Provider. This is a local relay around an already-working integration.

## Job Lifecycle

`launch` creates a private durable job directory under `~/.codex/deepseek-worker-jobs/<job-id>/`.
It stores atomically-written `meta.json`, exact before/after workspace manifests, the bounded
structured result, and only a redacted diagnostic when a process exits abnormally. Raw streamed
output is deleted once a job reaches a terminal state. Task text and idempotency keys are never
stored there in plaintext.

States are `queued`, `running`, `succeeded`, `partial`, `blocked`, `failed`, `cancelled`, and
`timed_out`. A timeout, cancellation, or stale process with workspace changes is recorded as
`partial`, so it must be reviewed and is never automatically replayed. A no-side-effect failure is
recorded as `failed` or `timed_out` and remains eligible for a caller-controlled new attempt.
An asynchronous job owns its worker and OpenCode process group, so `cancel` and deadline recovery
terminate the currently executing provider process before the terminal state is made durable.

## Requirements

- macOS or Linux with Python 3.11+ and `make`. Windows is unsupported because the write lock uses POSIX `fcntl`.
- Codex CLI/Desktop available on `PATH`.
- OpenCode installed at `~/.opencode/bin/opencode`, or an alternate executable path supplied with `DEEPSEEK_WORKER_OPENCODE_PATH`.
- A private Provider integration exposing all currently named OpenCode model IDs: `sensenova/deepseek-v4-flash`, `sensenova1/deepseek-v4-flash`, and `opencode-go/deepseek-v4-flash`. The current `doctor` command checks all three slots, even though `opencode-go` is diagnostic-only in automatic routing.
- Matching Codex profile files and local health endpoints. See [Configuration](docs/CONFIGURATION.md).

The repository never needs Provider credentials. Keep credentials in your Provider/OpenCode configuration; do not put them in task files, shell arguments, commits, CI secrets, or issue reports.

## Install And Verify

```bash
git clone https://github.com/T-Markus-Liang/codex-subagent-relay.git
cd codex-subagent-relay
PYTHON=python3.11 make release-gate
PYTHON=python3.11 make install-local
deepseek-worker --version
deepseek-worker --json doctor
```

The installer creates `~/.local/bin/deepseek-worker`; add that directory to `PATH` if needed. It writes a launcher bound to the Python interpreter used during installation, preventing an older system `python3` from running the Worker. A healthy `doctor` report is required before live work.

## Quick Start

Inspect routing without contacting a model:

```bash
deepseek-worker --json route --role repository-exploration
deepseek-worker --json route --role implementation
```

Run a bounded read-only task synchronously. This consumes Provider quota:

```bash
deepseek-worker --json run \
  --role repository-exploration \
  --workdir "$(pwd)" \
  --provider auto \
  --task "Inspect the repository structure only. Do not modify files. Return the required JSON result."
```

For execution work, use the non-blocking interface and poll the same job instead of launching duplicates:

```bash
deepseek-worker --json launch \
  --role implementation \
  --workdir /absolute/path/to/repository \
  --provider auto \
  --task-file /absolute/path/to/bounded-task.md

deepseek-worker --json poll --job-id <job_id>
```

`poll` can return `{"status":"running"}` while the Worker is active. Wait and poll the same job again. Do not start a second job for the same write task. Use `--task-file` for substantial briefs, and never include credentials or raw request data in it.

To make a caller retry-safe, supply an opaque key. The Relay stores only its SHA-256 digest and will
return the existing job for the same key and workdir, including across processes:

```bash
deepseek-worker --json launch --role implementation --workdir /absolute/path/to/repository \
  --task-file /absolute/path/to/bounded-task.md --idempotency-key caller-generated-opaque-key
deepseek-worker --json status --job-id <job_id>
deepseek-worker --json cancel --job-id <job_id>
deepseek-worker --json list --limit 20
deepseek-worker --json inspect --job-id <job_id>
deepseek-worker --json cleanup --retention-hours 168
deepseek-worker --json cleanup --retention-hours 168 --apply
deepseek-worker --json cleanup --retention-hours 168 --legacy-action archive
```

Every accepted terminal result has this exact core contract:

```json
{
  "status": "success",
  "summary": "concise result",
  "files_changed": ["relative/path"],
  "tests": ["command: passed"],
  "risks": []
}
```

The relay adds bounded metadata such as Provider, duration, usage, and stream retry count. Independently inspect `files_changed`, `git diff`, and the listed tests before accepting any write.

## Routing And Safety

| Role family | Default Provider order | Sandbox | Default budget |
| --- | --- | --- | --- |
| `search`, `repository-exploration`, `logs` | `sensenova -> sensenova1` | read-only | 75 seconds |
| `implementation`, `test`, `debug`, `refactor`, `docs` | `sensenova -> sensenova1` | workspace-write | 120 seconds |
| architecture, planning, final review | kept by the caller | n/a | n/a |

`opencode-go` is available only with an explicit `--provider` for diagnostics; it is not part of automatic production fallback. The Worker serializes concurrent write tasks per workdir. A write that changes files but fails the result contract returns `partial` and stops all retry/fallback. Review that diff manually.

Terminal job artifacts are retained for seven days by default. Cleanup is a dry run unless
`--apply` is supplied; it never deletes queued/running jobs, malformed directories, or jobs completed
inside the specified retention window. Review the candidate IDs before applying cleanup.

Earlier Worker releases stored flat `<job-id>.json` records in the job root. Current `list` reports
them as `legacy` using only safe lifecycle fields; `inspect` is read-only and they cannot be resumed.
Normal cleanup leaves them untouched. First preview `cleanup --legacy-action archive`, then add
`--apply` to move verified inactive records to the private `legacy/` directory. This never deletes or
converts historical artifacts.

Use `deepseek-worker --json stats --hours 8` to see local aggregate run metadata without storing task bodies. It reports overall plus role/Provider success rates, p50/p95 duration, fallback, partial-write, and stream-retry counts. The local log is `~/.codex/deepseek-worker-runs.jsonl` with mode `0600`; its `by_run_type` section keeps external production runs separate from native canaries.

Automatic routing maintains a short-lived per-Provider circuit in `~/.codex/deepseek-worker-circuit.json`. Two no-side-effect failures temporarily skip a Provider for 30 seconds, allowing the other route to run without spending its full timeout. Explicit `--provider` diagnostics bypass this state. State persistence is fail-open: an unreadable or unwritable circuit file never blocks a task. Any workspace side effect still stops retry and fallback.

## Status

This project is experimental. The offline safety gate is deterministic and enforced in CI, but live Provider reliability depends on services outside this repository. Do not use it for unattended or irreversible production changes until your own live soak results meet the policy in [Release Testing](docs/RELEASE_TESTING.md).

## Commands

```bash
deepseek-worker --json doctor
deepseek-worker --json route --role implementation
deepseek-worker --json run --task-file task.md --role implementation --workdir /path/to/repo
deepseek-worker --json launch --task-file task.md --role implementation --workdir /path/to/repo
deepseek-worker --json poll --job-id <job_id>
deepseek-worker --json status --job-id <job_id>
deepseek-worker --json cancel --job-id <job_id>
deepseek-worker --json list --limit 20
deepseek-worker --json inspect --job-id <job_id>
deepseek-worker --json cleanup --retention-hours 168
deepseek-worker --json smoke-test --provider sensenova --workdir /path/to/repo
deepseek-worker --json stats --hours 8
```

`run` invokes OpenCode directly with the configured third-party model IDs. Read-heavy roles use OpenCode's `plan` agent; implementation roles use its `build` agent. Production runs do not change Codex App root Provider or create Codex task threads.

`catalog-build`, `native-check`, `native-v1-canary`, and `cliproxy-native-canary` are experimental diagnostics. Native V1/V2 results do not establish production route reliability. See [Configuration](docs/CONFIGURATION.md) and [Release Testing](docs/RELEASE_TESTING.md).

## Codex Plugin And MCP

The bundled plugin package exposes the stable Relay lifecycle as local typed MCP tools:
relay_doctor, relay_launch, relay_status, relay_cancel, and relay_stats. It is a thin stdio facade
over this repository's Worker command: it does not receive Provider credentials, call a Provider
directly, or mutate Codex Provider/session configuration.

The repository includes a marketplace manifest at `.agents/plugins/marketplace.json`. Add the
repository as a local or Git marketplace, then install the plugin:

```bash
codex plugin marketplace add /absolute/path/to/codex-subagent-relay
codex plugin add codex-subagent-relay@codex-subagent-relay
```

For a Git checkout, use the repository URL instead of the local path. Validate the package with the
Codex plugin validator before installing it in Codex. After installation, start a new Codex thread
so the thread loads the updated Skill and MCP tool declarations.

The external Relay remains the production execution backend. Native Codex Subagents remain
canary-only until their independent promotion gate passes.

The tracked compatibility manifest is structurally valid without live evidence. A stable
publication requires separate qualifying records for both production routes. Run
scripts/validate_compatibility.py with --require-evidence to enforce that gate; it intentionally
fails while either route lacks 100 read plus 100 write qualifying evidence.

## Testing

`make release-gate` is credential-free and runs deterministic unit, fault-injection, fallback, concurrency, job-isolation, redaction, qualification-workspace, plugin-package, and install-launcher checks. GitHub Actions runs this same gate for pushes and pull requests. Live Provider soaks are opt-in, consume quota, and require a disposable directory per attempt. The runner defaults to the actual `auto` route, uses a bounded per-job deadline, cancels the same durable job on interruption, and verifies that reads are non-mutating and writes produce exactly the expected file. The exact commands, metrics, and pass/fail thresholds are in [Release Testing](docs/RELEASE_TESTING.md).

## Contributing And Security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Report possible exposure of credentials, task text, or raw responses through the private route in [SECURITY.md](SECURITY.md), not a public issue.
