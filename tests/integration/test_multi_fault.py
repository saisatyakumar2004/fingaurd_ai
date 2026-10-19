import json

import pytest

from src.agent.mock_agent import MockAgent
from src.corruption.engine import generate_cases, load_context
from src.corruption.generate import persist_cases
from src.corruption.multi import MULTI_SUBTYPES, generate_multi_cases
from src.corruption.transforms import apply_steps, reverse_steps
from src.evaluation.run import run_evaluation
from src.normalization.units import to_base, within_tolerance
from src.quality.validate import run_validation
from tests.helpers import build_db


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    con, settings = build_db()
    run_validation(con, settings)
    base, _, _ = generate_cases(con, settings, seed=42, n_per_category=20)
    persist_cases(con, base)
    multi, suite, short = generate_multi_cases(con, settings, seed=42, n_per_subtype=5)
    persist_cases(con, multi, scope="multi")
    return con, settings, base, multi, suite, short, tmp_path_factory.mktemp("rep")


def test_at_least_30_cases_all_subtypes_present_and_deterministic(env):
    con, settings, base, multi, suite, short, _ = env
    assert not short and len(multi) >= 30
    assert {c["subtype"] for c in multi} == set(MULTI_SUBTYPES)
    again, suite2, _ = generate_multi_cases(con, settings, seed=42, n_per_subtype=5)
    assert [c["case_id"] for c in again] == [c["case_id"] for c in multi] and suite == suite2
    assert [c["case_id"] for c in generate_multi_cases(con, settings, seed=9, n_per_subtype=5)[0]] != [c["case_id"] for c in multi]


def test_each_case_has_rationale_expected_behavior_evidence_severity(env):
    for c in env[3]:
        assert c["corruption_parameters"]["rationale"] and c["expected_behavior"] and c["source_evidence"] and c["severity"]
        assert c["case_id"].startswith("mcase-") and c["expected"]["expected_statuses"]


def test_base_suite_untouched_by_multi_persistence(env):
    con, _, base, multi, _, _, _ = env
    n_base = con.execute("SELECT count(*) FROM test_cases WHERE suite_version NOT LIKE 'multi-%'").fetchone()[0]
    n_multi = con.execute("SELECT count(*) FROM test_cases WHERE suite_version LIKE 'multi-%'").fetchone()[0]
    assert n_base == len(base) == 120 and n_multi == len(multi)


def test_every_multi_case_exhibits_its_defect(env):
    con, settings, _, multi, _, _, _ = env
    ctx = load_context(con)
    tol = settings["evaluation"]["relative_tolerance"]
    for c in multi:
        o = ctx.obs[c["affected_record_id"]]
        recs, sub, v = c["agent_input"]["records"], c["subtype"], o["value"]
        r = recs[0]
        if sub == "wrong_company_with_other_companys_value":
            assert int(r["cik"]) != ctx.companies[o["company_id"]]["cik"] and r["ticker"] == ctx.companies[o["company_id"]]["ticker"]
            assert r["value"] != v
        elif sub == "correct_value_unsupported_currency":
            assert r["unit"] == "EUR" and r["value"] == v
        elif sub == "quarter_value_labelled_annual":
            assert not within_tolerance(r["value"], v, 0.03) and r["fiscal_period_end"] == o["fiscal_period_end"].isoformat()
        elif sub == "missing_fiscal_period":
            assert r["fiscal_period_end"] is None
        elif sub == "restated_filing_versions":
            old, new = recs
            assert old["accession_number"] != new["accession_number"] and not within_tolerance(old["value"], new["value"], 0.01)
            assert new["value"] == v
        elif sub == "unidentifiable_company_no_evidence":
            assert r["cik"] is None and r["ticker"] == "ZZQX" and c["store_overrides"]["exclude_keys"]
        else:
            a, b = recs
            assert a["record_id"] != b["record_id"] and to_base(a["value"], a["unit"]) == to_base(b["value"], b["unit"])
            assert c["expected"]["is_error"] is False
        # reversibility of the recorded steps
        steps = c["corruption_parameters"]["steps"]
        if steps and sub != "restated_filing_versions":
            restored = reverse_steps(r, steps)
            for k, val in c["original_value"].items():
                assert restored.get(k) == val or restored.get(k) == pytest.approx(val)


def test_multi_agent_input_is_closed_and_unlabelled(env):
    banned = ["expected", "corruption", "rationale", "MULTI_FAULT", "subtype", "severity", "store_overrides", "exclude_keys", "mcase"]
    for c in env[3]:
        assert set(c["agent_input"]) == {"case_id", "task", "records"}
        blob = json.dumps(c["agent_input"])
        assert not [w for w in banned if w in blob.replace(c["case_id"], "")]


def test_multi_suite_evaluation_reference_vs_naive(env):
    con, settings, _, multi, _, _, reports = env
    ref = run_evaluation(con, settings, MockAgent("reference"), mode="full", suite="multi", reports_dir=reports)
    naive = run_evaluation(con, settings, MockAgent("naive"), mode="full", suite="multi", reports_dir=reports, agent_label="mock-naive")
    assert ref["metrics"]["total"] == naive["metrics"]["total"] == len(multi)
    assert ref["metrics"]["accuracy"] >= 0.9 and naive["metrics"]["accuracy"] < 0.4
    assert set(ref["metrics"]["by_category"]) == {"MULTI_FAULT"}
    with pytest.raises(ValueError):
        run_evaluation(con, settings, MockAgent("reference"), suite="nonsense", reports_dir=reports)


@pytest.mark.parametrize("n", [0, -1])
def test_invalid_multi_config_fails_clearly(env, n):
    with pytest.raises(ValueError, match="n_per_subtype"):
        generate_multi_cases(env[0], env[1], seed=1, n_per_subtype=n)
