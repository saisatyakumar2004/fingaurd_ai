import pytest

from src.corruption.transforms import apply_steps, reverse_steps
from src.evaluation.metrics import aggregate, calibration, cohens_kappa
from src.evaluation.scoring import numeric_check, root_cause, score_case

REC = {"record_id": "r1", "cik": "0000000001", "value": 1234.5, "unit": "USD", "filing_date": "2024-01-01",
       "accession_number": "a"}


@pytest.mark.parametrize("steps", [
    [{"transform": "scale_value", "params": {"op": "mul", "factor": 1000}}],
    [{"transform": "scale_value", "params": {"op": "div", "factor": 1000000}}],
    [{"transform": "set_field", "params": {"field": "cik", "from": "0000000001", "to": "0000000002"}}],
    [{"transform": "set_field", "params": {"field": "value", "from": 1234.5, "to": None}}],
    [{"transform": "delete_field", "params": {"field": "filing_date", "original": "2024-01-01"}},
     {"transform": "scale_value", "params": {"op": "div", "factor": 1000}},
     {"transform": "set_field", "params": {"field": "unit", "from": "USD", "to": "USDthousands"}}],
])
def test_corruption_is_reversible_and_does_not_mutate_input(steps):
    before = dict(REC)
    corrupted = apply_steps(REC, steps)
    assert REC == before and corrupted != REC
    restored = reverse_steps(corrupted, steps)
    assert restored.keys() == REC.keys()
    for k in REC:
        assert restored[k] == pytest.approx(REC[k]) if isinstance(REC[k], float) else restored[k] == REC[k]


def test_numeric_check_unit_aware():
    assert numeric_check(1000.0, 1.0, "USDthousands", 0.005) is True
    assert numeric_check(1000.0, 1000.0, "EUR", 0.005) is False            # tolerance cannot hide currency
    assert numeric_check(1000.0, 1000.0, None, 0.005) is False
    assert numeric_check(1000.0, 1_000_000.0, "USD", 0.005) is False       # 1000x scale error
    assert numeric_check(1000.0, 1004.0, "USD", 0.005) is True
    assert numeric_check(1000.0, 1004.0, "USD", 0.005, exact=True) is False
    assert numeric_check(None, 5.0, "USD", 0.005) is None


def resp(**kw):
    base = {"case_id": "c", "status": "flagged", "answer": "a", "normalized_value": 1000.0, "unit": "USD",
            "company_identifier": "0000000001", "fiscal_period": None, "confidence": 0.9,
            "evidence": [{"source": "reference_store", "ref": "obs1", "value": 1000.0, "detail": ""}],
            "explanation": "x" * 40, "detected_issue_types": ["scale_mismatch"], "recommended_action": "fix"}
    base.update(kw)
    return base


EXP = {"is_error": True, "expected_statuses": ["flagged"], "expected_issue_types": ["scale_mismatch"], "should_abstain": False,
       "expected_value": 1000.0, "exact_value": False, "expected_company_identifier": "0000000001"}


def test_score_case_pass_and_failure_modes():
    ok = score_case(EXP, resp(), {"obs1": 1000.0}, {"r1"}, 0.005)
    assert ok["passed"] and ok["rubric"]["evidence_relevance"] == 1.0 and ok["rubric"]["factual_consistency"] == 1.0
    assert not score_case(EXP, resp(status="verified", detected_issue_types=[]), {}, set(), 0.005)["passed"]
    assert not score_case(EXP, resp(company_identifier="0000000002"), {}, set(), 0.005)["passed"]
    assert not score_case(EXP, resp(normalized_value=1_000_000.0), {}, set(), 0.005)["passed"]
    assert not score_case(EXP, resp(evidence=[]), {}, set(), 0.005)["passed"]         # material claim w/o evidence
    bad_ref = score_case(EXP, resp(), {}, set(), 0.005)
    assert bad_ref["rubric"]["evidence_relevance"] == 0.0


def test_abstention_cases_penalise_invented_values():
    exp = {**EXP, "expected_statuses": ["insufficient_evidence"], "expected_issue_types": ["missing_input"],
           "should_abstain": True, "expected_value": None, "expected_company_identifier": None}
    good = resp(status="insufficient_evidence", normalized_value=None, unit=None, evidence=[], detected_issue_types=["missing_input"])
    assert score_case(exp, good, {}, set(), 0.005)["passed"]
    invented = resp(status="insufficient_evidence", normalized_value=5.0, evidence=[], detected_issue_types=["missing_input"])
    s = score_case(exp, invented, {}, set(), 0.005)
    assert s["invented_value"] and not s["passed"]


def test_root_cause_labels():
    s = {"passed": False, "abstained": False, "evidence_ok": True}
    assert root_cause("SCALE_ERROR", None, s, {"x": 1}, None) == "scale_mismatch"
    assert root_cause("ID_MISMAP", None, s, {"x": 1}, None) == "identifier_collision"
    assert root_cause("PERIOD_SHIFT", None, s, {"x": 1}, None) == "invalid_period"
    assert root_cause("CLEAN_CONTROL", None, s, {"x": 1}, None) == "false_alarm"
    assert root_cause("CLEAN_CONTROL", None, {**s, "abstained": True}, {"x": 1}, None) == "insufficient_evidence"
    assert root_cause("SCALE_ERROR", None, s, None, "boom") == "agent_error"
    assert root_cause("SCALE_ERROR", None, {"passed": True}, {"x": 1}, None) is None


def _res(cat, passed, predicted, is_error, conf, abstained=False, should=False, sev="high"):
    return {"case_id": f"{cat}-{passed}-{conf}-{predicted}", "category": cat, "subtype": None, "severity": sev,
            "expected": {"is_error": is_error}, "response": {"confidence": conf, "status": "flagged"}, "root_cause": None if passed else "x",
            "score": {"passed": passed, "predicted_issue": predicted, "abstained": abstained, "abstain_expected": should,
                      "invented_value": False, "evidence_ok": True, "rubric": {"evidence_relevance": 1.0}}}


def test_aggregate_detection_and_fpr_known_values():
    rs = [_res("A", True, True, True, 0.9), _res("A", False, False, True, 0.9),      # tp, fn (high-conf wrong)
          _res("CLEAN_CONTROL", True, False, False, 0.7), _res("CLEAN_CONTROL", False, True, False, 0.6)]  # tn, fp
    m = aggregate(rs)
    d = m["detection"]
    assert (d["tp"], d["fn"], d["fp"], d["tn"]) == (1, 1, 1, 1)
    assert d["precision"] == 0.5 and d["recall"] == 0.5 and d["f1"] == 0.5
    assert m["false_positive_rate"] == 0.5 and m["accuracy"] == 0.5
    assert m["high_confidence_wrong"]["count"] == 1
    assert m["by_category"]["A"]["accuracy"] == 0.5


def test_calibration_brier_ece():
    c = calibration([1.0, 1.0, 0.0, 0.0], [True, False, False, True], 10)
    assert c["brier"] == pytest.approx(0.5)
    assert c["ece"] == pytest.approx(0.5)
    perfect = calibration([1.0, 0.0], [True, False])
    assert perfect["brier"] == 0 and perfect["ece"] == 0
    assert sum(b["n"] for b in c["bins"]) == 4


def test_cohens_kappa():
    assert cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert cohens_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    assert cohens_kappa([], []) is None
