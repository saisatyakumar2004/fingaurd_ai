"""Mocked-provider tests for the real-LLM path: errors, retries, cache, limits, progress, interruption, health checks.
No network, no API key, no running model."""
import json

import pytest
import requests

from src.agent.llm_agent import (InvalidAgentResponse, LLMAgent, LLMSettings, MalformedOutput, MaxStepsExceeded,
                                 OllamaProvider, OpenAICompatibleProvider, ProviderError, ProviderTimeout, ProviderUnavailable,
                                 compact_tool_result)
from src.agent.mock_agent import MockAgent
from src.agent.tools import ReferenceStore
from src.corruption.engine import generate_cases
from src.corruption.generate import persist_cases
from src.evaluation.run import GateBlocked, main as eval_main, run_evaluation
from src.evaluation.scoring import score_case
from src.quality.validate import run_validation
from tests.helpers import build_db

FINAL_OK = {"action": "final", "response": {"status": "insufficient_evidence", "answer": "x", "normalized_value": None, "unit": None,
                                            "company_identifier": None, "fiscal_period": None, "confidence": 0.3, "evidence": [],
                                            "explanation": "Nothing could be retrieved.", "detected_issue_types": ["missing_input"],
                                            "recommended_action": "escalate"}}
INPUT = {"case_id": "case-z", "task": "verify_records", "records": [{"record_id": "rec-1", "ticker": "TSTA"}]}


class Scripted:
    model_id = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, system, messages):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(scope="module")
def db():
    return build_db(n=2)


def mk(provider, tmp_path, **kw):
    return LLMAgent(provider, tmp_path, "snap", sleep=lambda s: None, **kw)


# ------------------------------------------------------------------ error classes and retry policy
def test_timeout_is_retried_once_then_surfaces_as_timeout(db, tmp_path):
    p = Scripted([ProviderTimeout("slow"), ProviderTimeout("slow"), json.dumps(FINAL_OK)])
    with pytest.raises(ProviderTimeout):
        mk(p, tmp_path, llm=LLMSettings(timeout_retries=1, max_attempts=3)).run(INPUT, ReferenceStore(db[0]))
    assert p.calls == 2                                            # 1 try + 1 timeout retry, not 3


def test_timeout_then_success_is_ok(db, tmp_path):
    p = Scripted([ProviderTimeout("slow"), json.dumps(FINAL_OK)])
    assert mk(p, tmp_path).run(INPUT, ReferenceStore(db[0])).status == "insufficient_evidence"


def test_client_errors_are_never_retried(db, tmp_path):
    p = Scripted([ProviderError("HTTP 404: model not found"), json.dumps(FINAL_OK)])
    with pytest.raises(ProviderError):
        mk(p, tmp_path).run(INPUT, ReferenceStore(db[0]))
    assert p.calls == 1


def test_transient_errors_retry_up_to_max_attempts(db, tmp_path):
    p = Scripted([ProviderUnavailable("503")] * 5)
    with pytest.raises(ProviderUnavailable):
        mk(p, tmp_path, max_attempts=3).run(INPUT, ReferenceStore(db[0]))
    assert p.calls == 3


def test_malformed_json_gets_one_corrective_reask_then_fails(db, tmp_path):
    ok = mk(Scripted(["not json", json.dumps(FINAL_OK)]), tmp_path)
    assert ok.run(INPUT, ReferenceStore(db[0])).status == "insufficient_evidence" and ok.usage["corrective_retries"] == 1
    with pytest.raises(MalformedOutput):
        mk(Scripted(["nope", "still nope"]), tmp_path / "b").run(INPUT, ReferenceStore(db[0]))


def test_invalid_final_schema_is_reported_as_invalid_response(db, tmp_path):
    bad = {"action": "final", "response": {**FINAL_OK["response"], "confidence": 5}}
    with pytest.raises(InvalidAgentResponse) as e:
        mk(Scripted([json.dumps(bad), json.dumps(bad)]), tmp_path).run(INPUT, ReferenceStore(db[0]))
    assert "confidence" in str(e.value)


def test_tool_loop_runs_tools_dedupes_repeats_and_respects_step_budget(db, tmp_path):
    con, _ = db
    tool = json.dumps({"action": "tool", "tool": "lookup_company", "args": {"identifier": "TSTA"}})
    p = Scripted([tool, tool, json.dumps(FINAL_OK)])
    a = mk(p, tmp_path)
    store = ReferenceStore(con)
    a.run(INPUT, store)
    assert len(store.trace) == 1 and a.usage["duplicate_tool_calls"] == 1          # second identical call not re-executed
    loop = json.dumps({"action": "tool", "tool": "lookup_company", "args": {"identifier": "TSTB"}})
    with pytest.raises(MaxStepsExceeded):
        mk(Scripted([loop] * 10), tmp_path / "c", max_steps=3).run(INPUT, ReferenceStore(con))


def test_unknown_tool_and_missing_evidence_are_reported_back_not_hidden(db, tmp_path):
    p = Scripted([json.dumps({"action": "tool", "tool": "get_expected_answer", "args": {}}), json.dumps(FINAL_OK)])
    store = ReferenceStore(db[0])
    mk(p, tmp_path).run(INPUT, store)
    assert store.trace[0]["ok"] is False and not store.issued


def test_tool_results_are_compacted_for_small_context():
    big = {"observations": [{"ref": f"r{i}", "value": i, "source_url": "http://x" * 50, "xbrl_tag": "T"} for i in range(20)]}
    out = json.loads(compact_tool_result(big, 10_000))
    assert len(out["observations"]) == 8 and "source_url" not in out["observations"][0] and out["observations"][-1]["ref"] == "r19"
    assert compact_tool_result({"a": "x" * 5000}, 100).endswith("...[truncated]")


# ------------------------------------------------------------------ cache
def test_cache_hit_miss_counters_and_stats(db, tmp_path):
    con, _ = db
    a = mk(Scripted([json.dumps(FINAL_OK)]), tmp_path)
    a.run(INPUT, ReferenceStore(con))
    assert (a.usage["cache_hits"], a.usage["cache_misses"]) == (0, 1) and a.case_stats["cache"] == "miss"
    b = mk(Scripted([]), tmp_path)
    b.run(INPUT, ReferenceStore(con))
    assert (b.usage["cache_hits"], b.case_stats["cache"], b.case_stats["model_calls"]) == (1, "hit", 0)


@pytest.mark.parametrize("change", ["model", "snapshot", "settings", "input", "steps"])
def test_cache_key_changes_with_every_relevant_input(db, tmp_path, change):
    con, _ = db
    base = mk(Scripted([json.dumps(FINAL_OK)]), tmp_path)
    base.run(INPUT, ReferenceStore(con))
    p2 = Scripted([json.dumps(FINAL_OK)])
    inp = INPUT
    kw = {}
    if change == "model":
        p2.model_id = "other-model"
    elif change == "settings":
        kw["llm"] = LLMSettings(temperature=0.7)
    elif change == "input":
        inp = {**INPUT, "records": [{"record_id": "rec-1", "ticker": "TSTB"}]}
    elif change == "steps":
        kw["max_steps"] = 5
    other = LLMAgent(p2, tmp_path, "snap2" if change == "snapshot" else "snap", sleep=lambda s: None, **kw)
    other.run(inp, ReferenceStore(con))
    assert p2.calls == 1                                            # no stale reuse


def test_only_valid_completed_responses_are_cached(db, tmp_path):
    with pytest.raises(MalformedOutput):
        mk(Scripted(["bad", "bad"]), tmp_path).run(INPUT, ReferenceStore(db[0]))
    assert not list(tmp_path.glob("*.json"))
    p = tmp_path / "x.json"
    a = mk(Scripted([json.dumps(FINAL_OK)]), tmp_path)
    a.run(INPUT, ReferenceStore(db[0]))
    f = next(tmp_path.glob("*.json"))
    f.write_text("{corrupt", encoding="utf-8")                      # corrupt entry = miss, then repaired
    b = mk(Scripted([json.dumps(FINAL_OK)]), tmp_path)
    b.run(INPUT, ReferenceStore(db[0]))
    assert b.case_stats["cache"] == "miss" and json.loads(f.read_text())["response"]["status"] == "insufficient_evidence"


# ------------------------------------------------------------------ HTTP providers (fake session, no network)
class Sess:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it


class R:
    def __init__(self, code=200, body=None, text=""):
        self.status_code, self._b, self.text = code, body, text

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("FINGUARD_LLM_MODEL", "qwen2.5:3b")


def test_openai_provider_classifies_http_failures(env):
    llm = LLMSettings()
    cases = [(requests.exceptions.ReadTimeout(), ProviderTimeout), (requests.exceptions.ConnectionError(), ProviderUnavailable),
             (requests.exceptions.ConnectTimeout(), ProviderUnavailable), (R(503), ProviderUnavailable), (R(429), ProviderUnavailable),
             (R(404, text="model not found"), ProviderError), (R(200, None), ProviderError), (R(200, {"choices": []}), ProviderError)]
    for item, exc in cases:
        p = OpenAICompatibleProvider(llm=llm, session=Sess(item))
        with pytest.raises(exc):
            p.complete("s", [{"role": "user", "content": "u"}])


def test_openai_provider_request_shape_usage_and_no_secret_in_errors(env):
    body = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
    s = Sess(R(200, body))
    p = OpenAICompatibleProvider(llm=LLMSettings(max_output_tokens=321, temperature=0, seed=7), session=s)
    assert p.complete("sys", [{"role": "user", "content": "hi"}]) == "{}"
    payload = s.calls[0][2]["json"]
    assert payload["max_tokens"] == 321 and payload["seed"] == 7 and payload["response_format"] == {"type": "json_object"}
    assert p.last_usage == {"input_tokens": 12, "output_tokens": 3}
    with pytest.raises(ProviderError) as e:
        OpenAICompatibleProvider(llm=LLMSettings(), session=Sess(R(401, text="bad key"))).complete("s", [])
    assert "ollama" not in str(e.value)                              # the API key value is never echoed


def test_ollama_native_provider_sets_context_and_prediction_limits(env):
    s = Sess(R(200, {"message": {"content": "{}"}, "prompt_eval_count": 40, "eval_count": 5}))
    p = OllamaProvider(llm=LLMSettings(num_ctx=2048, max_output_tokens=256), session=s)
    p.complete("sys", [{"role": "user", "content": "hi"}])
    m, url, kw = s.calls[0]
    assert url == "http://localhost:11434/api/chat" and kw["json"]["options"]["num_ctx"] == 2048 and kw["json"]["options"]["num_predict"] == 256
    assert kw["json"]["format"] == "json" and p.last_usage == {"input_tokens": 40, "output_tokens": 5}


def test_health_check_reports_missing_model_and_unreachable_server(env):
    ok = OpenAICompatibleProvider(llm=LLMSettings(), session=Sess(R(200, {"data": [{"id": "qwen2.5:3b"}]}),
                                                                  R(200, {"choices": [{"message": {"content": "OK"}}]})))
    assert ok.health_check()[0] is True
    missing = OpenAICompatibleProvider(llm=LLMSettings(), session=Sess(R(200, {"data": [{"id": "llama3"}]})))
    good, msg = missing.health_check()
    assert not good and "qwen2.5:3b" in msg and "llama3" in msg
    down = OpenAICompatibleProvider(llm=LLMSettings(), session=Sess(requests.exceptions.ConnectionError()))
    assert not down.health_check()[0]
    nat = OllamaProvider(llm=LLMSettings(), session=Sess(R(200, {"models": [{"name": "other"}]})))
    assert "ollama pull qwen2.5:3b" in nat.health_check()[1]


def test_settings_env_overrides_and_validation(monkeypatch):
    s = LLMSettings.load({"max_output_tokens": 500}, env={"FINGUARD_NUM_CTX": "2048", "FINGUARD_JSON_MODE": "0", "FINGUARD_READ_TIMEOUT": "42"})
    assert (s.max_output_tokens, s.num_ctx, s.json_mode, s.read_timeout) == (500, 2048, False, 42.0)
    assert LLMSettings.load(env={"FINGUARD_NUM_CTX": "none"}).num_ctx is None
    with pytest.raises(ValueError):
        LLMSettings.load(env={"FINGUARD_MAX_OUTPUT_TOKENS": "0"})
    assert LLMSettings(temperature=0.0).fingerprint() != LLMSettings(temperature=0.5).fingerprint()
    assert LLMSettings(read_timeout=5).fingerprint() == LLMSettings(read_timeout=500).fingerprint()   # timeouts do not change output


# ------------------------------------------------------------------ evaluation runner: limit, progress, interruption, gate, reports
@pytest.fixture(scope="module")
def evaldb():
    con, settings = build_db()
    run_validation(con, settings)
    cases, _, _ = generate_cases(con, settings, seed=42, n_per_category=6)
    persist_cases(con, cases)
    return con, settings


def test_limit_is_applied_after_mode_filter_deterministically_and_recorded(evaldb, tmp_path):
    con, settings = evaldb
    full = run_evaluation(con, settings, MockAgent("reference"), mode="full", reports_dir=tmp_path)
    smoke = run_evaluation(con, settings, MockAgent("reference"), mode="smoke", reports_dir=tmp_path)
    lim = run_evaluation(con, settings, MockAgent("reference"), mode="full", limit=5, reports_dir=tmp_path)
    lim_smoke = run_evaluation(con, settings, MockAgent("reference"), mode="smoke", limit=3, reports_dir=tmp_path)
    again = run_evaluation(con, settings, MockAgent("reference"), mode="full", limit=5, reports_dir=tmp_path)
    ids = lambda o: [r["case_id"] for r in o["results"]]
    assert ids(lim) == ids(full)[:5] == ids(again) and lim["metrics"]["total"] == 5
    assert ids(lim_smoke) == ids(smoke)[:3]                          # limit sits on top of the mode filter
    ex = lim["execution"]
    assert (ex["limit"], ex["cases_selected"], ex["cases_attempted"]) == (5, 36, 5) and lim["run"]["n_cases"] == 5
    for bad in (0, -2, True):
        with pytest.raises(ValueError):
            run_evaluation(con, settings, MockAgent("reference"), limit=bad, reports_dir=tmp_path)


def test_progress_events_are_complete_and_leak_nothing(evaldb, tmp_path):
    con, settings = evaldb
    events = []
    run_evaluation(con, settings, MockAgent("reference"), mode="full", limit=3, reports_dir=tmp_path, progress=events.append)
    starts = [e for e in events if e["event"] == "case_start"]
    ends = [e for e in events if e["event"] == "case_end"]
    assert [(e["i"], e["n"]) for e in starts] == [(1, 3), (2, 3), (3, 3)] and len(ends) == 3
    for e in ends:
        assert {"case_id", "category", "seconds", "status", "passed", "model_calls", "cache"} <= set(e)
    blob = json.dumps(events)
    assert "expected" not in blob and "api_key" not in blob.lower()


def test_ctrl_c_saves_completed_cases_and_marks_run_interrupted(evaldb, tmp_path):
    con, settings = evaldb

    class StopAfter(MockAgent):
        n = 0

        def run(self, agent_input, store):
            StopAfter.n += 1
            if StopAfter.n == 3:
                raise KeyboardInterrupt
            return super().run(agent_input, store)

    out = run_evaluation(con, settings, StopAfter("reference"), mode="full", reports_dir=tmp_path)
    assert out["metrics"]["total"] == 2 and out["execution"]["interrupted"] is True and out["run"]["status"] == "interrupted"
    row = con.execute("SELECT status, n_cases FROM agent_runs WHERE run_id = ?", [out["run"]["run_id"]]).fetchone()
    assert row == ("interrupted", 2)

    class StopFirst(MockAgent):
        def run(self, agent_input, store):
            raise KeyboardInterrupt

    with pytest.raises(RuntimeError, match="interrupted before any case"):
        run_evaluation(con, settings, StopFirst("reference"), mode="full", reports_dir=tmp_path)


def test_agent_failures_are_classified_and_never_become_answers(evaldb, tmp_path):
    con, settings = evaldb
    seq = iter([ProviderTimeout("t"), MalformedOutput("m"), InvalidAgentResponse("i"), ProviderUnavailable("u")])

    class Broken(MockAgent):
        usage = {}

        def run(self, agent_input, store):
            raise next(seq)

    out = run_evaluation(con, settings, Broken("reference"), mode="full", limit=4, reports_dir=tmp_path, agent_label="llm")
    assert out["execution"]["agent_error_kinds"] == {"provider_timeout": 1, "malformed_json": 1, "invalid_agent_response": 1,
                                                     "provider_unavailable": 1}
    assert all(r["response"] is None and not r["score"]["passed"] for r in out["results"]) and out["metrics"]["passed"] == 0


def test_blocked_gate_stops_real_llm_runs_too(tmp_path):
    con, settings = build_db()                                       # never validated -> blocked
    cases, _, _ = generate_cases(con, settings, seed=1, n_per_category=2)
    persist_cases(con, cases)
    p = Scripted([])
    with pytest.raises(GateBlocked):
        run_evaluation(con, settings, mk(p, tmp_path / "cache"), reports_dir=tmp_path)
    assert p.calls == 0                                              # the model was never called


def test_evidence_must_cite_refs_the_tools_returned():
    exp = {"is_error": False, "expected_statuses": ["verified"], "expected_issue_types": [], "should_abstain": False,
           "expected_value": 10.0, "exact_value": False, "expected_company_identifier": None}
    resp = {"case_id": "c", "status": "verified", "answer": "a", "normalized_value": 10.0, "unit": "USD", "company_identifier": None,
            "fiscal_period": None, "confidence": 0.9, "explanation": "e" * 40, "detected_issue_types": [], "recommended_action": "none",
            "evidence": [{"source": "reference_store", "ref": "made-up-ref", "value": 10.0, "detail": ""}]}
    bad = score_case(exp, resp, {"real-ref": 10.0}, {"rec-1"}, 0.005)
    assert bad["passed"] is False and bad["invalid_evidence_refs"] == ["made-up-ref"]
    resp["evidence"][0]["ref"] = "real-ref"
    assert score_case(exp, resp, {"real-ref": 10.0}, {"rec-1"}, 0.005)["passed"] is True
    resp["evidence"][0]["ref"] = "rec-1"
    s2 = score_case(exp, resp, {}, {"rec-1"}, 0.005)
    assert s2["invalid_evidence_refs"] == [] and s2["passed"] is False         # valid ref, but not backed by any tool result


def test_cli_rejects_bad_limit_and_reports_unhealthy_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("src.evaluation.run.load_env", lambda: None)
    monkeypatch.setenv("FINGUARD_REPORTS_DIR", str(tmp_path))
    path = tmp_path / "e.duckdb"
    c, s = build_db(path=str(path))
    run_validation(c, s)
    c.close()
    with pytest.raises(SystemExit):
        eval_main(["--db", str(path), "--limit", "0"])
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")       # nothing listens on port 9
    monkeypatch.setenv("FINGUARD_LLM_MODEL", "qwen2.5:3b")
    monkeypatch.setenv("FINGUARD_CONNECT_TIMEOUT", "2")
    assert eval_main(["--db", str(path), "--agent", "llm", "--provider", "openai", "--mode", "smoke", "--limit", "1"]) == 6
    assert "health check: FAILED" in capsys.readouterr().out
    rep = json.loads((tmp_path / "llm_evaluation.json").read_text())
    assert rep["status"] == "NOT_RUN" and "health check failed" in rep["reason"]


def test_report_persistence_records_limit_and_execution(evaldb, tmp_path):
    con, settings = evaldb
    out = run_evaluation(con, settings, MockAgent("reference"), mode="full", limit=2, reports_dir=tmp_path)
    saved = json.loads((tmp_path / "runs" / f"{out['run']['run_id']}.json").read_text())
    assert saved["execution"]["limit"] == 2 and saved["execution"]["cases_attempted"] == 2 and len(saved["results"]) == 2
    from src.reporting.generate import build_scorecard
    sc = build_scorecard(con, settings, out["run"]["run_id"])
    assert sc["execution"]["cases_selected"] == 36 and sc["case_counts"]["executed"] == 2
    assert sc["case_counts"]["skipped_not_selected_in_this_mode"] == 34


def test_material_claims_need_store_backed_evidence_not_just_the_input_record():
    exp = {"is_error": False, "expected_statuses": ["verified"], "expected_issue_types": [], "should_abstain": False,
           "expected_value": 10.0, "exact_value": False, "expected_company_identifier": None}
    resp = {"case_id": "c", "status": "verified", "answer": "a", "normalized_value": 10.0, "unit": "USD", "company_identifier": None,
            "fiscal_period": None, "confidence": 0.9, "explanation": "e" * 40, "detected_issue_types": [], "recommended_action": "none",
            "evidence": [{"source": "input_record", "ref": "rec-1", "value": 10.0, "detail": "the record itself"}]}
    s = score_case(exp, resp, {}, {"rec-1"}, 0.005)             # no tool ever returned anything
    assert s["passed"] is False and s["evidence_ok"] is False
    resp["evidence"].append({"source": "reference_store", "ref": "obs-9", "value": 10.0, "detail": "tool result"})
    assert score_case(exp, resp, {"obs-9": 10.0}, {"rec-1"}, 0.005)["passed"] is True


def test_broken_stdout_does_not_abort_an_evaluation(evaldb, tmp_path, monkeypatch):
    import src.evaluation.run as runmod
    con, settings = evaldb

    def boom(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr("builtins.print", boom)
    monkeypatch.setattr(runmod, "_PROGRESS_OK", True)
    out = run_evaluation(con, settings, MockAgent("reference"), mode="full", limit=3, reports_dir=tmp_path, progress=runmod._print_progress)
    assert out["metrics"]["total"] == 3 and runmod._PROGRESS_OK is False
