"""Multi-fault and realistic edge-case suite (category MULTI_FAULT), separate from the 120-case base suite.

Expected outcomes are defined here from the golden data and the case construction alone - never from any agent output.
Each case records WHY it is a valid test in corruption_parameters["rationale"].
"""
from __future__ import annotations

import random
from typing import Any

from src.common.db import fetch_dicts
from src.common.util import pad_cik, stable_id
from src.corruption.engine import (Context, _evidence, _exclusion, _expected, _ordered, build_record, load_context)
from src.corruption.transforms import apply_steps
from src.normalization.units import within_tolerance

MULTI_SUBTYPES = [
    "wrong_company_with_other_companys_value",
    "correct_value_unsupported_currency",
    "quarter_value_labelled_annual",
    "missing_fiscal_period",
    "restated_filing_versions",
    "unidentifiable_company_no_evidence",
    "duplicate_feed_equivalent",
]
SUITE_PREFIX = "multi-v1"


def _quarterly(con, ctx: Context) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in fetch_dicts(con, "SELECT * FROM financial_observations WHERE is_selected AND period_type = 'quarterly' AND unit = 'USD' "
                              "AND value IS NOT NULL ORDER BY observation_id"):
        out.setdefault((r["company_id"], r["concept"]), []).append(r)
    return out


def _restated(con) -> list[dict[str, Any]]:
    return fetch_dicts(con, """
        SELECT s.observation_id AS sel_id, n.observation_id AS old_id, n.value AS old_value, n.accession_number AS old_accn,
               n.filing_date AS old_filed
        FROM financial_observations s JOIN financial_observations n
          ON n.company_id = s.company_id AND n.concept = s.concept AND n.period_type = s.period_type
         AND n.fiscal_period_end = s.fiscal_period_end AND n.xbrl_tag = s.xbrl_tag
        WHERE s.is_selected AND NOT n.is_selected AND s.period_type = 'annual' AND s.unit = 'USD' AND n.unit = 'USD'
          AND n.accession_number <> s.accession_number AND abs(n.value - s.value) > 0.01 * abs(s.value)
        ORDER BY s.observation_id, n.observation_id""")


def _valid_quarters(o, quarters, all_obs, tol):
    """Quarterly values usable as the wrong figure: clearly different from this annual value (>3%) and not equal to ANY
    annual value of the same company/concept (otherwise the case would be a period mislabel, not a quarter/annual confusion)."""
    annual = [x["value"] for x in all_obs if x["company_id"] == o["company_id"] and x["concept"] == o["concept"]]
    return [q for q in quarters.get((o["company_id"], o["concept"]), [])
            if not within_tolerance(q["value"], o["value"], 0.03) and not any(within_tolerance(q["value"], a, tol) for a in annual)]


def generate_multi_cases(con, settings: dict[str, Any], seed: int | None = None, n_per_subtype: int = 5
                         ) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    seed = settings["corruption"]["seed"] if seed is None else seed
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    if not isinstance(n_per_subtype, int) or n_per_subtype < 1:
        raise ValueError(f"n_per_subtype must be a positive integer, got {n_per_subtype!r}")
    tol = settings["evaluation"]["relative_tolerance"]
    ctx = load_context(con)
    quarters = _quarterly(con, ctx)
    restated = _restated(con)
    all_obs = sorted(ctx.obs.values(), key=lambda o: o["observation_id"])
    cases: list[dict[str, Any]] = []
    short: dict[str, int] = {}

    for sub in MULTI_SUBTYPES:
        cands = list(all_obs)
        if sub == "wrong_company_with_other_companys_value":
            cands = [o for o in all_obs if any(k[1:] == (o["concept"], o["fiscal_period_end"].isoformat()) and k[0] != o["company_id"]
                                               for k in ctx.by_key)]
        elif sub == "quarter_value_labelled_annual":
            cands = [o for o in all_obs if _valid_quarters(o, quarters, all_obs, tol)]
        elif sub == "restated_filing_versions":
            cands = [ctx.obs[r["sel_id"]] for r in restated if r["sel_id"] in ctx.obs]
        cands = _ordered(cands, seed, sub)
        if not cands:
            short[sub] = n_per_subtype
            continue
        for idx in range(n_per_subtype):
            o = cands[idx % len(cands)]
            case_seed = int(stable_id(seed, sub, idx, length=8), 16)
            rng = random.Random(case_seed)
            case_id = "mcase-" + stable_id(seed, sub, idx, o["observation_id"], length=12)
            company = ctx.companies[o["company_id"]]
            cik10, v = pad_cik(company["cik"]), o["value"]
            base = build_record(ctx, o, "rec-" + stable_id(case_id, "a", length=10))
            steps: list[dict[str, Any]] = []
            overrides: dict[str, Any] = {"exclude_keys": []}
            severity, rationale = "high", ""
            if sub == "wrong_company_with_other_companys_value":
                others = sorted((k for k in ctx.by_key if k[0] != o["company_id"] and k[1:] == (o["concept"], o["fiscal_period_end"].isoformat())))
                d_obs = ctx.by_key[rng.choice(others)]
                d_company = ctx.companies[d_obs["company_id"]]
                steps = [{"transform": "set_field", "params": {"field": "cik", "from": cik10, "to": pad_cik(d_company["cik"])}},
                         {"transform": "set_field", "params": {"field": "value", "from": v, "to": d_obs["value"]}}]
                presented = [apply_steps(base, steps)]
                expected = _expected(expected_statuses=["flagged"], expected_issue_types=["identifier_collision"],
                                     expected_company_identifier=cik10)
                severity = "critical"
                rationale = "Name/ticker say company A, CIK and value belong to company B: the value looks plausible for B, so only an identity check catches it."
                behavior = "Flag the identifier conflict; do not accept B's figures for A."
            elif sub == "correct_value_unsupported_currency":
                steps = [{"transform": "set_field", "params": {"field": "unit", "from": "USD", "to": "EUR"}}]
                presented = [apply_steps(base, steps)]
                expected = _expected(expected_statuses=["flagged"], expected_issue_types=["unit_inconsistency"], expected_value=v,
                                     expected_company_identifier=cik10)
                rationale = "The number is right but the currency label is wrong/unsupported; a tolerance check must not hide the unit problem."
                behavior = "Flag the unit/currency; report the verified USD value from the reference store."
            elif sub == "quarter_value_labelled_annual":
                qs = _valid_quarters(o, quarters, all_obs, tol)
                q = rng.choice(sorted(qs, key=lambda x: x["observation_id"]))
                steps = [{"transform": "set_field", "params": {"field": "value", "from": v, "to": q["value"]}}]
                presented = [apply_steps(base, steps)]
                expected = _expected(expected_statuses=["flagged"], expected_issue_types=["value_mismatch"], expected_value=v,
                                     expected_company_identifier=cik10)
                rationale = "A standalone-quarter figure is presented under an annual period end (quarterly-vs-annual confusion)."
                behavior = "Flag the mismatch; the annual figure for this period differs from the stated quarterly amount."
            elif sub == "missing_fiscal_period":
                steps = [{"transform": "set_field", "params": {"field": "fiscal_period_end", "from": base["fiscal_period_end"], "to": None}}]
                presented = [apply_steps(base, steps)]
                expected = _expected(expected_statuses=["insufficient_evidence"], expected_issue_types=["missing_input"], should_abstain=True)
                rationale = "Without a fiscal period the value cannot be attributed to a reporting period, even though the company has data."
                behavior = "Abstain: required period metadata is missing."
            elif sub == "restated_filing_versions":
                r = next(x for x in restated if x["sel_id"] == o["observation_id"])
                rec_old = dict(base, record_id="rec-" + stable_id(case_id, "old", length=10), source_label="filing_v1",
                               value=r["old_value"], accession_number=r["old_accn"],
                               filing_date=r["old_filed"].isoformat() if r["old_filed"] else None)
                rec_new = dict(base, record_id="rec-" + stable_id(case_id, "new", length=10), source_label="filing_v2")
                steps = [{"transform": "set_field", "params": {"field": "value", "from": v, "to": r["old_value"]}}]
                presented = [rec_old, rec_new]
                expected = _expected(expected_statuses=["flagged"], expected_issue_types=["conflicting_sources"], expected_value=v)
                severity = "medium"
                rationale = "Two filings report different values for the same period (restatement); the later filing is the golden selection."
                behavior = "Flag the conflict between filing versions and report the latest filed value."
            elif sub == "unidentifiable_company_no_evidence":
                overrides["exclude_keys"] = [_exclusion(o)]
                steps = [{"transform": "set_field", "params": {"field": "cik", "from": cik10, "to": None}},
                         {"transform": "set_field", "params": {"field": "ticker", "from": company["ticker"], "to": "ZZQX"}},
                         {"transform": "set_field", "params": {"field": "company_name", "from": company["name"], "to": "Unregistered Holdings Q"}}]
                presented = [apply_steps(base, steps)]
                expected = _expected(expected_statuses=["insufficient_evidence", "ambiguous"], expected_issue_types=["missing_input"],
                                     should_abstain=True)
                rationale = "The entity cannot be resolved AND the period is absent from the reference store: two independent reasons to abstain."
                behavior = "Abstain: neither identity nor evidence can be established."
            else:  # duplicate_feed_equivalent
                severity = "low"
                dup = dict(base, record_id="rec-" + stable_id(case_id, "dup", length=10), source_label="feed_B")
                presented = [base, dup]
                expected = _expected(is_error=False, expected_statuses=["verified"], expected_issue_types=[], expected_value=v)
                rationale = "The same correct observation arrives from two feeds in the same unit; duplicates are not errors."
                behavior = "Verify; do not raise a conflict for identical duplicate feeds."
            cases.append({
                "case_id": case_id, "corruption_type": "MULTI_FAULT", "subtype": sub, "random_seed": case_seed,
                "affected_record_id": o["observation_id"],
                "corruption_parameters": {"steps": steps, "store_overrides": overrides, "rationale": rationale},
                "original_value": base, "corrupted_value": presented, "expected_behavior": behavior,
                "expected_answer": {k: expected[k] for k in ("expected_statuses", "expected_issue_types", "expected_value",
                                                             "expected_unit", "expected_company_identifier")},
                "expected_abstention_or_escalation": bool(expected["should_abstain"] or expected["is_error"]),
                "source_evidence": [_evidence(o)], "severity": severity, "smoke": idx < 2,
                "agent_input": {"case_id": case_id, "task": "verify_records", "records": presented},
                "store_overrides": overrides, "expected": expected,
            })
    cases.sort(key=lambda c: c["case_id"])
    suite = f"{SUITE_PREFIX}-s{seed}-n{n_per_subtype}-{stable_id(*[c['case_id'] for c in cases], length=8)}"
    for c in cases:
        c["suite_version"] = suite
    return cases, suite, short
