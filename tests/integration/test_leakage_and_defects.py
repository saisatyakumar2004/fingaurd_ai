"""Evaluator-integrity tests: restricted data must be unreachable by the agent, and every generated
case must really exhibit its intended defect."""
import json

import pytest

from src.agent.tools import ReferenceStore
from src.corruption.engine import CATEGORIES, generate_cases, load_context
from src.corruption.generate import persist_cases
from src.identity.resolver import resolve_entity
from src.normalization.units import power_of_ten_ratio, to_base, within_tolerance
from src.quality.validate import run_validation
from tests.helpers import build_db


@pytest.fixture(scope="module")
def env():
    con, settings = build_db()
    run_validation(con, settings)
    cases, _, _ = generate_cases(con, settings, seed=42, n_per_category=20)
    persist_cases(con, cases)
    return con, settings, cases


# ---------------------------------------------------------------- leakage / permissions
RESTRICTED = ["test_cases", "evaluation_results", "agent_runs", "regression_baselines", "quality_issues"]


def test_store_exposes_only_whitelisted_tools(env):
    con, _, cases = env
    store = ReferenceStore(con)
    for tool in ("query", "sql", "execute", "get_test_case", "get_expected", "list_tables", "__init__", "call", "_find_companies"):
        res = store.call(tool, {"sql": "SELECT * FROM test_cases"})
        assert "error" in res and "unknown tool" in res["error"]


@pytest.mark.parametrize("payload", ["' OR 1=1 --", "x'; SELECT * FROM test_cases; --", "../../etc/passwd", "test_cases"])
def test_tool_arguments_cannot_reach_restricted_tables(env, payload):
    con, _, cases = env
    store = ReferenceStore(con)
    blob = json.dumps([store.call("lookup_company", {"identifier": payload}),
                       store.call("get_provenance", {"observation_id": payload}),
                       store.call("lookup_instrument", {"figi": payload}),
                       store.call("get_observations", {"company": payload, "concept": payload})], default=str)
    for c in cases[:30]:
        assert c["case_id"] not in blob
    for word in ("expected_statuses", "corruption_parameters", "store_overrides", "evaluation_results"):
        assert word not in blob


def test_tool_outputs_never_contain_case_metadata(env):
    con, _, cases = env
    forbidden = ("expected", "corruption", "store_overrides", "SCALE_ERROR", "ID_MISMAP", "CLEAN_CONTROL", "case-")
    for c in cases[:40]:
        store = ReferenceStore(con, c["store_overrides"]["exclude_keys"])
        rec = c["agent_input"]["records"][0]
        outs = [store.call("lookup_company", {"identifier": rec["ticker"]}),
                store.call("get_observations", {"company": rec["cik"], "concept": rec["concept"]})]
        blob = json.dumps(outs, default=str)
        assert not any(w in blob for w in forbidden), c["case_id"]


def test_excluded_period_is_invisible_through_every_tool(env):
    con, _, cases = env
    missing = next(c for c in cases if c["corruption_type"] == "MISSING_DATA")
    ex = missing["store_overrides"]["exclude_keys"][0]
    store = ReferenceStore(con, [ex])
    comp = store.call("lookup_company", {"identifier": missing["agent_input"]["records"][0]["ticker"]})["company"]
    got = store.call("get_observations", {"company": comp["cik"], "concept": ex["concept"], "period_end": ex["period_end"]})
    assert got["observations"] == []
    ctx = load_context(con)
    obs_id = next(o["observation_id"] for o in ctx.obs.values() if o["company_id"] == ex["company_id"]
                  and o["concept"] == ex["concept"] and o["fiscal_period_end"].isoformat() == ex["period_end"])
    assert store.call("get_provenance", {"observation_id": obs_id})["found"] is False


def test_agent_input_schema_is_closed(env):
    _, _, cases = env
    allowed_rec = {"record_id", "source_label", "company_name", "ticker", "cik", "concept", "period_type", "fiscal_period_end",
                   "value", "unit", "filing_date", "accession_number", "figi"}
    for c in cases:
        assert set(c["agent_input"]) == {"case_id", "task", "records"}
        for r in c["agent_input"]["records"]:
            assert set(r) <= allowed_rec


# ---------------------------------------------------------------- each case really exhibits its defect
def _golden(ctx, case):
    return ctx.obs[case["affected_record_id"]]


def test_every_case_exhibits_its_intended_defect(env):
    con, settings, cases = env
    ctx = load_context(con)
    tol = settings["evaluation"]["relative_tolerance"]
    seen = set()
    for c in cases:
        o = _golden(ctx, c)
        recs, v = c["agent_input"]["records"], o["value"]
        company = ctx.companies[o["company_id"]]
        t, sub = c["corruption_type"], c["subtype"]
        seen.add((t, sub))
        r = recs[0]
        if t == "SCALE_ERROR":
            base, _ = to_base(r["value"], r["unit"])
            assert not within_tolerance(base, v, tol)
            assert power_of_ten_ratio(base, v) in (3, 6, -3, -6), (sub, base, v)
        elif t == "ID_MISMAP":
            if sub == "cik_swap":
                assert int(r["cik"]) != company["cik"] and r["ticker"] == company["ticker"]
            else:
                assert r["figi"] != ctx.figi[o["company_id"]]
        elif t == "MISSING_DATA":
            ex = c["store_overrides"]["exclude_keys"][0]
            assert ex["company_id"] == o["company_id"] and ex["period_end"] == o["fiscal_period_end"].isoformat()
            if sub == "missing_value":
                assert r["value"] is None
        elif t == "CONTRADICTION":
            a, b = recs
            assert a["record_id"] != b["record_id"] and a["concept"] == b["concept"] and a["fiscal_period_end"] == b["fiscal_period_end"]
            ba, _ = to_base(a["value"], a["unit"])
            bb, _ = to_base(b["value"], b["unit"])
            if sub == "conflicting_values":
                assert not within_tolerance(bb, ba, tol)
            else:
                assert within_tolerance(bb, ba, tol) and a["unit"] != b["unit"]
        elif t == "PERIOD_SHIFT":
            assert r["fiscal_period_end"] != o["fiscal_period_end"].isoformat() and r["value"] == v
        else:  # CLEAN_CONTROL
            base, _ = to_base(r["value"], r["unit"])
            assert within_tolerance(base, v, tol) and int(r["cik"]) == company["cik"]
            assert r["fiscal_period_end"] == o["fiscal_period_end"].isoformat()
            assert c["expected"]["is_error"] is False
    assert {t for t, _ in seen} == set(CATEGORIES)
    assert {("SCALE_ERROR", "mul_value"), ("SCALE_ERROR", "div_value"), ("SCALE_ERROR", "mislabel_unit")} <= seen


def test_scale_factors_1000_and_1000000_both_generated(env):
    _, _, cases = env
    factors = {s["params"]["factor"] for c in cases if c["corruption_type"] == "SCALE_ERROR"
               for s in c["corruption_parameters"]["steps"] if s["transform"] == "scale_value"}
    units = {s["params"]["to"] for c in cases if c["corruption_type"] == "SCALE_ERROR"
             for s in c["corruption_parameters"]["steps"] if s["transform"] == "set_field"}
    assert factors == {1000, 1000000} or {1000, 1000000} <= factors | {1000 if "USDthousands" in units else 0, 1000000 if "USDmillions" in units else 0}


def test_golden_observations_unchanged_after_generation_and_expected_independent_of_agent(env):
    con, settings, cases = env
    for c in cases:
        assert c["expected"] and "response" not in c["expected"]
    before = con.execute("SELECT count(*), sum(value) FROM financial_observations").fetchone()
    generate_cases(con, settings, seed=42, n_per_category=20)
    assert con.execute("SELECT count(*), sum(value) FROM financial_observations").fetchone() == before


# ---------------------------------------------------------------- invalid configuration fails clearly
@pytest.mark.parametrize("n", [0, -3])
def test_invalid_case_counts_fail_clearly(env, n):
    con, settings, _ = env
    with pytest.raises(ValueError, match="n_per_category"):
        generate_cases(con, settings, seed=1, n_per_category=n)


def test_invalid_seed_type_fails_clearly(env):
    con, settings, _ = env
    with pytest.raises(ValueError, match="seed"):
        generate_cases(con, settings, seed="abc", n_per_category=2)  # type: ignore[arg-type]


# ---------------------------------------------------------------- identity fixtures (deterministic, SYNTHETIC)
class Cand:
    def __init__(self, lei, name, jur="US-DE", status="ACTIVE"):
        self.lei, self.legal_name, self.jurisdiction, self.entity_status = lei, name, jur, status


def test_incorrect_mapping_expected_lei_not_matching_name_is_not_verified():
    r1 = resolve_entity("Alpha Test Corp", [Cand("LEI-A", "Alpha Test Corp")], expected_lei="LEI-WRONG")
    r2 = resolve_entity("Bravo Test Corp", [Cand("LEI-B", "Bravo Test Corp"), Cand("LEI-C", "Charlie Test Corp")], expected_lei="LEI-C")
    for r in (r1, r2):
        assert r.status == "AMBIGUOUS" and r.selected_lei is None and "expected LEI" in r.rationale


def test_correct_mapping_requires_independent_corroboration_for_verified():
    assert resolve_entity("Alpha Test Corp", [Cand("LEI-A", "Alpha Test Corp")]).status == "CANDIDATE_UNIQUE"
    assert resolve_entity("Alpha Test Corp", [Cand("LEI-A", "Alpha Test Corp")], expected_lei="LEI-A").status == "VERIFIED"


def test_ambiguous_and_unmatched_fixtures():
    amb = resolve_entity("Delta Test Corp", [Cand("L1", "Delta Test Corp", "FR-X"), Cand("L2", "Delta Test", "DE-X")])
    assert amb.status == "AMBIGUOUS" and len(amb.candidates) == 2 and amb.selected_lei is None
    assert resolve_entity("Echo Test Corp", []).status == "UNMATCHED"
    assert resolve_entity("Echo Test Corp", [Cand("L3", "Totally Different Ltd")]).status == "UNMATCHED"
