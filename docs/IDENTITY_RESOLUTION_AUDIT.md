# Identity resolution audit

## States and rules (`src/identity/resolver.py`)
| State | Rule |
|---|---|
| `VERIFIED` | single ACTIVE exact-name GLEIF candidate (or exactly one of several) **and** independent evidence agrees: SEC submissions `stateOfIncorporation` equals GLEIF jurisdiction (`US-<state>`) **and** SEC business city is among GLEIF HQ/legal-address cities (and HQ region ends with the SEC state). An externally supplied `expected_lei` also qualifies. |
| `CANDIDATE_UNIQUE` | single exact-name ACTIVE candidate, no independent corroboration available (name-only) |
| `AMBIGUOUS` | several plausible candidates, only fuzzy/INACTIVE candidates, indistinguishable twins, or the SEC profile / expected LEI *contradicts* the candidate (possible incorrect mapping) |
| `UNMATCHED` | no plausible candidate |
| `SOURCE_UNAVAILABLE` | API unreachable / rate-limited past the retry budget; nothing fabricated |
A fuzzy name match is never selected. Entities (CIK/LEI) and instruments (FIGI composite / share class) live in separate identifier levels; FIGIs are registered only when exactly one common-stock share class and composite are returned.

## Live results (40 companies, run 2026-10-17)
GLEIF + SEC submissions corroboration: **25 VERIFIED, 9 CANDIDATE_UNIQUE, 4 AMBIGUOUS, 2 UNMATCHED**.
- CANDIDATE_UNIQUE (BA, HON, INTC, MA, MMM, ORCL, PEP, UNH, WMT): one exact-name candidate but corroboration did not succeed (SEC state missing for ORCL; for the others state/city agreement was not reached - causes not individually inspected) - not promoted.
- AMBIGUOUS: ABT (5 exact-name entities, none corroborated), CSCO, UPS (2, none corroborated), COP (2 candidates both match jurisdiction and address - indistinguishable).
- UNMATCHED: CVX, SLB (legal name differs from the queried names even after retrying with the SEC registered name).
- Spot consistency: AAPL `HWUPKR0MPOU8FGXBT394`, MSFT `INR2EJN1ERAN0W5ZP974`, JPM `8I5DZWZKVSZI1NUHU748` agree with my recollection of their published LEIs; recollection is only a plausibility check, not the verification basis.
- OpenFIGI (anonymous): 5/5 sample mapped, one common-stock share class each. The rest of the universe was mapped in the full run.
Previous run (name-only): 0 VERIFIED; 35 CANDIDATE_UNIQUE, 3 AMBIGUOUS, 2 UNMATCHED.

## Deterministic tests (`tests/unit/test_identity_resolution.py`, `tests/integration/test_leakage_and_defects.py`) - 11 + 3, all synthetic fixtures
Correct mapping (needs SEC corroboration), incorrect mapping (contradicting jurisdiction / wrong expected LEI), similar names (Delta Air Lines vs Delta Apparel), same-group different legal entities (parent vs subsidiary resolved by address; twins stay ambiguous), multiple share classes (no FIGI registered, all rows kept), unmatched instrument kept as `unmatched`, unmatched/inactive entity, missing-data corroboration never `ok`, API failure / HTTP 429 / HTTP 503 -> `SOURCE_UNAVAILABLE` with no LEI written.

## Limits
Corroboration is two public sources agreeing on name, state and city - strong but not a legal proof; companies that moved HQ or whose GLEIF HQ city differs from SEC's business address stay CANDIDATE_UNIQUE. GLEIF has no CIK link, so no source maps CIK<->LEI directly.
