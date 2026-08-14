# Release Testing

`deepseek-worker` has two deliberately separate test planes.

## Offline Release Gate

Run this locally and in CI for every pull request and release:

```bash
make release-gate
```

The gate is deterministic and uses no network, model account, API key, task body, or real project.
It must pass completely. It verifies:

- Python compilation and the full unit suite, including an isolated install-launcher regression test.
- Strict five-field result contracts and rejection of malformed, guessed, no-tool, stream-error,
  missing-finish, and length-truncated results.
- Bounded two-provider fallback under repeated bad primary responses.
- Per-workdir write lock contention: one owner and all competing writers blocked.
- Concurrent `launch -> poll` job identity and output-file isolation.
- Durable job lifecycle: atomic metadata/results/manifests, idempotent launch reuse, cancellation,
  stale-PID recovery, timeout/partial-write terminalization without automatic replay, and explicit
  dry-run-first retention cleanup that never deletes active jobs. Earlier flat job artifacts are
  observable and archive-only; they are never silently deleted or fabricated into resumable jobs.
- Run metadata excludes task text and secret-like result content.
- Real workspace changes stop automatic retry and Provider fallback.
- Native canaries remain evidence-only and must never promote a production route automatically.
- The bundled plugin manifest, repository marketplace entry, executable MCP launcher, and MCP
  initialize/tools-list handshake are validated by the release gate.
- Automatic routing and native canary telemetry are separate: compare `by_run_type.external_run` with
  `native_v1_canary`, `native_smoke_test`, or `cliproxy_native_canary`; never combine their success rates.
- Circuit state is tested across process boundaries through its private state file, including TTL expiry,
  explicit-provider bypass, and fail-open behavior on persistence errors.

GitHub Actions runs this same command on every pull request and push to `main`. It has read-only repository permissions and receives no Provider credentials.

## Offline Stress Matrix

The release gate is a deterministic stress test of relay behavior, not a claim about remote Provider reliability.

| Scenario | Test shape | Required result |
| --- | --- | --- |
| Stream faults | malformed, guessed, no-tool, error, missing-finish, length-truncated streams, and same-session finalization recovery | no invalid result accepted; recovery never replays a task or continues after a workspace change |
| Fallback | 32 repeated invalid-primary / valid-secondary sequences | bounded retry and only valid secondary acceptance |
| Write contention | 12 simultaneous lock contenders for one workdir | exactly one owner, 11 blocked |
| Job isolation | 24 concurrent `launch` operations | unique job IDs and private metadata/stdout/stderr paths |
| Lifecycle | success, stale PID, cancellation, timeout with side effects, idempotent repeat | durable terminal state; changed work is always `partial` |
| Data protection | synthetic task and secret-like risk content | neither persisted in run metadata |
| Side effects | invalid result after workspace change | no retry or Provider fallback |
| Install portability | temporary-home install with selected Python | installed launcher invokes that Python |

## Optional Live Provider Soak

Live tests are manual because they consume quota and test an upstream outside this repository.
They must use a disposable directory, a dedicated non-production profile, and a fixed budget. Never
place credentials in task files, shell history, CI logs, issue comments, or committed configuration.

Before a candidate release:

1. Run `deepseek-worker --json doctor` and save only its redacted JSON report.
2. Run at least 100 independent, sequential disposable read-only jobs per production route.
3. Run at least 100 independent, sequential disposable write jobs per production route; each writes
   one known file and verifies it. Use a fresh directory for every job.
4. Run a separate bounded concurrency experiment. Start at one job and increase one level at a time;
   do not use its result to justify the production concurrency default unless every level meets the
   release policy.
5. Keep native V1 and V2 canaries in a separate report. Their pass rate does not count toward the
   external Worker production SLO.

The included runner is sequential by design, defaults to the real automatic route, creates one disposable directory per job, verifies that read-only work leaves it empty, and verifies the exact single-file byte result. Run the Phase 0 qualification in batches so an upstream outage is visible before consuming the full budget:

```bash
python3.11 scripts/live_soak.py --confirm-live --provider auto --role repository-exploration \
  --runs 30 --report reports/phase0-auto-read-30.json
python3.11 scripts/live_soak.py --confirm-live --provider auto --role documentation \
  --runs 30 --report reports/phase0-auto-write-30.json
```

When an upstream is visibly unhealthy, add `--max-consecutive-failures 3` to preserve a bounded
failed-evidence report and stop consuming qualification quota after three consecutive non-success
attempts. The stopped report is not qualifying evidence: it remains below the required job count.
Use `0` (the default) only when deliberately completing a full diagnostic batch.

For publication qualification, repeat with `--runs 100 --require-opencode-idle` for each production role and separately use explicit Provider runs only as diagnostic controls. This preflight refuses to start while any pre-existing OpenCode process exists and records only an idle boolean plus process count; it never records process IDs, paths, or command arguments. Use `--job-deadline 180` (or a reviewed larger bound) so a long upstream call cannot leave the batch unbounded; interruption cancels the same durable job and writes a partial report. The tracked compatibility manifest and `scripts/validate_compatibility.py --require-evidence` define the publication contract for every production route. The report contains aggregate metrics plus bounded per-run status, selected Provider, terminal safety fields, and no prompt, workspace path, request, response, or credential data. A nonzero exit means at least one run did not complete or violated workspace verification.

After a route's explicit reports pass, generate a candidate manifest instead of editing the tracked
manifest by hand:

```bash
python3 scripts/qualify_manifest.py \
  --manifest compatibility/manifest.json \
  --route sensenova1 \
  --read-report reports/sensenova1-read-100.json \
  --write-report reports/sensenova1-write-100.json \
  --out /tmp/manifest-sensenova1.json
```

The runner reports safety and task completion separately: a failed write that leaves an empty
disposable workdir is a no-side-effect task failure, not a workspace safety violation. Any unexpected
file, wrong bytes, or partial write remains a safety violation. The converter rejects `auto` reports,
short batches, rates below 95%, workspace safety failures, partial writes, and missing explicit
zero-duplicate evidence. Review the candidate, then replace the tracked
manifest only as part of the release commit. Once every production route is represented, run
`python3 scripts/validate_compatibility.py --require-evidence`.

Run one explicit qualification batch per Provider at a time. Relay now uses a local one-slot lease
per Provider: an explicit run returns `blocked` while that route is executing another Relay task;
an automatic production task can use the next route instead. Do not treat a batch run while other
tasks share the Provider as qualification evidence.

For a concurrency experiment, keep write tasks pointed at one workdir and verify all but one return `blocked`; use separate disposable workdirs for read-only parallelism. Record the exact concurrency level, start method, status counts, p50/p95 duration, stream failure reasons, fallback count, and partial-write count. Do not publish a concurrency guarantee beyond the highest fully observed level.

Record only: timestamp, Worker version, run type, role, provider route, status, duration, stream finish
reason, retry/fallback flags, bounded attempt-failure categories, all-attempt Relay usage, and accepted-success Relay usage. Accepted-success usage contains only the final strict-contract attempt and must be reported separately from failed/retried attempt usage. Do not publish prompts, workspace paths,
request bodies, response bodies, thread ids, credentials, or raw logs.

`deepseek-worker --json stats --hours 168` summarizes the local ledger with separate run types,
role/Provider success rates, p50/p95 durations, fallback count, partial-write count, and stream-retry
count. It is an operational indicator, not an accounting ledger for Codex or Provider billing.

For the required seven-day operational observation, generate a source-local UTC report at the end of
each day and preserve only the redacted aggregate report:

```bash
python3.11 scripts/operational_report.py --days 7 --relay-version 0.10.14 \
  --run-type external_run --telemetry-scope production \
  --since <release-utc-timestamp> --out reports/operational-7d.json
```

Only a clean post-release window is eligible: require the release's `--relay-version`, filter to
`--run-type external_run --telemetry-scope production`, and set `--since` to the release UTC
timestamp. Run qualification with `--no-record` and mark manual experiments as `--telemetry-scope
diagnostic`. The report exposes excluded-row counts so legacy or diagnostic telemetry cannot
silently enter the window. Review the report's
daily and per-Provider success rate, P50/P95, fallback, stream retry, partial-write, and blocked
counts. Its Relay telemetry must remain separate from the Codex Rollout and CC Switch token ledgers;
use the documented `codex-usage-audit` workflow to report those sources.

## Suggested Publication Policy

Do not advertise a Provider reliability guarantee from an ad-hoc smoke test. A maintainable initial
policy is:

| Gate | Release condition |
| --- | --- |
| Offline gate | 100% pass |
| Read-only route | at least 100 runs, >= 95% complete success |
| Write route | at least 100 runs, >= 95% complete success and 0 duplicate-write incidents |
| Contract safety | 0 accepted results without tool activity or a healthy terminal frame |
| Partial writes | 100% stop retry/fallback and require human diff review |
| Concurrency | publish observed safe level; do not claim more |
| Native agents | experimental until independent repeated end-to-end passes |

If the live route misses a threshold, publish the observed failure classes and keep that route
experimental or disabled from automatic fallback. The gate should make a bad release visible, not
hide an upstream reliability problem.

## Evidence Boundary

The CI badge proves only the offline gate for its referenced commit. It does not prove third-party Provider availability, latency, tool quality, quota, or success rate. Native V1/V2 Agent Team paths remain experimental and must be reported separately from external Worker `run`/`launch` results.

## Current Native Finding

The current Desktop Codex `0.147.0-alpha.6.5` V1 role bisection is blocked at the first `builtin`
layer. It records a `spawn_agent` tool call but no parent wait, no direct child result, no Provider
bridge completion, and no isolated child metadata. This localizes the failure to native Agent Team
parent-child lifecycle before role overrides or third-party Provider selection. It is negative
research evidence, not a reason to lower the native contract or to promote the native backend.
