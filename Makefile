.PHONY: audit test release-gate install-local

PYTHON ?= python3.11

audit:
	$(PYTHON) -m py_compile deepseek-worker tests/test_worker.py tests/test_release_gate.py tests/test_install.py

test:
	$(PYTHON) -m unittest discover -s tests -v

release-gate: audit test

install-local:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or later is required"'
	mkdir -p "$(HOME)/.local/bin"
	@printf '%s\n' '#!/bin/sh' 'exec "$(shell command -v $(PYTHON))" "$(abspath deepseek-worker)" "$$@"' > "$(HOME)/.local/bin/deepseek-worker"
	chmod 755 "$(HOME)/.local/bin/deepseek-worker"
	@echo "Installed a Python 3.11 launcher to $(HOME)/.local/bin/deepseek-worker. Add $(HOME)/.local/bin to PATH if needed."
