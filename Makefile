.PHONY: audit test release-gate install-local plugin-gate

PYTHON ?= python3.11

audit:
	$(PYTHON) -m py_compile deepseek-worker relay_runtime/job_store.py scripts/live_soak.py scripts/operational_report.py scripts/operations_dashboard.py scripts/qualify_manifest.py plugins/codex-subagent-relay/mcp/server.py scripts/validate_compatibility.py scripts/validate_plugin_package.py tests/test_worker.py tests/test_release_gate.py tests/test_install.py tests/test_live_soak.py tests/test_operational_report.py tests/test_operations_dashboard.py tests/test_mcp.py tests/test_compatibility.py tests/test_marketplace.py tests/test_qualify_manifest.py

test:
	$(PYTHON) -m unittest discover -s tests -v

plugin-gate:
	$(PYTHON) scripts/validate_plugin_package.py

release-gate: audit test plugin-gate
	$(PYTHON) scripts/validate_compatibility.py

install-local:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or later is required"'
	mkdir -p "$(HOME)/.local/bin"
	@target="$(HOME)/.local/bin/deepseek-worker"; temporary="$$target.tmp.$$"; \
	printf '%s\n' '#!/bin/sh' 'exec "$(shell command -v $(PYTHON))" "$(abspath deepseek-worker)" "$$@"' > "$$temporary"; \
	chmod 755 "$$temporary"; mv -f "$$temporary" "$$target"
	@echo "Installed a Python 3.11 launcher to $(HOME)/.local/bin/deepseek-worker. Add $(HOME)/.local/bin to PATH if needed."
