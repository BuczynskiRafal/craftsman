.PHONY: check lint typecheck test doctor install uninstall clean

PYTHON ?= python3

check: lint typecheck test doctor

lint:
	$(PYTHON) -m ruff check craftsman scripts tests

typecheck:
	@if $(PYTHON) -m mypy --version >/dev/null 2>&1; then \
		$(PYTHON) -m mypy --strict craftsman scripts tests; \
	elif command -v uvx >/dev/null 2>&1; then \
		uvx mypy --strict craftsman scripts tests; \
	elif [ -x "$$HOME/.local/bin/uvx" ]; then \
		"$$HOME/.local/bin/uvx" mypy --strict craftsman scripts tests; \
	else \
		echo "Error: mypy not found (install via 'pip install -e .[dev]' or 'uvx')" >&2; exit 1; \
	fi

test:
	$(PYTHON) -m unittest discover -s tests -v

doctor:
	$(PYTHON) -m craftsman.cli doctor

install:
	./install.sh --all

uninstall:
	$(PYTHON) -m craftsman.cli uninstall

clean:
	rm -rf build dist *.egg-info .mypy_cache .pytest_cache .ruff_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
