# Test and run results (actual output, 2026-10-17, Windows 11)

## Automated tests
| Interpreter | Command | Result |
|---|---|---|
| Python 3.12.10 | `python -m pytest -o addopts="" -q -rs` | **127 passed, 4 skipped** (316.5 s) |
| Python 3.11.17 (uv-installed, separate venv) | same | **127 passed, 4 skipped** (325.9 s) |
| Python 3.12.10 | `python -m pytest -o addopts="" -q -m live --run-live tests/live` | **3 passed, 1 skipped** (SEC, GLEIF, OpenFIGI live OK; LLM skipped: no `ANTHROPIC_API_KEY` = NOT RUN) |
The 4 default skips are the live tests, reported as skipped (never as passed). Marker subsets: `-m unit`, `-m integration`, `-m e2e`, `-m live --run-live`.

## Live ingestion (real data, SEC UA from local .env)
- 5-company sample first (AAPL, MSFT, JPM, KO, XOM): 1,052 observations; validation **blocked** on XOM (SEC maps the ticker to CIK 2115436, `ExxonMobil Holdings Corp`). XOM replaced by EOG (evidence DB kept: `data/reference_5co_xom_finding.duckdb`).
- Full 40 companies: 9,127 observations, later 9,373 after adding `RevenuesNetOfInterestExpense` (offline re-normalisation of cached snapshots); 0 failures; `python -m src.ingestion.verify`: 0 integrity problems.
- Quality: 1,004 findings before -> 64 after (0 confirmed errors). See QUALITY_FINDINGS_AUDIT.md.
- Independent filing check: 12 observations / 8 companies, 24/24 rows located in the filing text (1 weak context). See FINANCIAL_DATA_VALIDATION.md.
- Identity: 25 VERIFIED, 9 CANDIDATE_UNIQUE, 4 AMBIGUOUS, 2 UNMATCHED. See IDENTITY_RESOLUTION_AUDIT.md.

## Evaluation (mock agents only; real LLM = NOT RUN)
| suite | agent | accuracy | detection recall | clean FPR | high-conf wrong |
|---|---|---|---|---|---|
| base 120 | mock-reference | 1.000 | 1.0 | 0.0 | 0 |
| base 120 | mock-degraded | 0.667 | 0.6 | 0.0 | 40 |
| multi 35 | mock-reference | 1.000 | 1.0 | 0.0 | 0 |
| multi 35 | mock-naive | 0.143 | 0.0 | 0.0 | 30 |
Final scorecard (superseded by the 2026-10-18 section below): `reports/scorecard.{json,html}` (run `run-20261017T181138Z-1c7e0c`, suite `v1-s42-n20-e9a56f67`, dataset snapshot `9255028dc22c0e5c`).
The reference mock encodes the defect taxonomy: these numbers validate the harness only.

## Regression: see REGRESSION_VALIDATION.md (fail exit 1, pass exit 0, incompatible exit 4).
## Dashboard: Streamlit AppTest ran `dashboard/app.py` without exceptions and rendered the metrics.

## NOT RUN
Real-LLM evaluation (smoke30 / full / multi) · live LLM judge · human-labelled judge sample · browser-interaction test of the dashboard · git commit tracking (folder is not a git repo).

## Clean-room check (Gate F)
The zip was extracted to an empty directory, a fresh Python 3.11.17 venv was created (`uv venv --python 3.11`), `requirements.txt` installed, then: doctor OK; synthetic DB -> validate (GATE OPEN) -> generate 120 + 35 -> mock evaluate -> baseline -> degraded run -> compare **exit 1** -> reports; `--agent llm` -> NOT RUN; `ingestion.run` without `SEC_USER_AGENT` -> actionable error; `-m unit` **92 passed**. This check found one defect (raw traceback on an offline cache miss), fixed with a CLI test (exit 3 / exit 2).

## Ollama / local-model work (2026-10-18)
- Stage A baseline before changes: **126 passed, 1 failed, 4 skipped**. The failure was environmental: the test read the developer's real `.env` (an `ANTHROPIC_API_KEY` was set), so `--agent llm` did not stop with NOT RUN. Fixed by isolating the test from `.env`.
- After changes: **158 passed, 4 skipped** (Python 3.12.10, 28 s; mocked providers only, no network, no model).
- Quality gate re-run: 66 findings at first (UNH net income/assets reported missing by DQ011 and Merck's raw snapshot body missing - see docs/DATA_INCIDENT_20261018.md), 64 after repair; 0 confirmed errors; GATE OPEN. Remaining: DQ006 x1 (SLB renamed), DQ007 x6 (4 ambiguous, 2 unmatched identity), DQ009 x57 (restatement warnings).
- Mock (smoke 24 and full 120): 1.000 accuracy; naive mock now 0.000 (it cites no store evidence); degraded mock 0.667 and regression check exit 1; restored exit 0.
- REAL Qwen results: docs/LOCAL_LLM_QWEN.md (smoke 24 cases: 7/24 strict accuracy).
