# Quality findings audit

**Fixed dataset snapshot.** 40 companies, SEC raw snapshots unchanged; `data/reference_snapshot_pre_audit2.duckdb` is the frozen "before" database
(9,127 observations, validation run `val-20261017T164843Z-8d1ae3`). The "after" database re-normalised the same cached snapshots
(9,373 observations: +246 bank `RevenuesNetOfInterestExpense` facts for GS/MS; no new downloads).

## Why there were 1,004 findings
| Rule | Count | Cause | Real? |
|---|---|---|---|
| DQ002 low | 283 | same value reported under two XBRL tags in one filing (e.g. `Revenues` and `RevenueFromContract...`, `NetIncomeLoss` and `ProfitLoss`) | no - equivalent observations |
| DQ009 medium | 601 | grouping mixed tags: `NetIncomeLoss` (attributable to parent) vs `ProfitLoss` (incl. noncontrolling interest) and `Revenues` vs `RevenueFromContract...` are *different measures*; also grouped by period across filings | mostly no |
| DQ009 low | 102 | genuine value changes between filings (recasts for spin-offs, small revisions) | yes (warning) |
| DQ010 | 9 | year-over-year ratio test applied to net income | no - net income legitimately swings (e.g. NVDA 4.4B->29.8B, PFE 31.4B->2.1B, MRK 14.5B->0.4B) |
| DQ006 low | 2 | McDonald's apostrophe normalisation bug; SLB renamed (valid) | 1 bug, 1 valid |
| DQ007 | 5 | GLEIF ambiguity / unmatched | yes |
| DQ011 | 2 | GS/MS have no `Revenues` tag (use `RevenuesNetOfInterestExpense`) | yes - mapping gap |

## Changes (none lowers thresholds or mutes a rule)
1. **Concept precedence** (`select_observations`): lowest tag priority present for a period is selected first, *then* latest filing. Before, a later-filed `ProfitLoss` could beat an earlier `NetIncomeLoss` (mixing measures in the golden store).
2. **Unit/tag-aware duplicates**: DQ002 only fires for the *same tag* repeated (confirmed error). Equivalent multi-tag groups are counted in the validation summary (`not_reported_by_design`: 301) - suppression is auditable, not silent.
3. **Provenance-aware conflicts** (DQ009): compared within one tag only. Same filing + same tag + different value = confirmed error (high). Value changed between filings = *one* aggregated warning per company/concept listing every restated period (medium if max change >= 5%).
4. **Status taxonomy**: `confirmed_error`, `warning` (restatement), `ambiguous` (identity), `needs_review`.
5. **DQ010** now covers revenue and total assets (level concepts); net income is excluded because a ratio of differences is not a sanity test.
6. **Mapping**: `RevenuesNetOfInterestExpense` added as last-priority revenue tag (banks).
7. **Name normalisation** drops apostrophes.
8. **Stable ids**: `issue_id = hash(rule, record, observed)`; validation-run ids are microsecond-sortable. Test: `test_issue_ids_are_stable_across_runs`.

## Manual review of 49 original findings (stratified, seed 7; `docs/evidence/quality_review_sample.csv`)
| Label | n |
|---|---|
| false positive | 31 (DQ002 12/12, DQ009-medium 14/16, DQ010 4/4, DQ006 1/2) |
| valid finding | 18 (DQ009-medium 2, DQ009-low 8/8, DQ007 5/5, DQ006 1, DQ011 2/2) |
| confirmed data errors | **0** |
| unresolved | 0 in sample |

Precision of the original findings in this sample: **18/49 = 36.7%**. Fewer than 50 were sampled (49); the "after" set (64) was reviewed by structure (below).
Reviewer note: DQ010 labels rely on my knowledge that those swings are real; they were not each re-verified against filings.

## After (same snapshot)
| | before | after |
|---|---|---|
| total findings | 1,004 | **64** (0 confirmed errors) |
| by class | 1,004 needs_review | 57 warning (restatements), 4 ambiguous, 3 needs_review |
| DQ002 / DQ010 / DQ011 | 283 / 9 / 2 | 0 / 0 / 0 |
| duplicates removed by grouping | - | 301 equivalent-tag groups not reported; 1,228 different-measure groups resolved by precedence; 703 per-period DQ009 rows collapsed into 57 per-company/concept warnings |
| attributable to rule defects | 31/49 sampled | 0 of the reviewed categories |

Intermediate step (new rules, old selection) = 60 findings (52 warnings, 1 name, 5 identity, 2 DQ011). After re-normalisation and the identity re-run = 64: +5 restatement warnings (the added bank facts restate too), -2 DQ011 (mapping gap closed), +1 identity ambiguity (the SEC-profile corroboration step turned one candidate into AMBIGUOUS).
Post-change findings are by construction: same-tag value changes between filings (restatements), identity ambiguity, one name mismatch. The 8/8 DQ009-low and 2/2 DQ009-medium valid restatements in the sample are of this type, so the expected precision of the remaining set is high - but it was **not** reviewed item-by-item; claim "reduced false positives", not "zero".
Open item: WFC quarterly restatement of ~60% (Q2/Q3 2020) is unresolved and needs a manual filing check.

Reproduce: `python -m src.quality.validate --db data/reference_snapshot_pre_audit2.duckdb` (new rules on old selection) and `python -m src.quality.validate` (after).
