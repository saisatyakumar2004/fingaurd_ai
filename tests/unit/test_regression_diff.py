from src.evaluation.metrics import aggregate
from src.regression.compare import diff_runs

TH = {"max_overall_accuracy_drop": 0.02, "max_category_accuracy_drop": 0.05, "max_recall_drop": 0.05,
      "max_fpr_increase": 0.02, "max_high_conf_wrong_increase": 0, "critical_categories": ["ID_MISMAP"],
      "max_new_failures_critical": 0}


def mk(case_id, cat, passed, conf=0.9, status="flagged"):
    return {"case_id": case_id, "category": cat, "subtype": None, "severity": "critical" if cat == "ID_MISMAP" else "low",
            "expected": {"is_error": cat != "CLEAN_CONTROL"}, "root_cause": None if passed else "x",
            "response": {"confidence": conf, "status": status, "normalized_value": None, "detected_issue_types": []},
            "score": {"passed": passed, "predicted_issue": True, "abstained": False, "abstain_expected": False,
                      "invented_value": False, "evidence_ok": True, "rubric": {"evidence_relevance": 1.0}}}


def snap(run_id, results):
    cases = {r["case_id"]: {"category": r["category"], "severity": r["severity"], "passed": r["score"]["passed"],
                            "status": r["response"]["status"], "normalized_value": None, "issues": [],
                            "confidence": r["response"]["confidence"]} for r in results}
    return {"run_id": run_id, "meta": {"test_suite_version": "s", "dataset_snapshot": "d"}, "metrics": aggregate(results), "cases": cases}


def test_identical_runs_pass():
    rs = [mk(f"c{i}", "ID_MISMAP", True) for i in range(5)] + [mk(f"d{i}", "CLEAN_CONTROL", True) for i in range(5)]
    d = diff_runs(snap("a", rs), snap("b", rs), TH)
    assert d["passed"] and not d["newly_failing"] and not d["changed_answers"]


def test_critical_regression_not_hidden_by_overall_improvement():
    base = [mk(f"c{i}", "ID_MISMAP", True) for i in range(5)] + [mk(f"d{i}", "CLEAN_CONTROL", False) for i in range(20)]
    cur = [mk(f"c{i}", "ID_MISMAP", i != 0) for i in range(5)] + [mk(f"d{i}", "CLEAN_CONTROL", True) for i in range(20)]
    d = diff_runs(snap("a", base), snap("b", cur), TH)
    assert d["metric_deltas"]["accuracy"] > 0                       # overall got better ...
    rules = {b["rule"] for b in d["breaches"]}
    assert not d["passed"] and "critical_category_new_failures" in rules and "category_accuracy_drop" in rules
    assert d["newly_failing"] == ["c0"] and len(d["newly_passing"]) == 20


def test_changed_answers_and_hc_wrong_breach():
    base = [mk("c0", "CLEAN_CONTROL", True, 0.5, "verified")]
    cur = [mk("c0", "CLEAN_CONTROL", False, 0.95, "flagged")]
    d = diff_runs(snap("a", base), snap("b", cur), TH)
    assert d["changed_answers"][0]["before"]["status"] == "verified"
    assert any(b["rule"] == "high_confidence_wrong_increase" for b in d["breaches"])


def test_baseline_tied_to_suite_snapshot_and_config():
    from src.regression.compare import incompatibilities
    rs = [mk("c0", "CLEAN_CONTROL", True)]
    base = snap("a", rs)
    base["meta"]["evaluation_config"] = '{"relative_tolerance":0.005}'
    same = snap("b", rs)
    same["meta"]["evaluation_config"] = '{"relative_tolerance":0.005}'
    assert incompatibilities(base, same) == []
    for key, val in (("test_suite_version", "other"), ("dataset_snapshot", "other"), ("evaluation_config", '{"relative_tolerance":0.05}')):
        cur = snap("c", rs)
        cur["meta"]["evaluation_config"] = '{"relative_tolerance":0.005}'
        cur["meta"][key] = val
        assert len(incompatibilities(base, cur)) == 1
    other_cases = snap("d", rs + [mk("c1", "CLEAN_CONTROL", True)])
    other_cases["meta"]["evaluation_config"] = '{"relative_tolerance":0.005}'
    assert any("case sets differ" in x for x in incompatibilities(base, other_cases))


def test_model_and_prompt_changes_are_reported_as_context_not_hidden():
    rs = [mk("c0", "CLEAN_CONTROL", True)]
    a, b = snap("a", rs), snap("b", rs)
    b["meta"].update(model_id="m2", prompt_version="v2")
    d = diff_runs(a, b, TH)
    assert d["context_changes"]["model_id"] == {"baseline": None, "current": "m2"} and "prompt_version" in d["context_changes"]
