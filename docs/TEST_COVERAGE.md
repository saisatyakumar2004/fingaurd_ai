# Test coverage

## Case suites (real reference data, seed 42)
| Suite | Cases | Version (this data) | Categories |
|---|---|---|---|
| base | 120 | `v1-s42-n20-e9a56f67` | SCALE_ERROR, ID_MISMAP, MISSING_DATA, CONTRADICTION, PERIOD_SHIFT, CLEAN_CONTROL x 20 |
| multi-fault | 35 | `multi-v1-s42-n5-978c9cb1` | MULTI_FAULT: 7 subtypes x 5 |

**Original 120 cases.** The earlier suite (`v1-s42-n20-a2690923`, produced on the previous selection rules) cannot be reproduced on the corrected data:
tag precedence and the extra bank facts change the candidate pool. Its files are archived untouched in `data/corrupted/archive_v1-s42-a2690923/` and the old
DB in `data/reference_snapshot_pre_audit2.duckdb`. The old regression baseline was therefore correctly **refused** (exit 4) and explicitly re-baselined.
On fixed data the generator is deterministic (same ids on repeat; tested) and seed-sensitive.

## Multi-fault subtypes (expected outcomes defined in `src/corruption/multi.py` from golden data, never from an agent)
| Subtype | Defect combination | Expected |
|---|---|---|
| wrong_company_with_other_companys_value | name/ticker of A, CIK **and value** of B | flagged `identifier_collision`, identity = A |
| correct_value_unsupported_currency | right number, unit `EUR` | flagged `unit_inconsistency`, USD value from store |
| quarter_value_labelled_annual | standalone-quarter figure under an annual period end | flagged `value_mismatch` |
| missing_fiscal_period | period metadata absent | `insufficient_evidence`, no value |
| restated_filing_versions | two filing versions (>1% apart) of one period | flagged `conflicting_sources`, latest filed value |
| unidentifiable_company_no_evidence | no CIK, unknown ticker/name **and** period absent from the store | abstain (`insufficient_evidence`/`ambiguous`) |
| duplicate_feed_equivalent | same correct record from two feeds | `verified` (control) |
Each case stores its rationale in `corruption_parameters.rationale`. A generator defect was found by running the reference agent: two
`quarter_value` cases used a quarterly figure equal (within 0.5%) to another year's annual value, i.e. a different defect; the generator now excludes those values.

## Automated tests (Python 3.12.10 and 3.11.17: **127 passed, 4 skipped**, skips = opt-in live suite)
- unit: units/tolerance, name matching, normalization & selection, HTTP cache/retry/Retry-After/refresh-never-overwrites/offline, adapter parsers, quality rules DQ001-013 (+precedence, equivalence, stable ids), identity states, SEC User-Agent, transforms reversibility, scoring/metrics/calibration/kappa, regression diff & baseline integrity, LLM adapter & judge with fake providers.
- integration: leakage (tool whitelist, injection payloads, excluded periods invisible via every tool, closed agent-input schema, no case ids/labels in tool output), "each case really exhibits its defect" for all categories, scale factors 1,000 and 1,000,000, golden data unchanged by generation, invalid config fails clearly, multi-fault suite.
- e2e (offline, synthetic DB): validate -> generate (120) -> reference vs naive vs degraded -> scorecard -> baseline -> regression fail/pass.
- live (opt-in `--run-live`): SEC, GLEIF, OpenFIGI = 3 passed on 2026-10-17; LLM = skipped (NOT RUN).
Markers: `unit`, `integration`, `e2e`, `live`.

Caveat: the reference mock agent is rule-based and was adjusted while building this suite (period-metadata check), so its 100% is not independent evidence of difficulty or of LLM quality.
