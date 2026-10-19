"""Seeded, deterministic test-case generation.

Each case = (agent_input, store_overrides, expected, metadata)
  agent_input     : the ONLY thing an agent may see (records under review). Opaque ids, no labels.
  store_overrides : hidden; removes periods from the case-scoped reference view (missing-data cases)
  expected        : hidden scoring spec
Golden records and raw snapshots are never modified; presented records are transformed copies.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.common.db import fetch_dicts
from src.common.util import pad_cik, stable_id
from src.corruption.transforms import apply_steps

CATEGORIES = ["SCALE_ERROR", "ID_MISMAP", "MISSING_DATA", "CONTRADICTION", "PERIOD_SHIFT", "CLEAN_CONTROL"]
SUBTYPES: dict[str, list[str]] = {
    "SCALE_ERROR": ["mul_value", "div_value", "mislabel_unit"],
    "ID_MISMAP": ["cik_swap", "figi_swap"],
    "MISSING_DATA": ["missing_value", "unverifiable_period"],
    "CONTRADICTION": ["conflicting_values", "unit_inconsistency"],
    "PERIOD_SHIFT": ["label_prior_year"],
    "CLEAN_CONTROL": ["valid_record", "optional_field_missing", "valid_thousands_label"],
}


@dataclass
class Context:
    companies: dict[str, dict[str, Any]]
    obs: dict[str, dict[str, Any]]                 # selected annual USD observations
    by_key: dict[tuple[str, str, str], dict[str, Any]]
    figi: dict[str, str]


def load_context(con) -> Context:
    companies = {c["company_id"]: c for c in fetch_dicts(con, "SELECT * FROM companies")}
    obs_rows = fetch_dicts(con, "SELECT * FROM financial_observations WHERE is_selected AND period_type = 'annual' "
                                "AND unit = 'USD' AND value IS NOT NULL ORDER BY observation_id")
    obs = {o["observation_id"]: o for o in obs_rows
           if math.isfinite(o["value"]) and o["value"] != 0 and o["company_id"] in companies}
    by_key = {(o["company_id"], o["concept"], o["fiscal_period_end"].isoformat()): o for o in obs.values()}
    figi = {r["company_id"]: r["id_value"] for r in fetch_dicts(
        con, "SELECT company_id, id_value FROM company_identifiers WHERE id_type = 'FIGI' "
             "AND verification_status = 'unique_common_stock'")}
    return Context(companies, obs, by_key, figi)


def build_record(ctx: Context, o: dict[str, Any], record_id: str, source_label: str = "feed_A") -> dict[str, Any]:
    c = ctx.companies[o["company_id"]]
    return {
        "record_id": record_id,
        "source_label": source_label,
        "company_name": c["name"],
        "ticker": c["ticker"],
        "cik": pad_cik(c["cik"]),
        "concept": o["concept"],
        "period_type": "annual",
        "fiscal_period_end": o["fiscal_period_end"].isoformat(),
        "value": o["value"],
        "unit": "USD",
        "filing_date": o["filing_date"].isoformat() if o["filing_date"] else None,
        "accession_number": o["accession_number"],
    }


def _evidence(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": o["observation_id"], "company_id": o["company_id"], "concept": o["concept"],
        "fiscal_period_end": o["fiscal_period_end"].isoformat(), "value": o["value"], "unit": o["unit"],
        "accession_number": o["accession_number"],
        "filing_date": o["filing_date"].isoformat() if o["filing_date"] else None,
        "xbrl_tag": o["xbrl_tag"], "source_url": o["source_url"], "snapshot_id": o["snapshot_id"],
        "retrieval_timestamp": str(o["retrieval_timestamp"]),
    }


def _ordered(cands: list[dict[str, Any]], seed: int, ctype: str) -> list[dict[str, Any]]:
    lst = list(cands)
    random.Random(f"{seed}:{ctype}").shuffle(lst)
    return lst


def _prior_obs(ctx: Context, o: dict[str, Any]) -> dict[str, Any] | None:
    for other in ctx.obs.values():
        if (other["company_id"] == o["company_id"] and other["concept"] == o["concept"]
                and 350 <= (o["fiscal_period_end"] - other["fiscal_period_end"]).days <= 380):
            return other
    return None


def _exclusion(o: dict[str, Any]) -> dict[str, str]:
    return {"company_id": o["company_id"], "concept": o["concept"], "period_type": "annual",
            "period_end": o["fiscal_period_end"].isoformat()}


def _expected(**kw) -> dict[str, Any]:
    base = {"is_error": True, "expected_statuses": [], "expected_issue_types": [], "should_abstain": False,
            "expected_value": None, "expected_unit": "USD", "exact_value": False, "expected_company_identifier": None}
    base.update(kw)
    return base


def make_case(ctx: Context, ctype: str, idx: int, o: dict[str, Any], seed: int, smoke_n: int) -> dict[str, Any]:
    subtype = SUBTYPES[ctype][idx % len(SUBTYPES[ctype])]
    case_seed = int(stable_id(seed, ctype, idx, length=8), 16)
    rng = random.Random(case_seed)
    case_id = "case-" + stable_id(seed, ctype, idx, o["observation_id"], length=12)
    company = ctx.companies[o["company_id"]]
    cik10 = pad_cik(company["cik"])
    rec_id = "rec-" + stable_id(case_id, "a", length=10)
    base = build_record(ctx, o, rec_id)
    v = o["value"]
    steps: list[dict[str, Any]] = []
    overrides: dict[str, Any] = {"exclude_keys": []}
    presented: list[dict[str, Any]]
    severity = "medium"
    behavior = ""

    if ctype == "SCALE_ERROR":
        severity = "high"
        if subtype == "mul_value":
            steps = [{"transform": "scale_value", "params": {"op": "mul", "factor": rng.choice([1000, 1000000])}}]
        elif subtype == "div_value":
            steps = [{"transform": "scale_value", "params": {"op": "div", "factor": rng.choice([1000, 1000000])}}]
        else:
            steps = [{"transform": "set_field", "params": {"field": "unit", "from": "USD",
                                                           "to": rng.choice(["USDthousands", "USDmillions"])}}]
        presented = [apply_steps(base, steps)]
        expected = _expected(expected_statuses=["flagged"], expected_issue_types=["scale_mismatch"],
                             expected_value=v, expected_company_identifier=cik10)
        behavior = "Flag a scale/unit error and give the verified USD value from the reference store."

    elif ctype == "ID_MISMAP":
        severity = "critical"
        others = [c for c in ctx.companies.values() if c["company_id"] != company["company_id"]]
        other = rng.choice(sorted(others, key=lambda c: c["company_id"]))
        if subtype == "figi_swap":
            pool = sorted((c for c in others if c["company_id"] in ctx.figi), key=lambda c: c["company_id"])
            if company["company_id"] in ctx.figi and pool:
                other = rng.choice(pool)
                base["figi"] = ctx.figi[company["company_id"]]
                steps = [{"transform": "set_field", "params": {"field": "figi", "from": ctx.figi[company["company_id"]],
                                                               "to": ctx.figi[other["company_id"]]}}]
            else:
                subtype = "cik_swap"
        if subtype == "cik_swap":
            steps = [{"transform": "set_field", "params": {"field": "cik", "from": cik10, "to": pad_cik(other["cik"])}}]
        presented = [apply_steps(base, steps)]
        expected = _expected(expected_statuses=["flagged"], expected_issue_types=["identifier_collision"],
                             expected_company_identifier=cik10)
        behavior = "Flag the identifier inconsistency; the name/ticker resolve to a different company than the CIK/FIGI."

    elif ctype == "MISSING_DATA":
        severity = "medium"
        overrides["exclude_keys"] = [_exclusion(o)]
        if subtype == "missing_value":
            steps = [{"transform": "set_field", "params": {"field": "value", "from": v, "to": None}}]
        presented = [apply_steps(base, steps)]
        expected = _expected(expected_statuses=["insufficient_evidence"], expected_issue_types=["missing_input"],
                             should_abstain=True)
        behavior = "Abstain (insufficient_evidence): the reference store has no observation for this period and/or the record lacks its value."

    elif ctype == "CONTRADICTION":
        rec_b = dict(base)
        rec_b["record_id"] = "rec-" + stable_id(case_id, "b", length=10)
        rec_b["source_label"] = "feed_B"
        if subtype == "conflicting_values":
            severity = "high"
            overrides["exclude_keys"] = [_exclusion(o)]
            delta = rng.choice([1.08, 0.88, 1.25, 0.8])
            steps = [{"transform": "scale_value", "params": {"op": "mul", "factor": delta}}]
            expected = _expected(expected_statuses=["ambiguous", "flagged"], expected_issue_types=["conflicting_sources"],
                                 should_abstain=True)
            behavior = "Do not pick a value: two feeds disagree and the reference store cannot adjudicate."
        else:
            severity = "medium"
            steps = [{"transform": "scale_value", "params": {"op": "div", "factor": 1000}},
                     {"transform": "set_field", "params": {"field": "unit", "from": "USD", "to": "USDthousands"}}]
            expected = _expected(expected_statuses=["flagged"], expected_issue_types=["unit_inconsistency"],
                                 expected_value=v)
            behavior = "Flag the inconsistent unit labels; after normalization both feeds equal the verified value."
        presented = [base, apply_steps(rec_b, steps)]

    elif ctype == "PERIOD_SHIFT":
        severity = "critical"
        prior = _prior_obs(ctx, o)
        assert prior is not None
        steps = [{"transform": "set_field", "params": {"field": "fiscal_period_end",
                                                       "from": base["fiscal_period_end"],
                                                       "to": prior["fiscal_period_end"].isoformat()}}]
        presented = [apply_steps(base, steps)]
        expected = _expected(expected_statuses=["flagged"], expected_issue_types=["period_mismatch"],
                             expected_company_identifier=cik10)
        behavior = "Flag the period mislabel: the value belongs to a different fiscal period than the one stated."

    else:  # CLEAN_CONTROL
        severity = "low"
        if subtype == "valid_record":
            if company["company_id"] in ctx.figi:
                base["figi"] = ctx.figi[company["company_id"]]
        elif subtype == "optional_field_missing":
            steps = [{"transform": "delete_field", "params": {"field": "filing_date", "original": base["filing_date"]}},
                     {"transform": "delete_field", "params": {"field": "accession_number", "original": base["accession_number"]}}]
        else:
            steps = [{"transform": "scale_value", "params": {"op": "div", "factor": 1000}},
                     {"transform": "set_field", "params": {"field": "unit", "from": "USD", "to": "USDthousands"}}]
        presented = [apply_steps(base, steps)]
        expected = _expected(is_error=False, expected_statuses=["verified"], expected_issue_types=[],
                             expected_value=v, expected_company_identifier=cik10)
        behavior = "Verify the record: it is correct (valid optional omissions / legitimate unit label)."

    agent_input = {"case_id": case_id, "task": "verify_records", "records": presented}
    return {
        "case_id": case_id,
        "corruption_type": ctype,
        "subtype": subtype,
        "random_seed": case_seed,
        "affected_record_id": o["observation_id"],
        "corruption_parameters": {"steps": steps, "store_overrides": overrides},
        "original_value": base,
        "corrupted_value": presented,
        "expected_behavior": behavior,
        "expected_answer": {k: expected[k] for k in ("expected_statuses", "expected_issue_types", "expected_value",
                                                     "expected_unit", "expected_company_identifier")},
        "expected_abstention_or_escalation": bool(expected["should_abstain"] or expected["is_error"]),
        "source_evidence": [_evidence(o)],
        "severity": severity,
        "smoke": idx < smoke_n,
        "agent_input": agent_input,
        "store_overrides": overrides,
        "expected": expected,
    }


def candidates_for(ctx: Context, ctype: str) -> list[dict[str, Any]]:
    cands = list(ctx.obs.values())
    if ctype == "PERIOD_SHIFT":
        out = []
        for o in cands:
            prior = _prior_obs(ctx, o)
            if prior is not None and abs(prior["value"] - o["value"]) > 0.03 * abs(o["value"]):
                out.append(o)
        return out
    return cands


def generate_cases(con, settings: dict[str, Any], seed: int | None = None, n_per_category: int | None = None
                   ) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    cc = settings["corruption"]
    seed = cc["seed"] if seed is None else seed
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    n = cc["cases_per_category"] if n_per_category is None else n_per_category
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"n_per_category must be a positive integer, got {n!r}")
    ctx = load_context(con)
    cases: list[dict[str, Any]] = []
    shortfalls: dict[str, int] = {}
    for ctype in CATEGORIES:
        cands = _ordered(candidates_for(ctx, ctype), seed, ctype)
        if not cands:
            shortfalls[ctype] = n
            continue
        for idx in range(n):
            cases.append(make_case(ctx, ctype, idx, cands[idx % len(cands)], seed, int(cc["smoke_per_category"])))
    cases.sort(key=lambda c: c["case_id"])      # opaque order: category is not inferable from position
    digest = stable_id(*[c["case_id"] for c in cases], length=8)
    suite_version = f"{cc['suite_version']}-s{seed}-n{n}-{digest}"
    for c in cases:
        c["suite_version"] = suite_version
    return cases, suite_version, shortfalls
