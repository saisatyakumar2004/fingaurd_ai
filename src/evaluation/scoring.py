"""Independent, side-effect-free scoring of one agent response against a hidden expectation."""
from __future__ import annotations

from typing import Any

from src.common.util import cik_int
from src.normalization.units import UnsupportedUnit, to_base, within_tolerance

ABSTAIN_STATUSES = {"insufficient_evidence", "ambiguous"}


def numeric_check(expected_value: float | None, value: float | None, unit: str | None,
                  tolerance: float, exact: bool = False) -> bool | None:
    """None = not applicable. Units are normalized first; an unsupported/missing unit FAILS
    (tolerance can never hide a unit or currency mismatch)."""
    if expected_value is None:
        return None
    if value is None or unit is None:
        return False
    try:
        base, _ = to_base(float(value), unit)
    except (UnsupportedUnit, TypeError, ValueError):
        return False
    return within_tolerance(base, expected_value, 1e-9 if exact else tolerance)


def identity_check(expected: str | None, actual: str | None) -> bool | None:
    if expected is None:
        return None
    a, e = cik_int(actual), cik_int(expected)
    return a is not None and a == e


def rubric_scores(resp: dict[str, Any], issued: dict[str, Any], input_refs: set[str], tolerance: float) -> dict[str, float]:
    ev = resp.get("evidence") or []
    allowed = set(issued) | input_refs
    relevance = sum(1 for e in ev if e.get("ref") in allowed) / len(ev) if ev else 0.0

    checks: list[bool] = []
    status, issues = resp["status"], resp.get("detected_issue_types") or []
    checks.append(not (status == "verified" and issues))
    checks.append(not (status == "flagged" and not issues))
    checks.append(not (status == "insufficient_evidence" and resp.get("normalized_value") is not None))
    for e in ev:
        shown = issued.get(e.get("ref"))
        if e.get("value") is not None and shown is not None:
            checks.append(within_tolerance(e["value"], shown, tolerance))
    factual = sum(checks) / len(checks)

    parts = [
        len((resp.get("explanation") or "").strip()) >= 30,
        bool((resp.get("recommended_action") or "").strip()),
        bool(ev) or status in ABSTAIN_STATUSES,
        status != "flagged" or bool(issues),
    ]
    return {"evidence_relevance": round(relevance, 4), "factual_consistency": round(factual, 4),
            "reasoning_completeness": round(sum(parts) / len(parts), 4)}


def score_case(expected: dict[str, Any], resp: dict[str, Any], issued: dict[str, Any],
               input_refs: set[str], tolerance: float) -> dict[str, Any]:
    status = resp["status"]
    issues = set(resp.get("detected_issue_types") or [])
    exp_issues = set(expected["expected_issue_types"])
    abstained = status in ABSTAIN_STATUSES

    status_ok = status in expected["expected_statuses"]
    issue_ok = exp_issues <= issues if exp_issues else not issues
    numeric_ok = numeric_check(expected.get("expected_value"), resp.get("normalized_value"), resp.get("unit"),
                               tolerance, bool(expected.get("exact_value")))
    identity_ok = identity_check(expected.get("expected_company_identifier"), resp.get("company_identifier"))
    needs_evidence = status in ("verified", "flagged")
    allowed_refs = set(issued) | input_refs
    invalid_refs = [e.get("ref") for e in (resp.get("evidence") or []) if e.get("ref") not in allowed_refs]
    # a material claim (verified/flagged) must cite at least one ref that a reference-store tool actually returned
    store_backed = any(e.get("ref") in issued for e in (resp.get("evidence") or []))
    evidence_ok = (not needs_evidence) or (bool(resp.get("evidence")) and not invalid_refs and store_backed)
    invented = bool(expected["should_abstain"]) and resp.get("normalized_value") is not None
    passed = bool(status_ok and issue_ok and numeric_ok in (True, None) and identity_ok in (True, None)
                  and evidence_ok and not invented)
    return {
        "status_ok": status_ok, "issue_ok": issue_ok, "numeric_ok": numeric_ok, "identity_ok": identity_ok,
        "evidence_ok": evidence_ok, "invalid_evidence_refs": invalid_refs, "invented_value": invented, "passed": passed,
        "predicted_issue": status != "verified", "abstained": abstained,
        "abstain_expected": bool(expected["should_abstain"]),
        "rubric": rubric_scores(resp, issued, input_refs, tolerance),
    }


def root_cause(category: str, subtype: str | None, score: dict[str, Any], resp: dict[str, Any] | None,
               agent_error: str | None) -> str | None:
    """Rule-based root-cause label for a FAILED case (None when passed)."""
    if score and score.get("passed"):
        return None
    if agent_error or resp is None:
        return "agent_error"
    if category == "CLEAN_CONTROL":
        return "insufficient_evidence" if score["abstained"] else "false_alarm"
    if not score["evidence_ok"]:
        return "unsupported_claim"
    if category == "MULTI_FAULT":
        return {"wrong_company_with_other_companys_value": "identifier_collision", "correct_value_unsupported_currency": "unit_mismatch",
                "quarter_value_labelled_annual": "invalid_period", "missing_fiscal_period": "missing_input",
                "restated_filing_versions": "conflicting_sources", "unidentifiable_company_no_evidence": "insufficient_evidence",
                "duplicate_feed_equivalent": "false_alarm"}.get(subtype or "", "unclassified")
    return {
        "SCALE_ERROR": "scale_mismatch",
        "ID_MISMAP": "identifier_collision",
        "MISSING_DATA": "missing_input",
        "CONTRADICTION": "conflicting_sources",
        "PERIOD_SHIFT": "invalid_period",
    }.get(category, "unclassified")
