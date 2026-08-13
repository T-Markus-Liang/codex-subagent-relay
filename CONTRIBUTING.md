# Contributing

Please keep changes narrowly scoped and preserve the safety invariants.

1. Do not commit provider credentials, task bodies, raw model responses, local run logs, or live soak reports.
2. Run `make release-gate` before opening a pull request.
3. Add deterministic tests for changes to parsing, retries, fallback, locking, job isolation, or redaction.
4. Treat native Codex subagent support as experimental unless the documented canaries pass repeatedly.

Live Provider tests are opt-in and must never run in CI. Follow `docs/RELEASE_TESTING.md` when testing them.
