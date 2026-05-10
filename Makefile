.PHONY: install test lint typecheck dry-run clean

install:
	pip install -e ".[dev]"

test:
	pytest tests/unit -v

test-all:
	pytest tests/ -v

test-cov:
	pytest tests/unit --cov=src/squant --cov-report=html

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

typecheck:
	mypy src/squant

dry-run:
	python -m squant.main --dry-run

bootstrap-sheet:
	python scripts/bootstrap_sheet.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	rm -rf .coverage htmlcov .mypy_cache .pytest_cache .ruff_cache
