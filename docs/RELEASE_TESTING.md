# Release Testing

`deepseek-worker` has two deliberately separate test planes.

## Offline Release Gate

Run this locally and in CI for every pull request and release:

```bash
make release-gate
```

The gate is deterministic and uses no network, model account, API key, task body, or real project.
It must pass completely. It verifies:

- Python compilation and the full unit suite.
- Strict five-field result contracts and rejection of malformed, guessed, no-tool, stream-error,
  missing-finish, and length-truncated results.
- Bounded two-provider fallback under repeated bad primary responses.
- Per-workdir write lock contention: one owner and all competing writers blocked.
- Concurrent `launch -> poll` job identity and output-file isolation.
- Run metadata excludes task text and secret-like result content.
- Real workspace changes stop automatic retry and Provider fallback.
- Native canaries remain evidence-only and must never promote a production route automatically.

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
