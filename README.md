# Codex Subagent Relay

[![Release Gate](https://github.com/T-Markus-Liang/codex-subagent-relay/actions/workflows/release-gate.yml/badge.svg)](https://github.com/T-Markus-Liang/codex-subagent-relay/actions/workflows/release-gate.yml)

Experimental, dependency-free execution relay for delegating bounded Codex tasks to compatible third-party model Providers.

Codex remains the planner and final reviewer. This relay isolates search, implementation, testing,
debugging, and documentation tasks in a Provider-backed worker with strict result contracts,
side-effect protection, bounded fallback, and release gates.

It does not alter Codex account authentication, root Provider identity, or conversation history.
It is not affiliated with or endorsed by OpenAI, Codex, DeepSeek, SenseNova, OpenCode, or CLIProxyAPI.

## What It Does

- Routes read-heavy work to one configured third-party route and write-heavy work to another.
- Requires real tool activity, a healthy terminal stream, a strict five-field JSON result, and a matching workspace diff before accepting a result.
- Retries a stream failure once, then uses bounded Provider fallback only when the workspace is unchanged.
- Stops on a partial write rather than replaying the task with another Provider.
- Keeps native Codex Agent Team integrations canary-only.

It does **not** provision Provider accounts, API keys, OpenCode, Codex profiles, local bridges, or a third-party Provider. This is a local relay around an already-working integration.

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
| `implementation`, `test`, `debug`, `refactor`, `docs` | `sensenova1 -> sensenova` | workspace-write | 120 seconds |
| architecture, planning, final review | kept by the caller | n/a | n/a |

`opencode-go` is available only with an explicit `--provider` for diagnostics; it is not part of automatic production fallback. The Worker serializes concurrent write tasks per workdir. A write that changes files but fails the result contract returns `partial` and stops all retry/fallback. Review that diff manually.

Use `deepseek-worker --json stats --hours 8` to see local aggregate run metadata without storing task bodies. The local log is `~/.codex/deepseek-worker-runs.jsonl` with mode `0600`.

## Status

This project is experimental. The offline safety gate is deterministic and enforced in CI, but live Provider reliability depends on services outside this repository. Do not use it for unattended or irreversible production changes until your own live soak results meet the policy in [Release Testing](docs/RELEASE_TESTING.md).

## Commands

```bash
deepseek-worker --json doctor
deepseek-worker --json route --role implementation
deepseek-worker --json run --task-file task.md --role implementation --workdir /path/to/repo
deepseek-worker --json launch --task-file task.md --role implementation --workdir /path/to/repo
deepseek-worker --json poll --job-id <job_id>
deepseek-worker --json smoke-test --provider sensenova --workdir /path/to/repo
deepseek-worker --json stats --hours 8
```

`run` invokes OpenCode directly with the configured third-party model IDs. Read-heavy roles use OpenCode's `plan` agent; implementation roles use its `build` agent. Production runs do not change Codex App root Provider or create Codex task threads.

`catalog-build`, `native-check`, `native-v1-canary`, and `cliproxy-native-canary` are experimental diagnostics. Native V1/V2 results do not establish production route reliability. See [Configuration](docs/CONFIGURATION.md) and [Release Testing](docs/RELEASE_TESTING.md).

## Testing

`make release-gate` is credential-free and runs deterministic unit, fault-injection, fallback, concurrency, job-isolation, redaction, and install-launcher checks. GitHub Actions runs this same gate for pushes and pull requests. Live Provider soaks are opt-in, consume quota, and require a disposable directory per attempt. The exact commands, metrics, and pass/fail thresholds are in [Release Testing](docs/RELEASE_TESTING.md).

## Contributing And Security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Report possible exposure of credentials, task text, or raw responses through the private route in [SECURITY.md](SECURITY.md), not a public issue.
