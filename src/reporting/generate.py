"""Build reports/scorecard.json and reports/scorecard.html from a stored evaluation run.

All numbers come from stored per-case results (evaluation_results); metrics are recomputed from
those results, never typed in. Hidden case evidence is attached for traceability.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from src.common.config import load_env, load_settings, path_for
from src.common.db import fetch_dicts, open_db
from src.evaluation.metrics import aggregate

DISCLAIMERS = [
    "Confidence values are self-reported by the agent and are not statistically validated probabilities.",
    "Metrics describe this agent on this synthetic corruption suite only; they do not estimate real-world error rates.",
    "If the agent is a mock profile, results validate the evaluation harness, not any language model.",
]


def load_run(con, run_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if run_id is None:
        rows = fetch_dicts(con, "SELECT run_id FROM agent_runs ORDER BY run_timestamp DESC, run_id DESC LIMIT 1")
        if not rows:
            raise RuntimeError("no evaluation runs found: run `python -m src.evaluation.run` first")
        run_id = rows[0]["run_id"]
    run = fetch_dicts(con, "SELECT * FROM agent_runs WHERE run_id = ?", [run_id])
    if not run:
        raise RuntimeError(f"unknown run_id {run_id}")
    res = [json.loads(r["result_json"]) for r in
           fetch_dicts(con, "SELECT result_json FROM evaluation_results WHERE run_id = ? ORDER BY case_id", [run_id])]
    return run[0], res


def build_scorecard(con, settings: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    run, results = load_run(con, run_id)
    ev = settings["evaluation"]
    metrics = aggregate(results, float(ev["high_confidence_threshold"]), int(ev["calibration_bins"]), ev["severity_weights"])
    hidden = {r["case_id"]: r for r in fetch_dicts(
        con, "SELECT case_id, corruption_parameters, original_value, corrupted_value, source_evidence, expected_behavior "
             "FROM test_cases")}
    cases = []
    for r in results:
        h = hidden.get(r["case_id"], {})
        resp = r.get("response") or {}
        cases.append({
            "case_id": r["case_id"], "category": r["category"], "subtype": r.get("subtype"), "severity": r["severity"],
            "passed": r["score"]["passed"], "root_cause": r.get("root_cause"),
            "expected": {k: r["expected"][k] for k in ("expected_statuses", "expected_issue_types", "expected_value",
                                                       "expected_company_identifier", "should_abstain")},
            "expected_behavior": h.get("expected_behavior"),
            "agent": {"status": resp.get("status"), "confidence": resp.get("confidence"),
                      "normalized_value": resp.get("normalized_value"), "company_identifier": resp.get("company_identifier"),
                      "detected_issue_types": resp.get("detected_issue_types"), "explanation": resp.get("explanation"),
                      "evidence": resp.get("evidence"), "recommended_action": resp.get("recommended_action")},
            "agent_error": r.get("agent_error"), "score_detail": r["score"],
            "corruption_parameters": json.loads(h["corruption_parameters"]) if h.get("corruption_parameters") else None,
            "original_value": json.loads(h["original_value"]) if h.get("original_value") else None,
            "presented_records": json.loads(h["corrupted_value"]) if h.get("corrupted_value") else None,
            "source_evidence": json.loads(h["source_evidence"]) if h.get("source_evidence") else None,
        })
    suite_total = con.execute("SELECT count(*) FROM test_cases WHERE suite_version IN (SELECT unnest(string_split(?, '+')))",
                              [run["test_suite_version"]]).fetchone()[0]
    val = fetch_dicts(con, "SELECT summary FROM validation_runs ORDER BY validation_run_id DESC LIMIT 1")
    validation = json.loads(val[0]["summary"]) if val else None
    counts = {"executed": metrics["total"], "passed": metrics["passed"], "failed": metrics["total"] - metrics["passed"],
              "agent_errors": sum(1 for r in results if r.get("agent_error")),
              "skipped_not_selected_in_this_mode": max(0, suite_total - metrics["total"])}
    return {
        "case_counts": counts,
        "data_validation": validation,
        "run": {k: (str(v) if k == "run_timestamp" else v) for k, v in run.items()},
        "data_snapshot": {"dataset_snapshot": run["dataset_snapshot"], "test_suite_version": run["test_suite_version"]},
        "model": {"model_id": run["model_id"], "agent_profile": run["agent_profile"], "prompt_version": run["prompt_version"]},
        "execution": json.loads(run["execution_json"]) if run.get("execution_json") else None,
        "metrics": metrics, "disclaimers": DISCLAIMERS, "cases": cases,
    }


# ------------------------------------------------------------------ HTML
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None, d: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


def _svg_reliability(bins: list[dict[str, Any]]) -> str:
    size, pad = 260, 30
    inner = size - 2 * pad
    pts = []
    for b in bins:
        if b["n"]:
            x, y = pad + b["mean_confidence"] * inner, size - pad - b["accuracy"] * inner
            pts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{3 + min(b["n"], 40) ** 0.5:.1f}" fill="#2563eb" opacity="0.75"><title>n={b["n"]} conf={b["mean_confidence"]:.2f} acc={b["accuracy"]:.2f}</title></circle>')
    return (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="Reliability diagram">'
            f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" fill="none" stroke="#999"/>'
            f'<line x1="{pad}" y1="{size - pad}" x2="{size - pad}" y2="{pad}" stroke="#bbb" stroke-dasharray="4"/>'
            + "".join(pts) +
            f'<text x="{size / 2}" y="{size - 6}" font-size="10" text-anchor="middle">mean confidence</text>'
            f'<text x="10" y="{size / 2}" font-size="10" transform="rotate(-90 10 {size / 2})" text-anchor="middle">accuracy</text></svg>')


def render_html(sc: dict[str, Any]) -> str:
    m, run, e = sc["metrics"], sc["run"], html.escape
    det, ab = m["detection"], m["abstention"]
    cat_rows = "".join(f"<tr><td>{e(c)}</td><td>{v['passed']}/{v['n']}</td><td>{_pct(v['accuracy'])}</td>"
                       f"<td>{_num(v.get('detection_precision'))}</td><td>{_num(v.get('detection_recall'))}</td><td>{_num(v.get('detection_f1'))}</td></tr>"
                       for c, v in m["by_category"].items())
    ex = sc.get("execution") or {}
    exec_rows = "".join(f"<tr><td>{e(k)}</td><td>{e(str(v) if v is not None else 'not available')}</td></tr>" for k, v in ex.items())
    cause_rows = "".join(f"<tr><td>{e(c['cause'])}</td><td>{c['count']}</td><td>{c['weighted_score']}</td></tr>" for c in m["root_causes"]) \
        or "<tr><td colspan=3>no failures</td></tr>"
    cal_rows = "".join(f"<tr><td>{b['lo']:.1f}-{b['hi']:.1f}</td><td>{b['n']}</td><td>{_num(b['mean_confidence'], 2)}</td><td>{_num(b['accuracy'], 2)}</td></tr>"
                       for b in m["calibration"]["bins"] if b["n"])
    hc = "".join(f"<tr><td>{e(h['case_id'])}</td><td>{e(h['category'])}</td><td>{e(h['severity'])}</td><td>{h['confidence']:.2f}</td><td>{e(str(h['status']))}</td></tr>"
                 for h in m["high_confidence_wrong"]["cases"]) or "<tr><td colspan=5>none</td></tr>"
    ordered = sorted(sc["cases"], key=lambda c: (c["passed"], c["case_id"]))
    case_rows = "".join(
        f"<tr class='{'ok' if c['passed'] else 'bad'}'><td>{e(c['case_id'])}</td><td>{e(c['category'])}/{e(str(c['subtype']))}</td>"
        f"<td>{e(c['severity'])}</td><td>{'PASS' if c['passed'] else 'FAIL'}</td><td>{e(str(c['agent']['status']))}</td>"
        f"<td>{e(','.join(c['agent']['detected_issue_types'] or []))}</td><td>{e(str(c['root_cause'] or ''))}</td>"
        f"<td><details><summary>evidence</summary><pre>{e(json.dumps({'expected': c['expected'], 'agent_evidence': c['agent']['evidence'], 'explanation': c['agent']['explanation'], 'presented_records': c['presented_records'], 'corruption': c['corruption_parameters'], 'source_evidence': c['source_evidence']}, indent=1, default=str))}</pre></details></td></tr>"
        for c in ordered)
    ranked = "".join(f"<tr><td>{e(f['case_id'])}</td><td>{e(f['category'])}</td><td>{e(f['severity'])}</td><td>{e(f['root_cause'])}</td><td>{f['confidence']:.2f}</td><td>{'YES' if f['wrong_company_or_period_risk'] else ''}</td></tr>"
                     for f in m["ranked_failures"][:30]) or "<tr><td colspan=6>no failures</td></tr>"
    count_rows = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in sc["case_counts"].items())
    dv = sc.get("data_validation") or {}
    val_rows = ("".join(f"<tr><td>{e(k)}</td><td>{e(json.dumps(v) if isinstance(v, (dict, list)) else str(v))}</td></tr>"
                        for k, v in dv.items() if k in ("total_issues", "confirmed_errors", "needs_review", "by_classification", "not_reported_by_design", "validation_run_id"))
                or "<tr><td colspan=2>not available</td></tr>")
    rubric_rows = "".join("<tr><td>" + e(k) + "</td><td>" + format(v, ".3f") + "</td></tr>" for k, v in m["rubric"].items())
    caveats = "".join("<li>" + e(d) + "</li>" for d in sc["disclaimers"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinGuard AI Scorecard</title><style>
body{{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px auto;max-width:1100px;padding:0 16px;color:#1f2937}}
h1{{margin-bottom:0}} h2{{margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;margin:8px 0}} th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}}
th{{background:#f3f4f6}} .kpi{{display:flex;flex-wrap:wrap;gap:12px}} .kpi div{{border:1px solid #ddd;border-radius:6px;padding:8px 14px;min-width:130px}}
.kpi b{{display:block;font-size:20px}} tr.bad td:nth-child(4){{color:#b91c1c;font-weight:600}} tr.ok td:nth-child(4){{color:#047857}}
pre{{white-space:pre-wrap;font-size:11px;max-width:620px;overflow:auto}} .note{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>FinGuard AI scorecard</h1>
<p class="note">run {e(run['run_id'])} &middot; {e(str(run['run_timestamp']))} &middot; model {e(str(run['model_id']))} &middot; prompt {e(str(run['prompt_version']))} &middot; suite {e(str(run['test_suite_version']))} &middot; dataset snapshot {e(str(run['dataset_snapshot']))} &middot; git {e(str(run.get('git_commit')))}</p>
<div class="kpi"><div><b>{m['total']}</b>cases executed</div><div><b>{_pct(m['accuracy'])}</b>overall accuracy</div>
<div><b>{_num(det['precision'])}</b>detection precision</div><div><b>{_num(det['recall'])}</b>detection recall</div><div><b>{_num(det['f1'])}</b>F1</div>
<div><b>{_pct(m['false_positive_rate'])}</b>clean-control false positives</div><div><b>{m['high_confidence_wrong']['count']}</b>high-confidence wrong</div>
<div><b>{_num(m['calibration']['brier'])}</b>Brier score</div></div>
<h2>Case counts</h2><table>{count_rows}</table>
<h2>Reference-data validation (latest run)</h2><table>{val_rows}</table>
<h2>Accuracy by failure type</h2><table><tr><th>Category</th><th>Passed</th><th>Accuracy</th><th>Det. precision*</th><th>Det. recall</th><th>Det. F1*</th></tr>{cat_rows}</table><p class="note">*Per category: that category's error cases are positives and all clean controls are negatives. n/a means not applicable (e.g. clean control) - it is not a zero score.</p>
<h2>Execution</h2><table>{exec_rows}</table>
<h2>Abstention</h2><table>
<tr><td>Cases requiring abstention</td><td>{ab['cases_requiring_abstention']}</td></tr>
<tr><td>Abstention recall</td><td>{_pct(ab['abstention_recall'])}</td></tr>
<tr><td>Abstention precision</td><td>{_pct(ab['abstention_precision'])}</td></tr>
<tr><td>Correct answers when evidence adequate</td><td>{_pct(ab['answered_correctly_when_evidence_adequate'])}</td></tr>
<tr><td>Incorrect abstention rate (abstained although evidence was adequate)</td><td>{_pct(ab['incorrect_abstention_rate'])}</td></tr>
<tr><td>Evidence completeness</td><td>{_pct(m['evidence_completeness'])}</td></tr>
<tr><td>Invented values where abstention was required</td><td>{ab['invented_values_when_abstention_required']}</td></tr></table>
<h2>Confidence calibration</h2><p class="note">ECE {_num(m['calibration']['ece'])} &middot; {e(m['calibration']['note'])}</p>
<div style="display:flex;gap:24px;flex-wrap:wrap">{_svg_reliability(m['calibration']['bins'])}
<table style="width:auto"><tr><th>bin</th><th>n</th><th>mean conf</th><th>accuracy</th></tr>{cal_rows}</table></div>
<h2>High-confidence wrong answers (&ge; {m['high_confidence_wrong']['threshold']})</h2><table><tr><th>case</th><th>category</th><th>severity</th><th>confidence</th><th>status</th></tr>{hc}</table>
<h2>Top recurring root causes</h2><table><tr><th>cause</th><th>failures</th><th>severity-weighted score</th></tr>{cause_rows}</table>
<h2>Severity-ranked failures (wrong company / wrong period first)</h2><table><tr><th>case</th><th>category</th><th>severity</th><th>root cause</th><th>confidence</th><th>wrong company/period risk</th></tr>{ranked}</table>
<h2>Explanation rubric (mean, 0-1)</h2><table>{rubric_rows}</table>
<h2>Case-level evidence</h2><table><tr><th>case</th><th>type</th><th>sev</th><th>result</th><th>status</th><th>issues</th><th>root cause</th><th>trace</th></tr>{case_rows}</table>
<h2>Caveats</h2><ul>{caveats}</ul>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate scorecard.json / scorecard.html")
    ap.add_argument("--db", default=None)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)
    load_env()
    settings = load_settings()
    con = open_db(args.db or path_for(settings, "db"))
    sc = build_scorecard(con, settings, args.run_id)
    out = path_for(settings, "reports")
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(sc, indent=2, default=str), encoding="utf-8")
    (out / "scorecard.html").write_text(render_html(sc), encoding="utf-8")
    print(f"wrote {out / 'scorecard.json'} and {out / 'scorecard.html'} for {sc['run']['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
