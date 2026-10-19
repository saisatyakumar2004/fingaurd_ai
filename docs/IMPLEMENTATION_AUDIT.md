> **Superseded in part** by [FINAL_GAP_AUDIT.md](FINAL_GAP_AUDIT.md) (2026-10-17, second audit). The table below is the first audit and is kept for history.

# Implementation audit

Status key: VERIFIED (executed, evidence below) · PARTIALLY VERIFIED · NOT IMPLEMENTED · BLOCKED · NOT APPLICABLE.
Environment: Windows 11, Python 3.12.10 (3.11 not installed on this machine; 3.14 and 3.13 also present but untested), duckdb 1.5.6, pydantic 2.14.1, requests 2.34.2, PyYAML 6.0.3, pytest 9.1.1.
Audit date: 2026-10-17. The code was first *written* without execution; this audit reflects what was executed afterwards.

| # | Capability | Status | Evidence | Gap / change made | Verification command |
|---|---|---|---|---|---|
| 1 | Environment / dependencies | VERIFIED (3.12 only) | venv created, `pip install -r requirements.txt`, `src.common.doctor` OK | added `doctor`; 3.11 itself NOT tested | `python -m src.common.doctor` |
| 2 | Offline unit tests | VERIFIED | 85 passed, 4 skipped (live) | 2 wrong test expectations fixed (resolver) | `python -m pytest -o addopts=""` |
| 3 | SEC ingestion (live) | VERIFIED for 40 companies | 40 ingested, 9,127 observations, 0 failures, 91 snapshots, 0 integrity problems | UA now validated; placeholder rejected | `python -m src.ingestion.run` |
| 4 | SEC value correctness | PARTIALLY VERIFIED | AAPL FY2022-24 revenue/net income/total assets match Apple's published 10-K figures as I know them (e.g. FY2023 revenue 383,285 M, net income 96,995 M, assets 352,583 M); only AAPL spot-checked, from memory, not against the filing PDFs | other companies unchecked | inspect with `scripts/inspect_companyfacts.py` |
| 5 | XBRL tag mapping | PARTIALLY VERIFIED | live run: all 40 companies produced revenue/net income/assets except those listed by DQ011 (2 gaps) | alt-tag list works for observed filers; not exhaustive | `python -m src.quality.validate` |
| 6 | Universe verification | VERIFIED | live check caught **XOM**: SEC ticker file maps XOM to CIK 2115436 (ExxonMobil Holdings Corp) vs configured 34088 -> DQ006 critical, gate BLOCKED | XOM replaced by EOG with an explanatory comment; successor/predecessor modelling NOT IMPLEMENTED | `python -m src.quality.validate` |
| 7 | GLEIF adapter | VERIFIED live (schema) | real response inspected: keys `meta,links,data`; `attributes.entity.legalName/jurisdiction/status` present; "Apple Inc." -> 10 candidates | statuses are CANDIDATE_UNIQUE (name-only). VERIFIED needs an independent expected LEI (none configured) | `pytest -m live --run-live` |
| 8 | OpenFIGI adapter | VERIFIED live (schema) | AAPL -> matched, 1 row (FIGI = composite BBG000B9XRY4, share class BBG001S5N8V8); fake ticker -> unmatched preserved | anonymous rate limit only | same |
| 9 | Identity statuses VERIFIED/AMBIGUOUS/UNMATCHED/SOURCE_UNAVAILABLE | PARTIALLY VERIFIED | resolver + tests with deterministic synthetic fixtures (2 incorrect mappings, ambiguous, unmatched); SOURCE_UNAVAILABLE written on fetch failure but only code-path reviewed, no failure test | no `VERIFIED` status reached on real data | `pytest tests/integration/test_leakage_and_defects.py` |
| 10 | Quality rules DQ001-013 + gate | VERIFIED | each rule triggered by injected defects in tests; gate blocked on real XOM data, open after fix | 1,004 needs-review items on real data (many DQ002/DQ009 are redundant/competing tags) - noisy but not blocking | `python -m src.quality.validate` |
| 11 | Corruption engine (6 categories x 20) | VERIFIED | 120 cases deterministic across runs; per-case defect-exhibits test; reversibility test | invalid config now raises ValueError | `python -m src.corruption.generate --seed 42` |
| 12 | Leakage prevention | VERIFIED (automated tests) | tool whitelist, injection payloads, excluded periods invisible, closed input schema | agent is in-process: isolation is by API design, not OS sandbox | `pytest tests/integration/test_leakage_and_defects.py` |
| 13 | Mock agents | VERIFIED | reference 120/120 (by construction - rule-based, knows the defect taxonomy), naive 20/120, degraded 80/120 | mock scores prove the harness, not any LLM | `python -m src.evaluation.run --agent mock` |
| 14 | Real LLM agent | NOT RUN | no `ANTHROPIC_API_KEY`; adapter has retries/timeouts/usage/cache code, unexecuted against a provider | | `python -m src.evaluation.run --agent llm` |
| 15 | LLM-as-judge | NOT IMPLEMENTED (scaffold only) | rubric version + agreement report function; no judge, no human labels | | |
| 16 | Metrics | VERIFIED | unit tests with hand-computed values (P/R/F1, FPR, Brier, ECE, kappa); added per-category P/R/F1, incorrect-abstention rate, evidence completeness | | `pytest tests/unit` |
| 17 | Regression | VERIFIED | baseline -> degraded agent -> exit 1 (accuracy -33.3 pts, critical categories) -> restored agent -> exit 0; baseline now refused (exit 4) if suite/dataset/case-set differ | | see docs/TEST_RESULTS.md |
| 18 | Reports | VERIFIED | `scorecard.json/html`, `regression_diff.json/html` generated from stored results | calibration meaningless for mock (confidence is rule-assigned) | `python -m src.reporting.generate` |
| 19 | Security | PARTIALLY VERIFIED | `.env` gitignored; UA validated, tests assert header on every SEC request; keys never logged by code (not independently audited) | | `pytest tests/unit/test_sec_user_agent.py` |
| 20 | Streamlit dashboard | NOT RUN | `dashboard/app.py` not executed (streamlit not installed) | | |
| 21 | Windows setup | VERIFIED on 3.12 | commands in README executed | 3.11 untested | README "First run" |
