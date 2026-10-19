"""Deterministic data-quality rules.

Every issue carries: issue_id, rule_id, severity, classification, affected_record,
expected_condition, observed_condition, evidence.

classification
  confirmed_error : the data violates a hard invariant (blocks evaluation if severity is
                    listed in quality.block_on)
  needs_review    : suspicious or ambiguous; a human should look, but it is not proven wrong
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from src.common.db import fetch_dicts
from src.common.util import stable_id
from src.identity.resolver import name_similarity
from src.normalization.sec_normalize import CONCEPTS
from src.normalization.units import is_currency_code

CONFIRMED = "confirmed_error"
REVIEW = "needs_review"
WARNING = "warning"          # expected-but-notable data behaviour (e.g. restatement), not an error
AMBIGUOUS = "ambiguous"      # more than one plausible interpretation
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Issue:
    rule_id: str
    severity: str
    classification: str
    affected_record: str
    expected_condition: str
    observed_condition: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def issue_id(self) -> str:
        return stable_id(self.rule_id, self.affected_record, self.observed_condition)


def _iso(d: Any) -> Any:
    return d.isoformat() if isinstance(d, date) else d


# ---------------------------------------------------------------- DQ001
def dq001_missing_identifiers(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT company_id, ticker, name FROM companies "
                              "WHERE ticker IS NULL OR trim(ticker) = '' OR name IS NULL OR trim(name) = ''"):
        out.append(Issue("DQ001", "critical", CONFIRMED, r["company_id"], "company has ticker and name",
                         "ticker or name missing", r))
    for r in fetch_dicts(con, "SELECT c.company_id FROM companies c LEFT JOIN company_identifiers i "
                              "ON i.company_id = c.company_id AND i.id_type = 'CIK' WHERE i.company_id IS NULL"):
        out.append(Issue("DQ001", "critical", CONFIRMED, r["company_id"], "company has a CIK identifier row",
                         "no CIK identifier row", r))
    for r in fetch_dicts(con, "SELECT o.observation_id, o.company_id FROM financial_observations o "
                              "LEFT JOIN companies c ON c.company_id = o.company_id "
                              "WHERE o.identifier IS NULL OR trim(o.identifier) = '' OR c.company_id IS NULL"):
        out.append(Issue("DQ001", "critical", CONFIRMED, r["observation_id"],
                         "observation carries an identifier and references a known company",
                         "identifier missing or company unknown", r))
    return out


# ---------------------------------------------------------------- DQ002
def dq002_duplicates(con, cfg) -> list[Issue]:
    """Exact duplicates (same tag repeated for the same company/concept/period/filing) are errors.
    The same value reported under several XBRL tags is NOT a finding: it is an equivalent observation,
    resolved by tag precedence and counted by `equivalent_duplicate_stats` (visible in the validation summary)."""
    out: list[Issue] = []
    rows = fetch_dicts(con, """
        SELECT company_id, concept, period_type, fiscal_period_start, fiscal_period_end, accession_number, xbrl_tag,
               count(*) AS n, count(DISTINCT value) AS n_values, list(observation_id) AS ids
        FROM financial_observations
        GROUP BY 1, 2, 3, 4, 5, 6, 7 HAVING count(*) > 1""")
    for r in rows:
        key = f"{r['company_id']}/{r['concept']}/{r['period_type']}/{_iso(r['fiscal_period_end'])}/{r['accession_number']}/{r['xbrl_tag']}"
        out.append(Issue("DQ002", "medium", CONFIRMED, key, "one observation per (company, concept, tag, period, filing)",
                         f"{r['n']} rows for the same tag ({r['n_values']} distinct values)", {"observation_ids": r["ids"]}))
    return out


def equivalent_duplicate_stats(con) -> dict[str, int]:
    """Counts what DQ002/DQ009 deliberately do NOT report, so suppression is auditable."""
    same_value_other_tag = con.execute("""
        SELECT count(*) FROM (SELECT 1 FROM financial_observations
          GROUP BY company_id, concept, period_type, fiscal_period_start, fiscal_period_end, accession_number
          HAVING count(DISTINCT xbrl_tag) > 1 AND count(DISTINCT value) = 1)""").fetchone()[0]
    diff_value_other_tag = con.execute("""
        SELECT count(*) FROM (SELECT 1 FROM financial_observations
          GROUP BY company_id, concept, period_type, fiscal_period_start, fiscal_period_end, accession_number
          HAVING count(DISTINCT xbrl_tag) > 1 AND count(DISTINCT value) > 1)""").fetchone()[0]
    return {"equivalent_multi_tag_groups_not_reported": same_value_other_tag,
            "different_measure_multi_tag_groups_resolved_by_precedence": diff_value_other_tag}


# ---------------------------------------------------------------- DQ003 / DQ004
def dq003_invalid_units(con, cfg) -> list[Issue]:
    allowed = set(cfg["quality"]["allowed_units"])
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT observation_id, concept, unit FROM financial_observations"):
        u = r["unit"]
        if u is None or u == "" or (u not in allowed and not is_currency_code(u)):
            out.append(Issue("DQ003", "high", CONFIRMED, r["observation_id"],
                             f"unit in {sorted(allowed)}", f"unit={u!r}", r))
    return out


def dq004_unsupported_currency(con, cfg) -> list[Issue]:
    allowed = set(cfg["quality"]["allowed_units"])
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT observation_id, concept, unit FROM financial_observations"):
        u = r["unit"]
        if u and u not in allowed and is_currency_code(u):
            out.append(Issue("DQ004", "high", REVIEW, r["observation_id"],
                             "value expressed in a supported currency/unit",
                             f"currency {u}: no FX conversion is implemented", r))
    return out


# ---------------------------------------------------------------- DQ005
def dq005_invalid_periods(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    ann = cfg["_annual_days"]
    qtr = cfg["_quarterly_days"]
    for r in fetch_dicts(con, "SELECT observation_id, concept, period_type, fiscal_period_start AS s, "
                              "fiscal_period_end AS e, filing_date AS f FROM financial_observations"):
        kind = CONCEPTS.get(r["concept"], {}).get("kind")
        s, e, f = r["s"], r["e"], r["f"]
        problems = []
        if kind == "duration" and s is None:
            problems.append("duration concept without period start")
        if kind == "instant" and s is not None:
            problems.append("instant concept with a period start")
        if s is not None and e < s:
            problems.append("period end before start")
        elif s is not None and kind == "duration":
            days = (e - s).days
            lo, hi = ann if r["period_type"] == "annual" else qtr
            if not lo <= days <= hi:
                problems.append(f"{r['period_type']} period spans {days} days (expected {lo}-{hi})")
        if f is not None and f < e:
            problems.append("filing date precedes period end")
        for p in problems:
            out.append(Issue("DQ005", "high", CONFIRMED, r["observation_id"], "valid fiscal period", p,
                             {k: _iso(v) for k, v in r.items()}))
    return out


# ---------------------------------------------------------------- DQ006
def dq006_cik_company_relationship(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    tmap: dict[str, list[dict]] = defaultdict(list)
    for r in fetch_dicts(con, "SELECT ticker, cik, title FROM sec_ticker_map"):
        tmap[r["ticker"].upper()].append(r)
    for c in fetch_dicts(con, "SELECT company_id, cik, ticker, name FROM companies"):
        entries = tmap.get(c["ticker"].upper())
        if not tmap:
            out.append(Issue("DQ006", "medium", REVIEW, c["company_id"], "SEC ticker map available to verify CIK",
                             "sec_ticker_map is empty; CIK/ticker association unverified", c))
        elif not entries:
            out.append(Issue("DQ006", "medium", REVIEW, c["company_id"], "ticker present in SEC company_tickers.json",
                             f"ticker {c['ticker']} not found", c))
        elif c["cik"] not in {e["cik"] for e in entries}:
            out.append(Issue("DQ006", "critical", CONFIRMED, c["company_id"],
                             f"CIK {c['cik']} listed by SEC for ticker {c['ticker']}",
                             f"SEC lists {[e['cik'] for e in entries]}", {**c, "sec_entries": entries}))
        elif max(name_similarity(c["name"], e["title"]) for e in entries) < 0.5:
            out.append(Issue("DQ006", "low", REVIEW, c["company_id"], "configured name resembles SEC title",
                             f"name {c['name']!r} vs SEC {[e['title'] for e in entries]}", c))
    for r in fetch_dicts(con, "SELECT o.observation_id, o.identifier, c.cik FROM financial_observations o "
                              "JOIN companies c ON c.company_id = o.company_id "
                              "WHERE TRY_CAST(o.identifier AS BIGINT) IS NULL OR TRY_CAST(o.identifier AS BIGINT) <> c.cik"):
        out.append(Issue("DQ006", "critical", CONFIRMED, r["observation_id"],
                         "observation identifier equals its company's CIK",
                         f"identifier {r['identifier']!r} vs company CIK {r['cik']}", r))
    return out


# ---------------------------------------------------------------- DQ007 / DQ008
def dq007_ambiguous_entities(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT company_id, query, status, rationale FROM identity_resolutions "
                              "WHERE status IN ('AMBIGUOUS', 'UNMATCHED', 'SOURCE_UNAVAILABLE')"):
        sev = "medium" if r["status"] in ("AMBIGUOUS", "SOURCE_UNAVAILABLE") else "low"
        out.append(Issue("DQ007", sev, AMBIGUOUS if r["status"] == "AMBIGUOUS" else REVIEW, r["company_id"], "legal entity resolves to exactly one LEI",
                         f"GLEIF resolution status={r['status']}: {r['rationale']}", r))
    return out


def dq008_unmatched_instruments(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT company_id, query_json, status, message FROM instrument_mappings "
                              "WHERE status IN ('unmatched', 'error')"):
        out.append(Issue("DQ008", "medium", REVIEW, r["company_id"], "instrument maps to a FIGI",
                         f"OpenFIGI status={r['status']} {r['message']}", r))
    for r in fetch_dicts(con, "SELECT c.company_id FROM companies c WHERE EXISTS (SELECT 1 FROM instrument_mappings m "
                              "WHERE m.company_id = c.company_id AND m.status = 'matched') AND NOT EXISTS "
                              "(SELECT 1 FROM company_identifiers i WHERE i.company_id = c.company_id AND i.id_type = 'FIGI')"):
        out.append(Issue("DQ008", "medium", REVIEW, r["company_id"], "exactly one unambiguous common-stock FIGI",
                         "OpenFIGI returned several share classes / composites; no FIGI registered", r))
    return out


# ---------------------------------------------------------------- DQ009
def dq009_conflicts(con, cfg) -> list[Issue]:
    """Conflicts are evaluated WITHIN one XBRL tag only (different tags are different measures; precedence resolves them).
      same filing + same tag + same period + different values -> confirmed error
      value changed between filings (restatement)             -> ONE warning per company/concept listing the periods
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in fetch_dicts(con, "SELECT observation_id, company_id, concept, period_type, fiscal_period_start AS s, "
                              "fiscal_period_end AS e, accession_number AS accn, filing_date AS filed, value, xbrl_tag "
                              "FROM financial_observations"):
        groups[(r["company_id"], r["concept"], r["period_type"], r["s"], r["e"], r["xbrl_tag"])].append(r)
    out: list[Issue] = []
    restated: dict[tuple, list[dict]] = defaultdict(list)
    for key, g in groups.items():
        vals = {x["value"] for x in g}
        if len(vals) < 2:
            continue
        by_accn: dict[Any, set] = defaultdict(set)
        for x in g:
            by_accn[x["accn"]].add(x["value"])
        rec = f"{key[0]}/{key[1]}/{key[2]}/{_iso(key[4])}/{key[5]}"
        if any(len(v) > 1 for v in by_accn.values()):
            out.append(Issue("DQ009", "high", CONFIRMED, rec, "one value per tag and period within a filing",
                             "same filing reports different values for the same tag and period",
                             {"observations": [{"id": x["observation_id"], "accn": x["accn"], "value": x["value"]} for x in g]}))
        else:
            first, last = sorted(g, key=lambda x: (x["filed"] or date.min, x["accn"] or ""))[0], sorted(g, key=lambda x: (x["filed"] or date.min, x["accn"] or ""))[-1]
            rel = abs(last["value"] - first["value"]) / abs(first["value"]) if first["value"] else None
            restated[(key[0], key[1], key[2])].append({"period_end": _iso(key[4]), "tag": key[5], "first_value": first["value"],
                                                       "latest_value": last["value"], "first_accn": first["accn"],
                                                       "latest_accn": last["accn"], "relative_change": rel})
    for (cid, concept, ptype), items in restated.items():
        mx = max((i["relative_change"] or 0) for i in items)
        out.append(Issue("DQ009", "medium" if mx >= 0.05 else "low", WARNING, f"{cid}/{concept}/{ptype}",
                         "stable values across filings",
                         f"{len(items)} period(s) restated between filings (max relative change {mx:.2%}); latest filing selected",
                         {"restated_periods": sorted(items, key=lambda i: i["period_end"])}))
    return out


# ---------------------------------------------------------------- DQ010
def dq010_unexpected_changes(con, cfg) -> list[Issue]:
    max_ratio = float(cfg["quality"]["yoy_max_ratio"])
    series: dict[tuple, list[dict]] = defaultdict(list)
    # net income is a difference that legitimately swings and changes sign, so a ratio test is applied to the
    # level concepts only (revenue, total assets); net-income sanity is covered by DQ012 and the scale checks.
    for r in fetch_dicts(con, "SELECT observation_id, company_id, concept, fiscal_period_end AS e, value "
                              "FROM financial_observations WHERE is_selected AND period_type = 'annual' "
                              "AND concept IN ('revenue', 'total_assets') ORDER BY 2, 3, 4"):
        series[(r["company_id"], r["concept"])].append(r)
    out: list[Issue] = []
    for (cid, concept), rows in series.items():
        for prev, cur in zip(rows, rows[1:]):
            gap = (cur["e"] - prev["e"]).days
            pv, cv = prev["value"], cur["value"]
            if not 300 <= gap <= 430 or pv in (None, 0) or cv is None or pv != pv or cv != cv:
                continue
            ratio = cv / pv
            if pv > 0 and cv > 0 and (ratio > max_ratio or ratio < 1 / max_ratio):
                out.append(Issue("DQ010", "medium", REVIEW, cur["observation_id"],
                                 f"year-over-year ratio within 1/{max_ratio:g}..{max_ratio:g}",
                                 f"ratio {ratio:.3f} ({prev['value']} -> {cur['value']})",
                                 {"company_id": cid, "concept": concept, "previous": prev["observation_id"]}))
    return out


# ---------------------------------------------------------------- DQ011
def dq011_missing_concepts(con, cfg) -> list[Issue]:
    have = {(r["company_id"], r["concept"]) for r in fetch_dicts(
        con, "SELECT DISTINCT company_id, concept FROM financial_observations WHERE is_selected AND period_type = 'annual'")}
    out: list[Issue] = []
    for c in fetch_dicts(con, "SELECT company_id, ticker, enabled_concepts FROM companies"):
        for concept in (c["enabled_concepts"] or "").split(","):
            if concept and (c["company_id"], concept) not in have:
                out.append(Issue("DQ011", "medium", REVIEW, f"{c['company_id']}/{concept}",
                                 f"annual {concept} observations present for {c['ticker']}",
                                 "none found (company may use an unmapped XBRL tag or report differently)", c))
    return out


# ---------------------------------------------------------------- DQ012
def dq012_invalid_numbers(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    for r in fetch_dicts(con, "SELECT observation_id, concept, value FROM financial_observations"):
        v = r["value"]
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            out.append(Issue("DQ012", "critical", CONFIRMED, r["observation_id"], "finite numeric value", f"value={v}", r))
        elif r["concept"] == "total_assets" and v <= 0:
            out.append(Issue("DQ012", "high", CONFIRMED, r["observation_id"], "total assets > 0", f"value={v}", r))
        elif r["concept"] == "revenue" and v < 0:
            out.append(Issue("DQ012", "medium", REVIEW, r["observation_id"], "revenue >= 0", f"value={v}", r))
    return out


# ---------------------------------------------------------------- DQ013
def dq013_provenance(con, cfg) -> list[Issue]:
    out: list[Issue] = []
    rows = fetch_dicts(con, """
        SELECT o.observation_id, o.accession_number, o.source_url, o.retrieval_timestamp, o.snapshot_id, s.snapshot_id AS snap_found
        FROM financial_observations o LEFT JOIN source_snapshots s ON s.snapshot_id = o.snapshot_id
        WHERE o.accession_number IS NULL OR trim(o.accession_number) = '' OR o.source_url IS NULL
           OR o.retrieval_timestamp IS NULL OR o.snapshot_id IS NULL OR s.snapshot_id IS NULL""")
    for r in rows:
        missing = [k for k in ("accession_number", "source_url", "retrieval_timestamp", "snapshot_id") if not r[k]]
        if r["snapshot_id"] and not r["snap_found"]:
            missing.append("snapshot_row")
        out.append(Issue("DQ013", "high", CONFIRMED, r["observation_id"], "complete provenance",
                         f"missing: {', '.join(missing)}", {k: _iso(v) for k, v in r.items()}))
    return out


RULES: list[tuple[str, Callable, str]] = [
    ("DQ001", dq001_missing_identifiers, "Missing required identifiers"),
    ("DQ002", dq002_duplicates, "Duplicate observations"),
    ("DQ003", dq003_invalid_units, "Invalid or inconsistent units"),
    ("DQ004", dq004_unsupported_currency, "Unsupported currency conversions"),
    ("DQ005", dq005_invalid_periods, "Invalid fiscal periods"),
    ("DQ006", dq006_cik_company_relationship, "Incorrect company-to-CIK relationships"),
    ("DQ007", dq007_ambiguous_entities, "Ambiguous entity matches"),
    ("DQ008", dq008_unmatched_instruments, "Unmatched financial instruments"),
    ("DQ009", dq009_conflicts, "Conflicting / restated observations for equivalent periods"),
    ("DQ010", dq010_unexpected_changes, "Unexpected year-over-year changes"),
    ("DQ011", dq011_missing_concepts, "Missing required concepts"),
    ("DQ012", dq012_invalid_numbers, "Invalid numerical values"),
    ("DQ013", dq013_provenance, "Source provenance gaps"),
]
