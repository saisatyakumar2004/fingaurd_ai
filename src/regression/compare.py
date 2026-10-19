"""Regression gate.

    python -m src.regression.compare --set-baseline [--run-id RUN]   save a baseline
    python -m src.regression.compare [--run-id RUN]                  compare latest vs baseline (exit 1 on breach)

Breach rules deliberately look at categories individually: an overall-accuracy improvement can
never hide a regression in a critical category.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from src.common.config import load_env, load_settings, path_for
from src.common.db import fetch_dicts, upsert_row
from src.common.db import open_db
from src.common.util import utcnow
from src.evaluation.metrics import aggregate


def _compact_cases(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in results:
        resp = r.get("response") or {}
        out[r["case_id"]] = {"category": r["category"], "severity": r["severity"], "passed": r["score"]["passed"],
                             "status": resp.get("status"), "normalized_value": resp.get("normalized_value"),
                             "issues": sorted(resp.get("detected_issue_types") or []), "confidence": resp.get("confidence")}
    return out


def load_run_snapshot(con, settings: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    if run_id is None:
        rows = fetch_dicts(con, "SELECT run_id FROM agent_runs ORDER BY run_timestamp DESC, run_id DESC LIMIT 1")
        if not rows:
            raise RuntimeError("no evaluation runs found")
        run_id = rows[0]["run_id"]
    run = fetch_dicts(con, "SELECT * FROM agent_runs WHERE run_id = ?", [run_id])
    if not run:
        raise RuntimeError(f"unknown run {run_id}")
    results = [json.loads(r["result_json"]) for r in
               fetch_dicts(con, "SELECT result_json FROM evaluation_results WHERE run_id = ?", [run_id])]
    ev = settings["evaluation"]
    metrics = aggregate(results, float(ev["high_confidence_threshold"]), int(ev["calibration_bins"]), ev["severity_weights"])
    meta = {k: (str(v) if k == "run_timestamp" else v) for k, v in run[0].items()}
    return {"run_id": run_id, "meta": meta, "metrics": metrics, "cases": _compact_cases(results)}


def set_baseline(con, name: str, snap: dict[str, Any]) -> None:
    upsert_row(con, "regression_baselines", ["baseline_name"], {
        "baseline_name": name, "run_id": snap["run_id"], "created_at": utcnow(),
        "metrics_json": json.dumps(snap["metrics"], default=str), "case_results_json": json.dumps(snap["cases"], default=str),
        "run_meta_json": json.dumps(snap["meta"], default=str)})


def load_baseline(con, name: str) -> dict[str, Any] | None:
    rows = fetch_dicts(con, "SELECT * FROM regression_baselines WHERE baseline_name = ?", [name])
    if not rows:
        return None
    r = rows[0]
    return {"run_id": r["run_id"], "meta": json.loads(r["run_meta_json"]), "metrics": json.loads(r["metrics_json"]),
            "cases": json.loads(r["case_results_json"])}


def _delta(cur: float | None, base: float | None) -> float | None:
    return None if cur is None or base is None else cur - base


def diff_runs(base: dict[str, Any], cur: dict[str, Any], th: dict[str, Any]) -> dict[str, Any]:
    bm, cm = base["metrics"], cur["metrics"]
    common = sorted(set(base["cases"]) & set(cur["cases"]))
    newly_failing = [c for c in common if base["cases"][c]["passed"] and not cur["cases"][c]["passed"]]
    newly_passing = [c for c in common if not base["cases"][c]["passed"] and cur["cases"][c]["passed"]]
    changed = [{"case_id": c, "before": {k: base["cases"][c][k] for k in ("status", "normalized_value", "issues")},
                "after": {k: cur["cases"][c][k] for k in ("status", "normalized_value", "issues")}}
               for c in common if any(base["cases"][c][k] != cur["cases"][c][k] for k in ("status", "normalized_value", "issues"))]
    warnings: list[str] = []
    if base["meta"].get("test_suite_version") != cur["meta"].get("test_suite_version"):
        warnings.append("test suite versions differ; only common cases were compared")
    if base["meta"].get("dataset_snapshot") != cur["meta"].get("dataset_snapshot"):
        warnings.append("dataset snapshots differ; metric changes may reflect data, not the agent")
    if set(base["cases"]) != set(cur["cases"]):
        warnings.append(f"case sets differ (baseline {len(base['cases'])}, current {len(cur['cases'])}, common {len(common)})")

    deltas = {
        "accuracy": _delta(cm["accuracy"], bm["accuracy"]),
        "precision": _delta(cm["detection"]["precision"], bm["detection"]["precision"]),
        "recall": _delta(cm["detection"]["recall"], bm["detection"]["recall"]),
        "f1": _delta(cm["detection"]["f1"], bm["detection"]["f1"]),
        "false_positive_rate": _delta(cm["false_positive_rate"], bm["false_positive_rate"]),
        "brier": _delta(cm["calibration"]["brier"], bm["calibration"]["brier"]),
        "high_confidence_wrong": cm["high_confidence_wrong"]["count"] - bm["high_confidence_wrong"]["count"],
    }
    cat_deltas = {c: _delta(cm["by_category"].get(c, {}).get("accuracy"), bm["by_category"].get(c, {}).get("accuracy"))
                  for c in sorted(set(bm["by_category"]) | set(cm["by_category"]))}

    breaches: list[dict[str, Any]] = []
    if deltas["accuracy"] is not None and deltas["accuracy"] < -th["max_overall_accuracy_drop"]:
        breaches.append({"rule": "overall_accuracy_drop", "detail": f"accuracy changed by {deltas['accuracy']:+.4f}"})
    for cat, d in cat_deltas.items():
        if d is not None and d < -th["max_category_accuracy_drop"]:
            breaches.append({"rule": "category_accuracy_drop", "detail": f"{cat} accuracy changed by {d:+.4f}"})
    if deltas["recall"] is not None and deltas["recall"] < -th["max_recall_drop"]:
        breaches.append({"rule": "detection_recall_drop", "detail": f"recall changed by {deltas['recall']:+.4f}"})
    if deltas["false_positive_rate"] is not None and deltas["false_positive_rate"] > th["max_fpr_increase"]:
        breaches.append({"rule": "false_positive_rate_increase", "detail": f"FPR changed by {deltas['false_positive_rate']:+.4f}"})
    if deltas["high_confidence_wrong"] > th["max_high_conf_wrong_increase"]:
        breaches.append({"rule": "high_confidence_wrong_increase", "detail": f"{deltas['high_confidence_wrong']:+d} cases"})
    crit = set(th["critical_categories"])
    crit_new = [c for c in newly_failing if cur["cases"][c]["category"] in crit]
    if len(crit_new) > th["max_new_failures_critical"]:
        breaches.append({"rule": "critical_category_new_failures",
                         "detail": f"{len(crit_new)} newly failing cases in {sorted(crit)}: {crit_new[:10]}"})
    context = {k: {"baseline": base["meta"].get(k), "current": cur["meta"].get(k)}
               for k in ("model_id", "prompt_version", "git_commit", "mode") if base["meta"].get(k) != cur["meta"].get(k)}
    return {"baseline_run": base["run_id"], "current_run": cur["run_id"], "context_changes": context, "passed": not breaches, "breaches": breaches,
            "warnings": warnings, "newly_failing": newly_failing, "newly_passing": newly_passing,
            "changed_answers": changed, "metric_deltas": deltas, "category_accuracy_deltas": cat_deltas,
            "thresholds": th, "created_at": utcnow().isoformat()}


def incompatibilities(base: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    """A baseline is only valid for the exact test suite AND dataset snapshot it was created from."""
    out = []
    for key, label in (("test_suite_version", "test suite version"), ("dataset_snapshot", "dataset snapshot"),
                       ("evaluation_config", "evaluation configuration")):
        if base["meta"].get(key) != cur["meta"].get(key):
            out.append(f"{label} differs (baseline {base['meta'].get(key)!r} vs current {cur['meta'].get(key)!r})")
    if set(base["cases"]) != set(cur["cases"]):
        out.append(f"case sets differ (baseline {len(base['cases'])}, current {len(cur['cases'])}); smoke vs full mode?")
    return out


def render_diff_html(d: dict[str, Any]) -> str:
    import html as h
    li = lambda xs: "".join(f"<li>{h.escape(str(x))}</li>" for x in xs) or "<li>none</li>"
    deltas = "".join(f"<tr><td>{h.escape(k)}</td><td>{'n/a' if v is None else format(v, '+.4f') if isinstance(v, float) else v}</td></tr>"
                     for k, v in d["metric_deltas"].items())
    cats = "".join(f"<tr><td>{h.escape(k)}</td><td>{'n/a' if v is None else format(v, '+.4f')}</td></tr>"
                   for k, v in d["category_accuracy_deltas"].items())
    changed = "".join(f"<tr><td>{h.escape(c['case_id'])}</td><td>{h.escape(json.dumps(c['before']))}</td><td>{h.escape(json.dumps(c['after']))}</td></tr>"
                      for c in d["changed_answers"][:200])
    verdict = "PASS" if d["passed"] else "FAIL"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>FinGuard regression diff</title>
<style>body{{font:14px system-ui;max-width:1000px;margin:24px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}}
.pass{{color:#047857}}.fail{{color:#b91c1c}}</style></head><body>
<h1>Regression check: <span class="{verdict.lower()}">{verdict}</span></h1>
<p>baseline {h.escape(d['baseline_run'])} vs current {h.escape(d['current_run'])}</p>
<h2>Breached thresholds</h2><ul>{li([b['rule'] + ': ' + b['detail'] for b in d['breaches']])}</ul>
<h2>Metric deltas</h2><table>{deltas}</table><h2>Per-category accuracy deltas</h2><table>{cats}</table>
<h2>Newly failing ({len(d['newly_failing'])})</h2><ul>{li(d['newly_failing'][:100])}</ul>
<h2>Newly passing ({len(d['newly_passing'])})</h2><ul>{li(d['newly_passing'][:100])}</ul>
<h2>Changed answers ({len(d['changed_answers'])})</h2><table><tr><th>case</th><th>before</th><th>after</th></tr>{changed}</table>
<h2>Context changes vs baseline</h2><pre>{h.escape(json.dumps(d.get('context_changes', {}), indent=1))}</pre><h2>Warnings</h2><ul>{li(d['warnings'])}</ul></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare the latest evaluation run with a saved baseline")
    ap.add_argument("--db", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--baseline", default=None, help="baseline name (default from settings)")
    ap.add_argument("--set-baseline", action="store_true")
    args = ap.parse_args(argv)
    load_env()
    settings = load_settings()
    th = settings["regression"]
    name = args.baseline or th["baseline_name"]
    con = open_db(args.db or path_for(settings, "db"))
    try:
        cur = load_run_snapshot(con, settings, args.run_id)
        if args.set_baseline:
            set_baseline(con, name, cur)
            print(f"baseline '{name}' set from run {cur['run_id']} ({cur['metrics']['total']} cases, accuracy {cur['metrics']['accuracy']:.3f})")
            return 0
        base = load_baseline(con, name)
        if base is None:
            print(f"ERROR: no baseline named '{name}'. Create one with --set-baseline.", file=sys.stderr)
            return 2
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    problems = incompatibilities(base, cur)
    if problems:
        print(f"ERROR: baseline '{name}' is not comparable with run {cur['run_id']}:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        print("Re-baseline explicitly after reviewing the change:  python -m src.regression.compare --set-baseline", file=sys.stderr)
        return 4
    diff = diff_runs(base, cur, th)
    out = path_for(settings, "reports")
    out.mkdir(parents=True, exist_ok=True)
    (out / "regression_diff.json").write_text(json.dumps(diff, indent=2, default=str), encoding="utf-8")
    (out / "regression_diff.html").write_text(render_diff_html(diff), encoding="utf-8")
    print(f"baseline {diff['baseline_run']}  vs  current {diff['current_run']}")
    for w in diff["warnings"]:
        print("  warning:", w)
    print(f"  newly failing: {len(diff['newly_failing'])}  newly passing: {len(diff['newly_passing'])}  changed answers: {len(diff['changed_answers'])}")
    for k, v in diff["metric_deltas"].items():
        print(f"  delta {k}: {v}")
    if diff["passed"]:
        print("REGRESSION CHECK: PASS")
        return 0
    print("REGRESSION CHECK: FAIL")
    for b in diff["breaches"]:
        print(f"  - {b['rule']}: {b['detail']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
