"""LLM adapter + judge exercised with scripted FAKE providers (fixtures). This proves the plumbing, NOT any model's quality."""
import json

import pytest

from src.agent.llm_agent import AgentError, LLMAgent, ProviderUnavailable, extract_json
from src.agent.tools import ReferenceStore
from src.corruption.engine import generate_cases
from src.corruption.generate import persist_cases
from src.evaluation.judge import agreement_report
from src.evaluation.judge_runner import judge_case, parse_judge_output
from src.evaluation.llm_report import build_llm_report, render_html
from src.evaluation.run import main as eval_main, run_evaluation
from src.quality.validate import run_validation
from tests.helpers import build_db


class Scripted:
    def __init__(self, replies, model_id="fake-model-1", usage=(11, 7)):
        self.replies, self.model_id, self.calls = list(replies), model_id, 0
        self.usage = usage
        self.last_usage = {}

    def complete(self, system, messages):
        self.calls += 1
        self.last_usage = {"input_tokens": self.usage[0], "output_tokens": self.usage[1]}
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


FINAL = {"action": "final", "response": {"status": "insufficient_evidence", "answer": "cannot verify", "normalized_value": None,
                                         "unit": None, "company_identifier": None, "fiscal_period": None, "confidence": 0.4,
                                         "evidence": [], "explanation": "No evidence was retrieved for this record.",
                                         "detected_issue_types": ["missing_input"], "recommended_action": "escalate"}}
INPUT = {"case_id": "case-x", "task": "verify_records", "records": [{"record_id": "rec-1", "ticker": "TSTA"}]}


@pytest.fixture(scope="module")
def db():
    return build_db(n=2)


def agent(provider, tmp_path, **kw):
    return LLMAgent(provider, tmp_path, dataset_snapshot="snap", sleep=lambda s: None, **kw)


def test_tool_loop_usage_and_cache_keyed_by_model_and_prompt(db, tmp_path):
    con, _ = db
    tool = {"action": "tool", "tool": "lookup_company", "args": {"identifier": "TSTA"}}
    p = Scripted([json.dumps(tool), json.dumps(FINAL)])
    a = agent(p, tmp_path)
    store = ReferenceStore(con)
    resp = a.run(INPUT, store)
    assert resp.status == "insufficient_evidence" and p.calls == 2
    assert store.trace[0]["tool"] == "lookup_company" and "CIK0009000001" in store.issued
    assert a.usage["input_tokens"] == 22 and a.usage["output_tokens"] == 14 and a.usage["calls"] == 2
    store2 = ReferenceStore(con)
    again = agent(Scripted([]), tmp_path).run(INPUT, store2)           # served from cache: no provider call
    assert again.model_dump() == resp.model_dump() and "CIK0009000001" in store2.issued
    other = Scripted([json.dumps(FINAL)], model_id="fake-model-2")      # different model id must not reuse the entry
    agent(other, tmp_path).run(INPUT, ReferenceStore(con))
    assert other.calls == 1


def test_provider_outage_is_bounded_and_never_invents_an_answer(db, tmp_path):
    p = Scripted([RuntimeError("503")] * 4)
    with pytest.raises(ProviderUnavailable):
        agent(p, tmp_path, max_attempts=3).run(INPUT, ReferenceStore(db[0]))
    assert p.calls == 3


@pytest.mark.parametrize("reply", ["not json at all", "{broken", json.dumps({"action": "dance"}),
                                   json.dumps({"action": "final", "response": {**FINAL["response"], "confidence": 7}}),
                                   json.dumps({"action": "final", "response": {**FINAL["response"], "status": "great"}})])
def test_malformed_or_invalid_model_output_raises_instead_of_fabricating(db, tmp_path, reply):
    with pytest.raises(Exception) as e:
        agent(Scripted([reply]), tmp_path).run(INPUT, ReferenceStore(db[0]))
    assert isinstance(e.value, (AgentError, ValueError))


def test_evaluation_records_agent_errors_as_failures_with_no_invented_response(tmp_path):
    con, settings = build_db()
    run_validation(con, settings)
    cases, _, _ = generate_cases(con, settings, seed=42, n_per_category=3)
    persist_cases(con, cases)

    class Down:
        model_id = "down"
        prompt_version = "v1"
        usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cache_hits": 0, "errors": 0}

        def run(self, agent_input, store):
            raise ProviderUnavailable("model provider unavailable after 3 attempts: ConnectionError")

    out = run_evaluation(con, settings, Down(), mode="full", reports_dir=tmp_path, agent_label="llm")
    assert out["metrics"]["total"] == 18 and out["metrics"]["passed"] == 0
    assert all(r["response"] is None and r["agent_error"] and r["root_cause"] == "agent_error" for r in out["results"])
    assert out["execution"]["provider_unavailable"] == 18 and out["execution"]["agent_errors"] == 18
    rep = build_llm_report(status="PARTIAL", out=out)
    assert rep["metrics"]["total"] == 18 and "ProviderUnavailable" in render_html(rep)


def test_smoke30_is_balanced_and_deterministic(tmp_path):
    con, settings = build_db()
    run_validation(con, settings)
    cases, _, _ = generate_cases(con, settings, seed=42, n_per_category=20)
    persist_cases(con, cases)
    from src.agent.mock_agent import MockAgent
    a = run_evaluation(con, settings, MockAgent("reference"), mode="smoke30", reports_dir=tmp_path)
    b = run_evaluation(con, settings, MockAgent("reference"), mode="smoke30", reports_dir=tmp_path)
    assert a["metrics"]["total"] == 30 and {v["n"] for v in a["metrics"]["by_category"].values()} == {5}
    assert [r["case_id"] for r in a["results"]] == [r["case_id"] for r in b["results"]]


def test_llm_without_credentials_reports_not_run_and_exit_5(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("src.evaluation.run.load_env", lambda: None)      # tests must never read the developer's real .env
    monkeypatch.setenv("FINGUARD_REPORTS_DIR", str(tmp_path))
    path = tmp_path / "x.duckdb"
    c2, _ = build_db(path=str(path))
    c2.close()
    assert eval_main(["--db", str(path), "--agent", "llm", "--mode", "smoke30"]) == 5
    rep = json.loads((tmp_path / "llm_evaluation.json").read_text())
    assert rep["status"] == "NOT_RUN" and rep["metrics"] is None


def test_extract_json_handles_wrapped_text():
    assert extract_json('Sure! {"a": 1} done')["a"] == 1


GOOD = '{"evidence_relevance": 2, "factual_consistency": 1, "reasoning_completeness": 2, "comment": "ok"}'


def test_judge_parses_preserves_raw_and_flags_malformed():
    ok = parse_judge_output("c1", "Here: " + GOOD, "fake-judge")
    assert ok.ok and ok.scores["factual_consistency"] == 1 and ok.raw.startswith("Here:")
    for bad in ("nonsense", '{"evidence_relevance": 5, "factual_consistency": 1, "reasoning_completeness": 1}', '{"evidence_relevance": 1}'):
        r = parse_judge_output("c2", bad, "fake-judge")
        assert not r.ok and r.scores is None and r.raw == bad and r.error


def test_judge_provider_failure_recorded_not_guessed():
    class Boom:
        model_id = "fake-judge"

        def complete(self, s, m):
            raise TimeoutError("x")

    r = judge_case(Boom(), "c3", {"status": "verified"}, [])
    assert not r.ok and "provider_error" in r.error and r.scores is None


def test_judge_receives_no_expected_answers():
    seen = {}

    class Spy:
        model_id = "spy"

        def complete(self, system, messages):
            seen["payload"] = messages[0]["content"]
            return GOOD

    judge_case(Spy(), "c4", {"status": "flagged", "explanation": "e"}, [{"ref": "obs1", "value": 1.0}])
    assert "expected" not in seen["payload"] and "corruption" not in seen["payload"]


def test_agreement_report_arithmetic():
    rep = agreement_report(["a", "b", "c", "d"], ["2", "2", "1", "0"], ["2", "1", "1", "0"])
    assert rep["percent_agreement"] == 0.75 and rep["disagreements"][0]["case_id"] == "b" and rep["cohens_kappa"] is not None
    with pytest.raises(ValueError):
        agreement_report([], [], [])
