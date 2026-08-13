# Configuration

Codex Subagent Relay deliberately does not create Provider accounts, API keys, or proxy routes. Configure those outside this repository, then use `doctor` to prove the local contract is complete.

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

`healthy` is not a live task success guarantee. Before real work, run one disposable read-only smoke against each route you plan to use:

```bash
mkdir -p /tmp/codex-subagent-relay-smoke
deepseek-worker --json smoke-test --provider sensenova --workdir /tmp/codex-subagent-relay-smoke --no-record
```

That command consumes Provider quota. Delete the disposable directory when finished.

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DEEPSEEK_WORKER_OPENCODE_PATH` | OpenCode executable used by the external Worker | `~/.opencode/bin/opencode` |
| `DEEPSEEK_WORKER_CLIPROXY_URL` | CLIProxyAPI V2 canary endpoint only | `http://127.0.0.1:18317/v1` |
| `DEEPSEEK_WORKER_CLIPROXY_TOKEN` | CLIProxyAPI V2 canary token only | local canary placeholder |
| `DEEPSEEK_WORKER_CLIPROXY_ALIAS` | CLIProxyAPI V2 child model alias only | `gpt-5.6-luna` |
| `DEEPSEEK_WORKER_CLIPROXY_LOG_PATH` | CLIProxyAPI V2 canary request log | user-local CLIProxyAPI path |

The `CLIPROXY` variables affect only the experimental native V2 canary. They are not required for external `run` or `launch` execution.
