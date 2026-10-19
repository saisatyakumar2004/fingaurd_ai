# Convenience targets. On Windows without `make`, use scripts/run_pipeline.ps1 instead.
PY ?= python

setup:
	$(PY) -m venv .venv
	.venv/Scripts/python -m pip install -r requirements.txt

test:
	$(PY) -m pytest

ingest:
	$(PY) -m src.ingestion.run

ingest-offline:
	$(PY) -m src.ingestion.run --offline

validate:
	$(PY) -m src.quality.validate

corrupt:
	$(PY) -m src.corruption.generate --seed 42

evaluate-smoke:
	$(PY) -m src.evaluation.run --mode smoke --agent mock

evaluate-full:
	$(PY) -m src.evaluation.run --mode full --agent mock

report:
	$(PY) -m src.reporting.generate

baseline:
	$(PY) -m src.regression.compare --set-baseline

regression:
	$(PY) -m src.regression.compare

.PHONY: setup test ingest ingest-offline validate corrupt evaluate-smoke evaluate-full report baseline regression
