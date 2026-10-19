# Final gap audit (2026-10-17)

Baseline claims from the previous version were re-checked, not trusted. Status: VERIFIED / PARTIALLY VERIFIED / NOT IMPLEMENTED / NOT RUN / BLOCKED.
Environment: Windows 11; tested on **Python 3.12.10 and 3.11.17**; duckdb 1.5.6, pydantic 2.14.1, requests 2.34.2, PyYAML 6.0.3, pytest 9.1.1.
Note: `data/reference_snapshot_v1_prechange.duckdb` appeared in `data/` during this session and was not created by this work; it was left untouched and is not used.

| # | Requirement | Status | Files | Change / verification command | Actual outcome |
|---|---|---|---|---|---|
| 1 | Previous: 85 pass / 4 skip | superseded | tests/ | `python -m pytest -o addopts=""` | now **127 passed, 4 skipped** on 3.12 and on 3.11 |
| 2 | Independent financial verification (>=10 obs, >=5 cos) | VERIFIED (11/12), 1 PARTIAL | scripts/verify_against_filings.py, docs/FINANCIAL_DATA_VALIDATION.md | `python scripts/verify_against_filings.py` | 24/24 rows found in filing text; JNJ context weak -> partial; WFC quarterly restatement unresolved |
| 3 | Reduce false-positive findings | VERIFIED (measured) | src/quality/rules.py, validate.py, sec_normalize.py | `python -m src.quality.validate` | 1,004 -> 64, 0 confirmed errors; sample precision of original = 36.7% (49 reviewed); remaining 64 reviewed by structure only |
| 4 | Concept precedence / deterministic period selection | VERIFIED | sec_normalize.py | unit tests | tag precedence before filing date; mixed-measure selection bug fixed |
| 5 | Identity resolution states | VERIFIED (deterministic) / PARTIAL (live) | resolver.py, gleif.py, sec.py, run.py | `pytest tests/unit/test_identity_resolution.py` | live: 25 VERIFIED / 9 CANDIDATE_UNIQUE / 4 AMBIGUOUS / 2 UNMATCHED; VERIFIED = two sources agree on name+state+city |
| 6 | Real LLM evaluation | **NOT RUN** | llm_agent.py, llm_report.py | `python -m src.evaluation.run --agent llm --mode smoke30` | exit 5, report status NOT_RUN. Plumbing tested with fake providers (found and fixed a dead-code/recursion bug) |
| 7 | Multi-fault coverage (>=30) | VERIFIED | corruption/multi.py | `python -m src.corruption.generate --multi` | 35 cases, deterministic, reference mock 35/35, naive 5/35 |
| 8 | LLM-as-judge | PARTIALLY IMPLEMENTED / live NOT RUN | judge.py, judge_runner.py | fixture tests | interface only; >=30 human labels BLOCKED (no real explanations) |
| 9 | Regression & baseline integrity | VERIFIED | regression/compare.py | see REGRESSION_VALIDATION.md | fail exit 1 / pass exit 0 / incompatible exit 4 |
| 10 | Windows environment | VERIFIED on 3.12 and 3.11 | requirements.txt, doctor.py | `python -m src.common.doctor`; full pytest in both venvs | see TEST_RESULTS.md. Created with `uv` for 3.11 (no `py -3.11` launcher entry on this machine) |
| 11 | Reporting | VERIFIED | reporting/generate.py | `python -m src.reporting.generate` | counts, validation findings, execution, per-category P/R/F1, NOT-RUN vs zero distinction |
| 12 | Streamlit dashboard | VERIFIED (render only) | dashboard/app.py | streamlit AppTest + server start in isolated venv | no exception; metrics rendered from scorecard.json; browser interaction not tested |
| 13 | Live SEC ingestion | VERIFIED 2026-10-17 | ingestion/* | `python -m src.ingestion.run` | 40 companies, 9,373 observations, 0 failures, 0 snapshot integrity problems; live pytest 3 passed |
| 14 | XOM universe entry | RESOLVED by replacement | config/universe.yaml | DQ006 | XOM->EOG; successor-entity modelling NOT IMPLEMENTED |
| 15 | Leakage prevention | VERIFIED (tests) | tests/integration/test_leakage_and_defects.py | pytest | in-process isolation by API design, no OS sandbox |
| 16 | Production readiness | **NOT CLAIMED** | | | no load/ops/security review; local portfolio tool |
