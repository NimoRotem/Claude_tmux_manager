.PHONY: test test-fast coverage check lint lint-fix run run-dev backup backup-data restore-check install help

PORT ?= 8501

help:
	@echo "tmux Dashboard — common development commands"
	@echo ""
	@echo "  make test          Run full test suite (verbose)"
	@echo "  make test-fast     Run tests without coverage (faster)"
	@echo "  make coverage      Run tests and open HTML coverage report"
	@echo "  make check         Run lint then full test suite"
	@echo "  make lint          Check code with ruff"
	@echo "  make lint-fix      Auto-fix ruff violations"
	@echo "  make run           Start server (requires TMUX_DASH_PASS)"
	@echo "  make run-dev       Start server without auth (dev only)"
	@echo "  make backup        Create timestamped backup of app.py"
	@echo "  make backup-data   Backup persistent data in ~/.tmux-dashboard/"
	@echo "  make restore-check Verify persistent data files are readable JSON"
	@echo "  make install       Install Python dependencies"
	@echo ""
	@echo "Environment variables:"
	@echo "  PORT               Server port (default: 8501)"
	@echo "  TMUX_DASH_PASS     Login password"
	@echo "  TMUX_DASH_SECRET   HMAC secret for auth tokens"
	@echo "  OPENAI_API_KEY     OpenAI key for LLM summaries"

test:
	python3 -m pytest test_app.py test_api.py test_runtime_control.py test_browser_mcp_lease_proxy.py -v

test-fast:
	python3 -m pytest test_app.py test_api.py test_runtime_control.py test_browser_mcp_lease_proxy.py -q

coverage:
	python3 -m pytest test_app.py test_api.py test_runtime_control.py test_browser_mcp_lease_proxy.py --cov=app --cov-report=term-missing --cov-report=html -q
	@echo "HTML report written to htmlcov/index.html"

check: lint test

lint:
	python3 -m ruff check app.py runtime_control.py browser_mcp_lease_proxy.py test_app.py test_api.py test_runtime_control.py test_browser_mcp_lease_proxy.py

lint-fix:
	python3 -m ruff check --fix app.py runtime_control.py browser_mcp_lease_proxy.py test_app.py test_api.py test_runtime_control.py test_browser_mcp_lease_proxy.py

run:
	@if [ -z "$(TMUX_DASH_PASS)" ]; then \
		echo "Warning: TMUX_DASH_PASS is not set — auth is DISABLED"; \
	fi
	PORT=$(PORT) python3 app.py

run-dev:
	@echo "Starting in dev mode — auth disabled"
	PORT=$(PORT) python3 app.py

backup:
	cp app.py app.py.bak-$$(date +%Y%m%d-%H%M%S)
	@echo "Backed up to app.py.bak-$$(date +%Y%m%d-%H%M%S)"

backup-data:
	@DATA_DIR=$$HOME/.tmux-dashboard; \
	DEST=$$HOME/.tmux-dashboard-backup-$$(date +%Y%m%d-%H%M%S); \
	if [ -d "$$DATA_DIR" ]; then \
		cp -r "$$DATA_DIR" "$$DEST"; \
		echo "Data backed up to $$DEST"; \
	else \
		echo "No data directory found at $$DATA_DIR"; \
	fi

restore-check:
	@DATA_DIR=$$HOME/.tmux-dashboard; \
	if [ ! -d "$$DATA_DIR" ]; then \
		echo "No data directory at $$DATA_DIR"; exit 0; \
	fi; \
	for f in $$DATA_DIR/*.json; do \
		[ -f "$$f" ] || continue; \
		python3 -c "import json,sys; json.load(open('$$f')); print('OK: $$f')" \
			|| echo "CORRUPT: $$f"; \
	done

install:
	pip3 install -r requirements.txt
