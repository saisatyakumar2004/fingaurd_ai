# Limitations and honest status

## Status (see docs/FINAL_GAP_AUDIT.md, docs/TEST_RESULTS.md)
- Verified by execution on Python 3.12 and 3.11: offline suite (127 passed / 4 live skipped), live SEC/GLEIF/OpenFIGI ingestion, quality gate, 120 + 35 case suites, mock evaluation, regression fail/pass/incompatible, reports, dashboard render.
- **NOT RUN:** real-LLM evaluation, live LLM judge, human-labelled judge sample.
- Financial values: 12 observations from 8 companies were checked against filing text (11 verified, JNJ weak context). The other ~9,360 observations are unaudited; the WFC quarterly restatement (~60%) is unresolved.
- Quality: 64 findings remain (57 restatement warnings, 4 ambiguous identities, 3 review); not all were reviewed item by item.
- Identity: VERIFIED means GLEIF and SEC agree on name, state of incorporation and city - not a legal proof. 9 stay CANDIDATE_UNIQUE, 4 AMBIGUOUS, 2 UNMATCHED.
- XOM was replaced by EOG (successor-entity modelling not implemented). Bank "revenue" is net revenue (a different measure).
- The mock reference agent encodes the defect taxonomy (and was adjusted during this work); its 100% is not evidence of LLM quality.
- Not a git repository, so run records carry `git_commit = null`.
- Not production-ready: no operational monitoring, concurrency, load or security review.

## By design
- Mock-agent scores validate the **harness**. They say nothing about any LLM. A real model must be run via `--agent llm`.
- Corruptions are synthetic and single-fault; real data errors are messier. Metrics do not estimate real-world error rates.
- Agent confidence is self-reported and uncalibrated; the calibration section reports empirical behaviour only.
- Fiscal-period labels are period-end-year based (`FY2023` = period ends in 2023). Companies with 52/53-week or
  non-calendar years may be named differently by themselves.
- Only annual USD observations are used for corruption; quarterly data is stored but not yet tested.
- Explanation quality uses a deterministic rubric (evidence relevance, factual consistency, completeness). No LLM judge
  is wired in; `cohens_kappa` exists for comparing a future judge to human labels, but no human labels exist yet.
- Validation freshness is a fingerprint (row counts + sum of values); it is a staleness guard, not a tamper check.
- The OpenFIGI step maps `TICKER` + exchange `US`; dual-class and multi-listing cases are surfaced for review, not resolved.
- No currency conversion; non-USD facts are flagged only.

## Suggested next steps
1. Run the offline tests and fix whatever breaks. 2. Run online ingestion once; review DQ006/DQ011 output and tag coverage.
3. Add human-labelled explanation samples before introducing an LLM judge. 4. Extend corruption to quarterly data and multi-fault cases.
