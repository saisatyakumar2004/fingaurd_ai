"""Evaluation runner.

    python -m src.evaluation.run --mode smoke|full --agent mock|mock-naive|mock-degraded|llm

Refuses to run when the deterministic data-quality gate is blocked. The agent only receives
`agent_input`; expected answers, corruption labels and store overrides stay in this process.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from src.agent.mock_agent import MockAgent
from src.agent.tools import ReferenceStore
from src.common.config import ROOT, load_env, load_settings, path_for
from src.common.db import fetch_dicts, insert_rows, open_db
from src.common.util import canonical_json, git_commit, stable_id, utcnow
from src.evaluation.metrics import aggregate
from src.evaluation.scoring import root_cause, score_case
from src.quality.validate import gate_status


class GateBlocked(RuntimeError):
    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__("; ".join(status["reasons"]))
        self.status = status


def dataset_snapshot_id(con) -> str:
    ids = [r[0] for r in con.execute("SELECT DISTINCT snapshot_id FROM financial_observations ORDER BY 1").fetchall()]
    return stable_id(*ids, length=16)


def llm_settings_from(settings: dict[str, Any]):
    from src.agent.llm_agent import LLMSettings
    cfg = dict(settings.get("llm") or {})
    for k in ("connect_timeout", "read_timeout"):          # yaml uses *_seconds
        if f"{k}_seconds" in cfg:
            cfg[k] = cfg.pop(f"{k}_seconds")
    return LLMSettings.load(cfg)


def build_agent(name: str, settings: dict[str, Any], dataset_snapshot: str, provider: str = "anthropic"):
    if name == "mock":
        return MockAgent("reference")
    if name.startswith("mock-"):
        return MockAgent(name.split("-", 1)[1])
    if name == "llm":
        from src.agent.llm_agent import LLMAgent, build_provider
        llm = llm_settings_from(settings)
        prov = build_provider(provider, llm)
        return LLMAgent(prov, path_for(settings, "cache") / "llm", dataset_snapshot, int(settings["agent"]["max_tool_steps"]), llm=llm)
    raise ValueError(f"unknown agent {name!r}")


def _failed_score() -> dict[str, Any]:
    return {"status_ok": False, "issue_ok": False, "numeric_ok": None, "identity_ok": None, "evidence_ok": False,
            "invented_value": False, "passed": False, "predicted_issue": False, "abstained": False,
            "abstain_expected": False, "invalid_evidence_refs": [],
            "rubric": {"evidence_relevance": 0.0, "factual_consistency": 0.0, "reasoning_completeness": 0.0}}


def run_evaluation(con, settings: dict[str, Any], agent, *, mode: str = "smoke", reports_dir: Path | None = None,
                   agent_label: str = "mock", suite: str = "base", limit: int | None = None,
                   progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    gate = gate_status(con)
    if gate["blocked"]:
        raise GateBlocked(gate)
    clauses = {"base": "suite_version NOT LIKE 'multi-%'", "multi": "suite_version LIKE 'multi-%'", "all": "TRUE"}
    if suite not in clauses:
        raise ValueError(f"unknown suite {suite!r} (base | multi | all)")
    where = "WHERE " + clauses[suite] + (" AND smoke" if mode == "smoke" else "")
    cases = fetch_dicts(con, f"SELECT * FROM test_cases {where} ORDER BY case_id")
    if mode == "smoke30":
        per: dict[str, int] = {}
        picked = []
        for c in cases:
            if per.get(c["corruption_type"], 0) < 5:
                per[c["corruption_type"]] = per.get(c["corruption_type"], 0) + 1
                picked.append(c)
        cases = picked
    if not cases:
        raise RuntimeError("no test cases found: run `python -m src.corruption.generate` first")
    cases_selected = len(cases)
    if limit is not None:                                   # applied AFTER suite + mode filters, in deterministic case-id order
        cases = cases[:limit]

    ev_cfg = settings["evaluation"]
    tol = float(ev_cfg["relative_tolerance"])
    results: list[dict[str, Any]] = []
    t0 = time.monotonic()
    interrupted = False
    emit = progress or (lambda ev: None)
    if hasattr(agent, "on_event"):
        agent.on_event = lambda ev: emit({**ev, "event": "agent"})
    for idx, c in enumerate(cases, 1):
        case_t0 = time.monotonic()
        emit({"event": "case_start", "i": idx, "n": len(cases), "case_id": c["case_id"], "category": c["corruption_type"]})
        agent_input = json.loads(c["agent_input"])
        expected = json.loads(c["expected"])
        overrides = json.loads(c["store_overrides"])
        store = ReferenceStore(con, overrides.get("exclude_keys"))
        resp_dict, err, err_kind = None, None, None
        try:
            resp = agent.run(agent_input, store)
            resp_dict = resp.model_dump()
            score = score_case(expected, resp_dict, store.issued, {r["record_id"] for r in agent_input["records"]}, tol)
        except KeyboardInterrupt:
            interrupted = True                               # clean cancellation: keep everything completed so far
            emit({"event": "interrupted", "i": idx, "n": len(cases), "case_id": c["case_id"]})
            break
        except Exception as e:  # an agent failure is a failed case, never a skipped one and never an invented answer
            err = f"{type(e).__name__}: {e}"
            err_kind = getattr(e, "kind", type(e).__name__)
            score = _failed_score()
            traceback.print_exc(file=sys.stderr) if os.environ.get("FINGUARD_DEBUG") else None
        stats = dict(getattr(agent, "case_stats", {}) or {})
        emit({"event": "case_end", "i": idx, "n": len(cases), "case_id": c["case_id"], "category": c["corruption_type"],
              "seconds": round(time.monotonic() - case_t0, 2), "status": (resp_dict or {}).get("status"),
              "passed": score["passed"], "error": err_kind, "model_calls": stats.get("model_calls", 0),
              "cache": stats.get("cache"), "tool_calls": len(store.trace)})
        results.append({
            "case_id": c["case_id"], "category": c["corruption_type"], "subtype": c["subtype"], "severity": c["severity"],
            "smoke": c["smoke"], "expected": expected, "response": resp_dict, "score": score,
            "root_cause": root_cause(c["corruption_type"], c["subtype"], score, resp_dict, err),
            "agent_error": err, "agent_error_kind": err_kind, "tool_calls": len(store.trace),
            "model_calls": stats.get("model_calls"), "case_seconds": round(time.monotonic() - case_t0, 3), "cache": stats.get("cache"),
        })
    if not results:
        raise RuntimeError("evaluation interrupted before any case completed; nothing was recorded")

    metrics = aggregate(results, float(ev_cfg["high_confidence_threshold"]), int(ev_cfg["calibration_bins"]),
                        ev_cfg["severity_weights"])
    usage = dict(getattr(agent, "usage", {}) or {})
    kinds: dict[str, int] = {}
    for r in results:
        if r["agent_error"]:
            kinds[r["agent_error_kind"]] = kinds.get(r["agent_error_kind"], 0) + 1
    execution = {"duration_seconds": round(time.monotonic() - t0, 3),
                 "limit": limit, "cases_selected": cases_selected, "cases_attempted": len(results),
                 "cases_completed_without_agent_error": sum(1 for r in results if not r["agent_error"]),
                 "interrupted": interrupted,
                 "agent_errors": sum(1 for r in results if r["agent_error"]), "agent_error_kinds": kinds,
                 "provider_unavailable": sum(1 for r in results if r["agent_error"] and "ProviderUnavailable" in r["agent_error"]),
                 "model_calls": sum(r["model_calls"] or 0 for r in results),
                 "cache_hits": sum(1 for r in results if r["cache"] == "hit"),
                 "cache_misses": sum(1 for r in results if r["cache"] == "miss"),
                 "token_usage": usage or None,
                 "estimated_cost_usd": agent.estimated_cost_usd() if hasattr(agent, "estimated_cost_usd") else None}
    now = utcnow()
    run_id = f"run-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    suite_version = "+".join(sorted({c["suite_version"] for c in cases}))
    run_row = {
        "run_id": run_id, "run_timestamp": now, "git_commit": git_commit(ROOT), "dataset_snapshot": dataset_snapshot_id(con),
        "model_id": getattr(agent, "model_id", agent_label), "agent_profile": agent_label,
        "prompt_version": getattr(agent, "prompt_version", settings["agent"]["prompt_version"]),
        "evaluation_config": canonical_json({"relative_tolerance": tol, **{k: ev_cfg[k] for k in ("high_confidence_threshold", "calibration_bins")}}),
        "test_suite_version": suite_version, "mode": mode, "n_cases": len(results),
        "status": "interrupted" if interrupted else "completed",
        "execution_json": json.dumps(execution, default=str),
    }
    insert_rows(con, "agent_runs", [run_row])
    insert_rows(con, "evaluation_results", [{
        "run_id": run_id, "case_id": r["case_id"], "passed": r["score"]["passed"],
        "status": (r["response"] or {}).get("status"), "confidence": (r["response"] or {}).get("confidence"),
        "root_cause": r["root_cause"], "severity": r["severity"], "result_json": json.dumps(r, default=str)} for r in results])

    out = {"run": {**run_row, "run_timestamp": now.isoformat(), "validation_run_id": gate["validation_run_id"]},
           "execution": execution,
           "metrics": metrics, "results": results}
    reports_dir = reports_dir or path_for(settings, "reports")
    (reports_dir / "runs").mkdir(parents=True, exist_ok=True)
    (reports_dir / "runs" / f"{run_id}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (reports_dir / "latest_run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    return out


_PROGRESS_OK = True


def _print_progress(ev: dict[str, Any]) -> None:
    """Progress must never kill an evaluation: if stdout is closed/broken (e.g. `| head`), progress output is switched off."""
    global _PROGRESS_OK
    if not _PROGRESS_OK:
        return
    try:
        _print_progress_line(ev)
    except (OSError, ValueError):
        _PROGRESS_OK = False


def _print_progress_line(ev: dict[str, Any]) -> None:
    """One flushed line per event. Never prints prompts, responses beyond the status, or credentials."""
    k = ev["event"]
    if k == "case_start":
        print(f"[{ev['i']}/{ev['n']}] {ev['case_id']} {ev['category']} ...", flush=True)
    elif k == "agent" and ev.get("type") == "model_call":
        print(f"      model call {ev['n']}: {ev['seconds']}s, {ev['output_tokens']} output tokens", flush=True)
    elif k == "agent" and ev.get("type") == "retry":
        print(f"      retrying after {ev['reason']} (attempt {ev['attempt']})", flush=True)
    elif k == "case_end":
        verdict = "PASS" if ev["passed"] else "FAIL"
        extra = f" error={ev['error']}" if ev["error"] else f" status={ev['status']}"
        print(f"[{ev['i']}/{ev['n']}] {verdict}{extra} elapsed={ev['seconds']}s model_calls={ev['model_calls']} "
              f"tool_calls={ev['tool_calls']} cache={ev['cache']}", flush=True)
    elif k == "interrupted":
        print(f"[{ev['i']}/{ev['n']}] interrupted (Ctrl+C) - saving the {ev['i'] - 1} completed case(s)", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate an agent against generated test cases")
    ap.add_argument("--db", default=None)
    ap.add_argument("--mode", choices=["smoke", "smoke30", "full"], default="smoke")
    ap.add_argument("--agent", default="mock", help="mock | mock-naive | mock-degraded | llm")
    ap.add_argument("--suite", choices=["base", "multi", "all"], default="base")
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "openai", "ollama"])
    ap.add_argument("--limit", type=int, default=None, help="evaluate only the first N cases (after suite/mode filters, in case-id order)")
    ap.add_argument("--no-health-check", action="store_true", help="skip the provider/model check before an llm run")
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be a positive integer")
    load_env()
    settings = load_settings()
    con = open_db(args.db or path_for(settings, "db"))
    from src.evaluation.llm_report import build_llm_report, write_llm_report
    try:
        try:
            agent = build_agent(args.agent, settings, dataset_snapshot_id(con), args.provider)
        except RuntimeError as e:
            if args.agent != "llm":
                raise
            write_llm_report(path_for(settings, "reports"), build_llm_report(status="NOT_RUN", reason=str(e), requested_mode=args.mode))
            print(f"REAL-LLM EVALUATION: NOT RUN ({e})", file=sys.stderr)
            return 5
        if args.agent == "llm" and not args.no_health_check:
            ok, msg = agent.provider.health_check() if hasattr(agent.provider, "health_check") else (True, "no health check available")
            print(f"provider health check: {'OK' if ok else 'FAILED'} - {msg}", flush=True)
            if not ok:
                write_llm_report(path_for(settings, "reports"), build_llm_report(status="NOT_RUN", reason=f"provider health check failed: {msg}",
                                                                                 requested_mode=args.mode))
                return 6
        out = run_evaluation(con, settings, agent, mode=args.mode, agent_label=args.agent, suite=args.suite, limit=args.limit,
                             progress=_print_progress)
    except GateBlocked as e:
        print("EVALUATION BLOCKED by data-quality gate:", file=sys.stderr)
        for r in e.status["reasons"]:
            print("  -", r, file=sys.stderr)
        return 3
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.agent == "llm":
        ex = out["execution"]
        status = "INTERRUPTED" if ex["interrupted"] else ("COMPLETED" if not ex["agent_errors"] else "PARTIAL")
        write_llm_report(path_for(settings, "reports"), build_llm_report(status=status, out=out, requested_mode=args.mode))
    m = out["metrics"]
    ex = out["execution"]
    print(f"run {out['run']['run_id']}  agent={out['run']['model_id']}  cases={m['total']}/{ex['cases_selected']} selected  "
          f"accuracy={m['accuracy']:.3f}  agent_errors={ex['agent_errors']}  duration={ex['duration_seconds']}s")
    d = m["detection"]
    print(f"detection precision={d['precision']} recall={d['recall']} f1={d['f1']}  FPR(clean)={m['false_positive_rate']}")
    for cat, v in m["by_category"].items():
        print(f"  {cat:14s} {v['passed']}/{v['n']}  acc={v['accuracy']:.3f}")
    print(f"high-confidence wrong: {m['high_confidence_wrong']['count']}   brier={m['calibration']['brier']}")
    return 130 if ex["interrupted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
