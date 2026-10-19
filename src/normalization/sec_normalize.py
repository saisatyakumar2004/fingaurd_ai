"""Normalize SEC companyfacts JSON into financial_observations rows.

Shape of the source (https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json):
  {"cik": 320193, "entityName": "...", "facts": {"us-gaap": {"<Tag>": {"units": {"USD": [
      {"start": "2022-09-25", "end": "2023-09-30", "val": 383285000000, "accn": "...",
       "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03", "frame": "CY2023"}, ...]}}}}}

Important properties
- fy / fp in a fact describe the FILING that reported it, not the fact's own period; we keep
  them as reported_fy / reported_fp and derive our own period label from the period dates.
- The same period is usually reported several times (original + later comparatives, and
  sometimes restated). ALL distinct reports are kept; exactly one per
  (company, concept, period_type, start, end) is flagged is_selected using
  filing date DESC, accession DESC, tag priority ASC -- deterministic.
- Companies use different XBRL tags for the same concept; every candidate tag present is
  stored (with its priority) and the tag used is recorded on the row.
- Missing data stays missing. Nothing is defaulted to zero.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from src.common.db import fetch_dicts, insert_rows
from src.common.util import pad_cik, stable_id
from src.normalization.units import is_currency_code

CONCEPTS: dict[str, dict[str, Any]] = {
    "revenue": {
        "kind": "duration",
        "tags": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "RevenuesNetOfInterestExpense",   # banks (GS, MS ...): net revenue; semantics differ from product-company revenue
        ],
    },
    "net_income": {"kind": "duration", "tags": ["NetIncomeLoss", "ProfitLoss"]},
    "total_assets": {"kind": "instant", "tags": ["Assets"]},
}


class SecFact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: Optional[date] = None
    end: date
    val: float
    accn: Optional[str] = None
    fy: Optional[int] = None
    fp: Optional[str] = None
    form: Optional[str] = None
    filed: Optional[date] = None


def classify_period(
    kind: str,
    start: date | None,
    end: date,
    form: str | None,
    annual_days: tuple[int, int] = (350, 380),
    quarterly_days: tuple[int, int] = (80, 100),
) -> str | None:
    """Return 'annual' / 'quarterly' or None when the period is not a clean annual/quarter.
    YTD (6/9-month) durations are intentionally excluded (None)."""
    if kind == "instant":
        if start is not None:
            return None
        f = (form or "").upper()
        if f.startswith("10-K"):
            return "annual"
        if f.startswith("10-Q"):
            return "quarterly"
        return None
    if start is None:
        return None
    days = (end - start).days
    if annual_days[0] <= days <= annual_days[1]:
        return "annual"
    if quarterly_days[0] <= days <= quarterly_days[1]:
        return "quarterly"
    return None


def period_label(period_type: str, end: date) -> str:
    if period_type == "annual":
        return f"FY{end.year}"
    return f"{end.year}-CQ{(end.month - 1) // 3 + 1}"


def normalize_company_facts(
    payload: dict[str, Any],
    *,
    company_id: str,
    cik: int,
    concepts: list[str],
    snapshot: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    sec_cfg = settings["sec"]
    min_year = int(sec_cfg["min_period_year"])
    allowed_forms = {f.upper() for f in sec_cfg["allowed_forms"]}
    annual_days = tuple(sec_cfg["annual_days"])
    quarterly_days = tuple(sec_cfg["quarterly_days"])
    gaap = ((payload.get("facts") or {}).get("us-gaap")) or {}

    retrieved = snapshot.get("retrieval_timestamp")
    if isinstance(retrieved, str):
        retrieved = datetime.fromisoformat(retrieved)

    stats: Counter[str] = Counter()
    rows: dict[str, dict[str, Any]] = {}
    for concept in concepts:
        spec = CONCEPTS.get(concept)
        if spec is None:
            stats["unknown_concept"] += 1
            continue
        for prio, tag in enumerate(spec["tags"]):
            node = gaap.get(tag)
            if not node:
                continue
            for unit, items in (node.get("units") or {}).items():
                for raw in items:
                    stats["raw_facts"] += 1
                    try:
                        f = SecFact.model_validate(raw)
                    except ValidationError:
                        stats["invalid_fact"] += 1
                        continue
                    if f.end.year < min_year:
                        stats["before_min_year"] += 1
                        continue
                    if (f.form or "").upper() not in allowed_forms:
                        stats["form_not_allowed"] += 1
                        continue
                    ptype = classify_period(spec["kind"], f.start, f.end, f.form, annual_days, quarterly_days)
                    if ptype is None:
                        stats["period_not_annual_or_quarterly"] += 1
                        continue
                    oid = stable_id(company_id, concept, tag, ptype, f.start, f.end, f.accn, f.val, unit, length=24)
                    if oid in rows:
                        stats["exact_duplicate_dropped"] += 1
                        continue
                    rows[oid] = {
                        "observation_id": oid,
                        "company_id": company_id,
                        "identifier": pad_cik(cik),
                        "concept": concept,
                        "xbrl_tag": tag,
                        "tag_priority": prio,
                        "period_type": ptype,
                        "fiscal_year": f.end.year,
                        "fiscal_period": period_label(ptype, f.end),
                        "reported_fy": f.fy,
                        "reported_fp": f.fp,
                        "fiscal_period_start": f.start,
                        "fiscal_period_end": f.end,
                        "filing_date": f.filed,
                        "accession_number": f.accn,
                        "form": f.form,
                        "amended": (f.form or "").upper().endswith("/A"),
                        "value": f.val,
                        "unit": unit,
                        "currency": unit if is_currency_code(unit) else ("USD" if unit.startswith("USD") else None),
                        "source_system": "sec_edgar",
                        "source_url": snapshot.get("url"),
                        "retrieval_timestamp": retrieved,
                        "snapshot_id": snapshot.get("snapshot_id"),
                        "raw_ref": f"us-gaap/{tag}/units/{unit}#accn={f.accn}",
                        "is_selected": False,
                    }
                    stats["kept"] += 1
    return list(rows.values()), dict(stats)


def select_observations(rows: list[dict[str, Any]]) -> None:
    """Set is_selected in place: exactly one row per (company, concept, period_type, start, end).

    Deterministic precedence (documented in docs/QUALITY_FINDINGS_AUDIT.md):
      1. concept-tag precedence FIRST: the lowest tag_priority present for that period wins
         (e.g. NetIncomeLoss before ProfitLoss - they are different measures, never mixed);
      2. then the latest filing date; 3. then the highest accession number.
    Every other report of the period stays in the table (restatements keep their provenance).
    """
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        r["is_selected"] = False
        groups[(r["company_id"], r["concept"], r["period_type"], r["fiscal_period_start"], r["fiscal_period_end"])].append(r)
    for g in groups.values():
        best_tag = min((r["tag_priority"] or 0) for r in g)
        cands = [r for r in g if (r["tag_priority"] or 0) == best_tag]
        cands.sort(key=lambda r: (r["filing_date"] or date.min, r["accession_number"] or ""), reverse=True)
        cands[0]["is_selected"] = True


def reselect_all(con) -> int:
    """Recompute is_selected for every company (after a precedence-rule change). Observations are untouched."""
    n = 0
    for (cid,) in con.execute("SELECT DISTINCT company_id FROM financial_observations").fetchall():
        rows = fetch_dicts(con, "SELECT * FROM financial_observations WHERE company_id = ?", [cid])
        select_observations(rows)
        con.execute("DELETE FROM financial_observations WHERE company_id = ?", [cid])
        insert_rows(con, "financial_observations", rows)
        n += len(rows)
    return n


def merge_company_observations(con, company_id: str, new_rows: list[dict[str, Any]]) -> int:
    """Merge new rows with whatever is already stored for the company, recompute selection,
    and rewrite the company's rows. Older observations are never dropped."""
    existing = fetch_dicts(con, "SELECT * FROM financial_observations WHERE company_id = ?", [company_id])
    merged = {r["observation_id"]: r for r in existing}
    for r in new_rows:
        merged.setdefault(r["observation_id"], r)
    rows = list(merged.values())
    select_observations(rows)
    con.execute("DELETE FROM financial_observations WHERE company_id = ?", [company_id])
    insert_rows(con, "financial_observations", rows)
    return len(rows)
