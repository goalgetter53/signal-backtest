PY ?= .venv/bin/python
.PHONY: install lint type test check universe collect summary demo power backtest

install:
	python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'

lint:
	$(PY) -m ruff check src tests && $(PY) -m ruff format --check src tests

type:
	$(PY) -m mypy

test:
	$(PY) -m pytest -q

check: lint type test

universe:
	$(PY) -m signal_backtest universe --limit 1000

collect:
	$(PY) -m signal_backtest collect

summary:
	$(PY) -m signal_backtest summary

demo:
	$(PY) -m signal_backtest demo

power:
	$(PY) -m signal_backtest power

# usage: make backtest OUTCOMES=../outbound-engine/exports/outcomes.csv
backtest:
	$(PY) -m signal_backtest backtest --outcomes $(OUTCOMES)
