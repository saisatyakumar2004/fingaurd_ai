"""End-to-end, fully offline: synthetic reference DB -> validate -> generate -> evaluate -> report -> regression."""
import json

import pytest

from src.agent.mock_agent import MockAgent
from src.corruption.engine import CATEGORIES, generate_cases
from src.corruption.generate import persist_cases
from src.corruption.transforms import reverse_steps
from src.evaluation.run import GateBlocked, run_evaluation
from src.quality.validate import run_validation
from src.regression.compare import diff_runs, load_run_snapshot, set_baseline, load_baseline
from src.reporting.generate import build_scorecard, render_html
from tests.helpers import build_db


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    con, settings = build_db()
    run_validation(con, settings)
    cases, suite, short = generate_cases(con, settings, seed=42, n_per_category=20)
    persist_cases(con, cases, tmp_path_factory.mktemp("corrupted"))
    return con, settings, cases, suite, short, tmp_path_factory.mktemp("reports")


def test_at_least_100_cases_20_per_category(env):
    _, _, cases, _, short, _ = env
    assert not short and len(cases) >= 100
    for cat in CATEGORIES:
        assert sum(1 for c in cases if c["corruption_type"] == cat) >= 20


def test_generation_is_deterministic_and_seed_sensitive(env):
    con, settings, cases, suite, _, _ = env
    again, suite2, _ = generate_cases(con, settings, seed=42, n_per_category=20)
    assert [c["case_id"] for c in again] == [c["case_id"] for c in cases] and suite == suite2
    assert json.dumps(again[0]["agent_input"], sort_keys=True) == json.dumps(cases[0]["agent_input"], sort_keys=True)
    other, _, _ = generate_cases(con, settings, seed=7, n_per_category=20)
    assert [c["case_id"] for c in other] != [c["case_id"] for c in cases]


def test_agent_input_has_no_leakage(env):
    _, _, cases, _, _, _ = env
    banned = ["expected", "corruption", "SCALE_ERROR", "ID_MISMAP", "MISSING_DATA", "CONTRADICTION", "PERIOD_SHIFT",
              "CLEAN_CONTROL", "subtype", "severity", "store_overrides", "exclude_keys", "random_seed", "smoke"]
    for c in cases:
        blob = json.dumps(c["agent_input"])
        assert set(c["agent_input"]) == {"case_id", "task", "records"}
        for word in banned:
            assert word not in blob, (c["case_id"], word)


def test_corruption_reversible_on_generated_cases(env):
    _, _, cases, _, _, _ = env
    for c in cases:
        steps = c["corruption_parameters"]["steps"]
        presented = c["agent_input"]["records"][-1]
        if c["corruption_type"] == "CONTRADICTION":
            continue   # record B is derived from A by the steps; checked below
        if steps:
            restored = reverse_steps(presented, steps)
            for k, v in c["original_value"].items():
                assert restored.get(k) == pytest.approx(v) if isinstance(v, float) else restored.get(k) == v
    for c in (x for x in cases if x["corruption_type"] == "CONTRADICTION"):
        a, b = c["agent_input"]["records"]
        restored = reverse_steps(b, c["corruption_parameters"]["steps"])
        assert restored["value"] == pytest.approx(a["value"]) and restored["unit"] == a["unit"]


def test_golden_data_untouched_by_generation(env):
    con, settings, cases, _, _, _ = env
    before = con.execute("SELECT count(*), sum(value) FROM financial_observations").fetchone()
    generate_cases(con, settings, seed=42, n_per_category=20)
    assert con.execute("SELECT count(*), sum(value) FROM financial_observations").fetchone() == before


def test_reference_agent_beats_naive_and_controls_pass(env):
    con, settings, _, _, _, reports = env
    ref = run_evaluation(con, settings, MockAgent("reference"), mode="full", reports_dir=reports, agent_label="mock")
    naive = run_evaluation(con, settings, MockAgent("naive"), mode="full", reports_dir=reports, agent_label="mock-naive")
    assert ref["metrics"]["total"] >= 100
    assert ref["metrics"]["accuracy"] >= 0.95
    assert naive["metrics"]["accuracy"] < ref["metrics"]["accuracy"]
    assert naive["metrics"]["detection"]["recall"] < 0.2
    assert naive["metrics"]["high_confidence_wrong"]["count"] > 50
    assert ref["metrics"]["false_positive_rate"] == 0.0


def test_smoke_mode_runs_subset(env):
    con, settings, _, _, _, reports = env
    out = run_evaluation(con, settings, MockAgent("reference"), mode="smoke", reports_dir=reports)
    assert 0 < out["metrics"]["total"] <= 6 * settings["corruption"]["smoke_per_category"]


def test_gate_blocks_evaluation_when_critical_error_present(env, tmp_path):
    con, settings = build_db()
    con.execute("DELETE FROM sec_ticker_map WHERE ticker = 'TSTA'")
    con.execute("INSERT INTO sec_ticker_map VALUES ('TSTA', 1, 'X', 's')")
    run_validation(con, settings)
    cases, _, _ = generate_cases(con, settings, seed=1, n_per_category=3)
    persist_cases(con, cases)
    with pytest.raises(GateBlocked) as e:
        run_evaluation(con, settings, MockAgent("reference"), reports_dir=tmp_path)
    assert "DQ006" in str(e.value)


def test_scorecard_and_regression_flow(env):
    con, settings, _, _, _, reports = env
    ref_run = run_evaluation(con, settings, MockAgent("reference"), mode="full", reports_dir=reports, agent_label="mock")
    sc = build_scorecard(con, settings, ref_run["run"]["run_id"])
    assert sc["metrics"]["total"] == ref_run["metrics"]["total"] and sc["cases"][0]["source_evidence"]
    html = render_html(sc)
    assert "FinGuard AI scorecard" in html and "<svg" in html
    assert json.loads(json.dumps(sc, default=str))["run"]["run_id"] == ref_run["run"]["run_id"]

    base = load_run_snapshot(con, settings, ref_run["run"]["run_id"])
    set_baseline(con, "t", base)
    same = diff_runs(load_baseline(con, "t"), load_run_snapshot(con, settings, ref_run["run"]["run_id"]), settings["regression"])
    assert same["passed"]

    bad_run = run_evaluation(con, settings, MockAgent("degraded"), mode="full", reports_dir=reports, agent_label="mock-degraded")
    diff = diff_runs(load_baseline(con, "t"), load_run_snapshot(con, settings, bad_run["run"]["run_id"]), settings["regression"])
    assert not diff["passed"] and diff["newly_failing"]
    assert any(b["rule"] in ("critical_category_new_failures", "category_accuracy_drop") for b in diff["breaches"])
