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
- Run metadata excludes task text and secret-like result content.
- Real workspace changes stop automatic retry and Provider fallback.
- Native canaries remain evidence-only and must never promote a production route automatically.

GitHub Actions runs this same command on every pull request and push to `main`. It has read-only repository permissions and receives no Provider credentials.

## Offline Stress Matrix

The release gate is a deterministic stress test of relay behavior, not a claim about remote Provider reliability.

| Scenario | Test shape | Required result |
| --- | --- | --- |
| Stream faults | malformed, guessed, no-tool, error, missing-finish, and length-truncated streams | no invalid result accepted |
| Fallback | 32 repeated invalid-primary / valid-secondary sequences | bounded retry and only valid secondary acceptance |
| Write contention | 12 simultaneous lock contenders for one workdir | exactly one owner, 11 blocked |
| Job isolation | 24 concurrent `launch` operations | unique job IDs and metadata/stdout/stderr paths |
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

The included runner is sequential by design and creates one disposable directory per job:

```bash
python3 scripts/live_soak.py --confirm-live --provider sensenova --role repository-exploration \
  --runs 100 --report reports/sensenova-read.json
python3 scripts/live_soak.py --confirm-live --provider sensenova1 --role documentation \
  --runs 100 --report reports/sensenova1-write.json
```

Its report contains aggregates only. A nonzero exit means at least one run did not complete.

For a concurrency experiment, keep write tasks pointed at one workdir and verify all but one return `blocked`; use separate disposable workdirs for read-only parallelism. Record the exact concurrency level, start method, status counts, p50/p95 duration, stream failure reasons, fallback count, and partial-write count. Do not publish a concurrency guarantee beyond the highest fully observed level.

Record only: timestamp, Worker version, role, provider route, status, duration, stream finish
reason, retry/fallback flags, and redacted aggregate usage. Do not publish prompts, workspace paths,
request bodies, response bodies, thread ids, credentials, or raw logs.

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
