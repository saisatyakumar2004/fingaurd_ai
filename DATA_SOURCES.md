# Data sources

**Status:** live calls to SEC (tickers, companyfacts, submissions, Archives filing documents), GLEIF (`lei-records`) and OpenFIGI (anonymous `v3/mapping`) were made on 2026-10-17 and the response shapes below were inspected in the cached files under `data/raw/`; the live pytest suite passed for all three. OpenFIGI rate limits remain as documented, not measured. Retrieval dates are recorded per snapshot in
`source_snapshots.retrieval_timestamp` (not hard-coded here).

| | SEC EDGAR | GLEIF | OpenFIGI |
|---|---|---|---|
| Role | primary financial ground truth | legal-entity identity (LEI) | instrument identity (FIGI) |
| Docs | https://www.sec.gov/edgar/sec-api-documentation | https://www.gleif.org/en/lei-data/gleif-api | https://www.openfigi.com/api/documentation |
| Endpoints | `https://www.sec.gov/files/company_tickers.json`<br>`https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json`<br>`https://data.sec.gov/submissions/CIK{10-digit}.json` (identity corroboration only)<br>`https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` (filing documents; verification script only) | `GET https://api.gleif.org/api/v1/lei-records` with `filter[entity.legalName]` (exact) or `filter[fulltext]`, `page[size]` | `POST https://api.openfigi.com/v3/mapping` |
| Auth | none, but a descriptive `User-Agent` with contact info is **required** (`SEC_USER_AGENT`) | none | optional `X-OPENFIGI-APIKEY` (`OPENFIGI_API_KEY`) |
| Rate limit | SEC fair-access policy: max 10 req/s. Default here: 1 request / 0.25 s (`sec.min_request_interval_seconds`) | not documented in this project; default 1 request / 0.5 s | anonymous ≈ 25 req/min and 10 jobs/request; with key ≈ 25 req/6 s and 100 jobs/request (**re-verify in docs**; configurable under `openfigi:`). HTTP 429 + `Retry-After` honoured |
| Caching | every response stored immutably in `data/raw`, newest used; `--refresh` adds a new snapshot only if content changed | same | same (cache key = hash of the job batch) |
| Terms / attribution | SEC public data; follow https://www.sec.gov/os/webmaster-faq#developers and fair-access policy | GLEIF LEI data is published under CC0; check current terms at gleif.org | check OpenFIGI terms of use and Bloomberg attribution requirements at openfigi.com before redistributing data |

## Field mappings

### SEC companyfacts → `financial_observations`
| Source | Column |
|---|---|
| `facts.us-gaap.<Tag>.units.<unit>[]` | `xbrl_tag`, `unit`, `raw_ref` |
| `start` / `end` | `fiscal_period_start` / `fiscal_period_end` (instants have no `start`) |
| `val` | `value` (stored in the reported unit; never converted on write) |
| `accn`, `form`, `filed` | `accession_number`, `form`, `filing_date` |
| `fy`, `fp` | `reported_fy`, `reported_fp` — **these describe the filing, not the fact's own period**; our `fiscal_period` label is derived from the period dates (`FY2023`, `2023-CQ1`) |
| response URL / hash / time | `source_url`, `snapshot_id`, `retrieval_timestamp` |

Concept → candidate tags (all present tags are stored, priority recorded; selection is deterministic: latest `filed`, then accession, then tag priority):
- `revenue`: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, `RevenuesNetOfInterestExpense` (banks; added after GS/MS had none of the others)
- Precedence: the first tag in this list that has a value for a period is used for that period; later filings only choose among reports of that same tag.
- `net_income`: `NetIncomeLoss`, `ProfitLoss`
- `total_assets`: `Assets` (instant)

Period handling: duration 350–380 days → `annual`; 80–100 days → `quarterly`; anything else (e.g. 6/9-month YTD) is excluded and counted in `ingestion_summary.json`. Instants take their type from the filing form (10-K → annual, 10-Q → quarterly). Forms kept: 10-K, 10-K/A, 10-Q, 10-Q/A. Periods before `sec.min_period_year` are dropped.

### GLEIF → `identity_resolutions`, `company_identifiers`
`data[].attributes.lei`, `.entity.legalName.name`, `.entity.jurisdiction`, `.entity.status`, `.entity.legalAddress.country`, `.registration.status`.
A unique exact normalized-name match on an ACTIVE entity is stored as `candidate_unique_name_match` — **a candidate, not proof**. Multiple plausible entities → `ambiguous`, nothing selected.

### OpenFIGI → `instrument_mappings`, `company_identifiers`
Job `{idType: TICKER, idValue, exchCode: US}`. Every returned row is stored (`figi`, `compositeFIGI`, `shareClassFIGI`, `securityType`, `securityType2`, `marketSector`…). `{"warning": ...}` stays `unmatched`; `{"error": ...}` stays `error`. A `FIGI`/`COMPOSITE_FIGI`/`SHARE_CLASS_FIGI` identifier is registered only when exactly one share class (and one composite) of *Common Stock* is returned; otherwise DQ008 raises it for review. A FIGI identifies an **instrument**, a CIK/LEI identifies an **entity** — `company_identifiers.level` keeps them apart.

## Known limitations of the sources
- The three systems do not cover identical entities; absence in one is recorded, not filled.
- `company_tickers.json` lists current tickers only. Delisted/renamed companies may be missing.
- Banks and some insurers/payment companies report revenue under other tags (or not as `Revenues`); DQ011 will list them.
- Restated values appear as several reports of one period; we keep all and select the latest filing (see DQ009).
- GLEIF legal names may differ from SEC names (punctuation, suffixes); name matching is heuristic.
- No currency conversion exists; non-USD facts are flagged (DQ004) and never converted.
