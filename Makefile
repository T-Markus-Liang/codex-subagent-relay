.PHONY: audit test release-gate install-local

PYTHON ?= python3.11

audit:
	$(PYTHON) -m py_compile deepseek-worker tests/test_worker.py tests/test_release_gate.py

test:
	$(PYTHON) -m unittest discover -s tests -v

release-gate: audit test

install-local:
	chmod +x deepseek-worker
	mkdir -p "$(HOME)/.local/bin"
	ln -sfn "$(abspath deepseek-worker)" "$(HOME)/.local/bin/deepseek-worker"
	@echo "Installed to $(HOME)/.local/bin/deepseek-worker. Add $(HOME)/.local/bin to PATH if needed."
