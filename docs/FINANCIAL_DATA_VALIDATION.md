# Financial data validation (independent of the ingestion output)

**Method.** `scripts/verify_against_filings.py` takes stored observations, fetches the *actual filing document* named by the stored
accession number (`/Archives/edgar/data/<cik>/<accession>/<primary .htm>` - not the XBRL API), strips the HTML and searches for the value
printed in the filing's own scale next to a concept-specific label. The matched snippet is stored in `docs/evidence/filing_verification.json`.
Each observation was checked twice: the **original report** (the filing for that fiscal year) and the currently **selected report**
(latest filing). Run date 2026-10-17; filings downloaded to `data/raw/filings` (SHA-256 recorded in the JSON).

**What this proves / does not prove.** It proves the SEC filing text prints that number next to that label, which is independent of the
companyfacts JSON that the pipeline ingested. It does *not* mechanically prove the period column: the snippet is recorded so a reviewer can
see it, and in every case below the matched number is the first (current-year) column of the table or an explicit "...were $X million in 2022".
Verification used the filings themselves, not recollection and not the ingestion output.

## Sample: 12 observations, 8 companies (5 non-calendar fiscal years)

| # | Company (FY end) | Concept | Period end | Stored value (USD) | Original report | Evidence in filing text | Result |
|---|---|---|---|---|---|---|---|
| 1 | Apple (Sept) | revenue | 2023-09-30 | 383,285,000,000 | 0000320193-23-000106 (10-K) | "Total net sales $ 383,285 (3)% $ 394,328 8% ..." | VERIFIED |
| 2 | Apple (Sept) | total assets | 2024-09-28 | 364,980,000,000 | 0000320193-24-000123 | "Total assets $ 364,980 $ 352,583" | VERIFIED |
| 3 | Microsoft (June) | net income | 2024-06-30 | 88,136,000,000 | 0000950170-24-087843 | "Net income 88,136 72,361 22%" | VERIFIED |
| 4 | Microsoft (June) | revenue | 2023-06-30 | 211,915,000,000 | 0000950170-23-035122 | "Revenue $ 211,915 $ 198,270 7%" | VERIFIED |
| 5 | Walmart (Jan) | revenue | 2025-01-31 | 680,985,000,000 | 0000104169-25-000021 | "Total revenues 680,985 648,125 611,289" | VERIFIED |
| 6 | Walmart (Jan) | net income | 2024-01-31 | 15,511,000,000 | 0000104169-24-000056 | "Consolidated net income attributable to Walmart $ 15,511 $ 11,680 $ 13,673" | VERIFIED (attributable-to-parent measure = `NetIncomeLoss`) |
| 7 | Costco (Aug/Sep) | revenue | 2023-09-03 | 242,290,000,000 | 0000909832-23-000042 | "Total revenue 242,290 226,954 195,929" (53-week year) | VERIFIED |
| 8 | Nike (May) | net income | 2024-05-31 | 5,700,000,000 | 0000320187-24-000044 | "Net income $ 5,700 $ 5,070 $ 6,046" | VERIFIED |
| 9 | Nike (May) | total assets | 2023-05-31 | 37,531,000,000 | 0000320187-23-000039 | "TOTAL ASSETS $ 37,531 $ 40,321" | VERIFIED |
| 10 | Coca-Cola (Dec) | revenue | 2022-12-31 | 43,004,000,000 | 0000021344-23-000011 | "Net operating revenues were $43,004 million in 2022, compared to $38,655 million" | VERIFIED |
| 11 | JPMorgan (Dec) | net income | 2023-12-31 | 49,552,000,000 | 0000019617-24-000225 | "Net income $ 49,552 $ 37,676 $ 48,334" | VERIFIED |
| 12 | Johnson & Johnson (Dec) | revenue | 2023-12-31 | 85,159,000,000 | 0000200406-24-000013 | number found in a segment table ("...Segment earnings before tax 22,915 20,094 85,159 79,990...") - label context is weak | **PARTIALLY VERIFIED**: value printed in the filing, line label not shown in the snippet; needs a human look at the income-statement page |

For every row the **selected (latest) report** value is identical to the original and was also found in its own (later) filing - no
restatement discrepancy in this sample (24/24 rows `FOUND_IN_FILING`). Units: all stored in USD; filings print in millions - the scaling step was applied by the script and recorded as `filing_scale`.

**Acceptance:** 11 of 12 independently verified from filing text; 1 (JNJ) explicitly classified as partially verified. Not claimed: complete verification of the 9,373-observation store - this is a sample.

## Pipeline checks (audit of ingestion code)
| Check | Status | Evidence |
|---|---|---|
| CIK/ticker/name vs SEC `company_tickers.json` | VERIFIED | DQ006; caught XOM (SEC now lists CIK 2115436 `ExxonMobil Holdings Corp`); McDonald's apostrophe normalisation bug found and fixed |
| Revenue / net income / total assets mapping | VERIFIED for 40 filers | all enabled concepts present after adding `RevenuesNetOfInterestExpense` for GS/MS (DQ011 2 -> 0). Bank "revenue" = net revenue, a different measure from product revenue |
| Competing tags | VERIFIED | tag precedence first (`NetIncomeLoss` before `ProfitLoss`; `Revenues` before `RevenueFromContract...`); 1,228 period groups with different-measure tags resolved by precedence, 301 equivalent-tag groups counted not reported |
| Units / currency | VERIFIED | stored unit kept (`USD`); DQ003/DQ004 + tests; no conversion layer beyond USD scales |
| Annual vs quarterly vs YTD | VERIFIED (tests + data) | duration 350-380 d annual, 80-100 d standalone quarter, other (6/9-month YTD) excluded and counted; balance-sheet instants typed by form |
| Duplicates / restatements / amendments | VERIFIED | all reports kept; one selected per period; restatements aggregated into DQ009 warnings; `amended` flag from `/A` forms |
| Missing observations | VERIFIED | never zero-filled; DQ011 lists missing concepts |
| Unresolved | OPEN | WFC quarterly net income 2020-Q2/Q3 differs by ~60% between the original 10-Q and the 2021 comparatives (DQ009 medium). Not resolved here: needs a manual filing check. |

Limits: the JSON is *not* a re-implementation of XBRL; dimensional/segment facts are absent from companyfacts; fiscal-year labels are period-end-year based.
