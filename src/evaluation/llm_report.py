"""reports/llm_evaluation.json / .html for the real-model path.

A run that could not happen is reported as NOT_RUN with the reason - never as zero scores and never as a pass.
Every number is derived from stored case-level results (the `results` list of an evaluation run).
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from src.common.util import utcnow

HEADLINE = ["numeric_accuracy", "identity_accuracy", "accuracy", "false_positive_rate", "evidence_completeness"]


def build_llm_report(*, status: str, reason: str | None = None, out: dict[str, Any] | None = None, requested_mode: str = "") -> dict[str, Any]:
    report: dict[str, Any] = {"status": status, "reason": reason, "requested_mode": requested_mode,
                              "generated_at": utcnow().isoformat(), "note": (
        "Self-reported confidence is not a calibrated probability. Metrics are computed from stored case outputs.")}
    if out is None:
        report.update(run=None, metrics=None, execution=None, cases=[],
                      statement="Real-LLM evaluation was NOT RUN: no model output exists. All live-model claims are unverified.")
        return report
    m = out["metrics"]
    d, ab = m["detection"], m["abstention"]
    report.update(
        run=out["run"], execution=out.get("execution"),
        metrics={"total": m["total"], "passed": m["passed"], "accuracy": m["accuracy"], "status_accuracy": m["status_accuracy"],
                 "numeric_accuracy": m["numeric_accuracy"], "identity_accuracy": m["identity_accuracy"],
                 "detection_precision": d["precision"], "detection_recall": d["recall"], "detection_f1": d["f1"],
                 "false_positive_rate": m["false_positive_rate"], "correct_abstention_rate": ab["abstention_recall"],
                 "incorrect_abstention_rate": ab["incorrect_abstention_rate"],
                 "high_confidence_incorrect": m["high_confidence_wrong"]["count"],
                 "evidence_completeness": m["evidence_completeness"], "by_category": m["by_category"],
                 "brier_score_of_self_reported_confidence": m["calibration"]["brier"]},
        cases=[{"case_id": r["case_id"], "category": r["category"], "passed": r["score"]["passed"], "agent_error": r["agent_error"],
                "response": r["response"]} for r in out["results"]],
        statement="Computed from stored model outputs of this run.")
    return report


def render_html(rep: dict[str, Any]) -> str:
    e = html.escape
    if rep["metrics"] is None:
        body = f"<h2>NOT RUN</h2><p>{e(str(rep['reason']))}</p><p>{e(rep['statement'])}</p>"
    else:
        rows = "".join(f"<tr><td>{e(k)}</td><td>{'n/a' if v is None else (f'{v:.4f}' if isinstance(v, float) else e(str(v)))}</td></tr>"
                       for k, v in rep["metrics"].items() if k != "by_category")
        cats = "".join(f"<tr><td>{e(c)}</td><td>{v['passed']}/{v['n']}</td></tr>" for c, v in rep["metrics"]["by_category"].items())
        ex = "".join(f"<tr><td>{e(k)}</td><td>{e(str(v) if v is not None else 'not available')}</td></tr>" for k, v in (rep["execution"] or {}).items())
        errs = [c for c in rep["cases"] if c["agent_error"]]
        body = (f"<h2>{e(rep['status'])}</h2><p>run {e(rep['run']['run_id'])} model {e(str(rep['run']['model_id']))} prompt {e(str(rep['run']['prompt_version']))} "
                f"suite {e(str(rep['run']['test_suite_version']))}</p><h3>Metrics</h3><table>{rows}</table><h3>By category</h3><table>{cats}</table>"
                f"<h3>Execution</h3><table>{ex}</table><h3>Provider/agent errors: {len(errs)}</h3>"
                f"<ul>{''.join('<li>' + e(c['case_id']) + ': ' + e(str(c['agent_error'])) + '</li>' for c in errs)}</ul>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>FinGuard LLM evaluation</title>"
            f"<style>body{{font:14px system-ui;max-width:900px;margin:24px auto;padding:0 16px}}td,th{{border:1px solid #ddd;padding:4px 8px}}table{{border-collapse:collapse}}</style></head>"
            f"<body><h1>Real-LLM evaluation</h1><p>{e(rep['note'])}</p>{body}</body></html>")


def write_llm_report(reports_dir: Path, rep: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    j, h = reports_dir / "llm_evaluation.json", reports_dir / "llm_evaluation.html"
    j.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    h.write_text(render_html(rep), encoding="utf-8")
    return j, h


def main(argv: list[str] | None = None) -> int:
    """Rebuild reports/llm_evaluation.* from a STORED run file (keeps the original timings, no model call):
       python -m src.evaluation.llm_report --run-id run-... [--status COMPLETED]"""
    import argparse
    from src.common.config import load_settings, path_for
    from src.evaluation.metrics import aggregate
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    settings = load_settings()
    src = path_for(settings, "reports").parent / "reports" / "runs" / f"{args.run_id}.json"
    if not src.exists():
        src = Path(settings["paths"]["reports"]) / "runs" / f"{args.run_id}.json"
    if not src.exists():
        from src.common.config import resolve
        src = resolve(settings["paths"]["reports"]) / "runs" / f"{args.run_id}.json"
    out = json.loads(src.read_text(encoding="utf-8"))
    ev = settings["evaluation"]
    out["metrics"] = aggregate(out["results"], float(ev["high_confidence_threshold"]), int(ev["calibration_bins"]), ev["severity_weights"])
    ex = out["execution"]
    status = "INTERRUPTED" if ex.get("interrupted") else ("COMPLETED" if not ex["agent_errors"] else "PARTIAL")
    j, h = write_llm_report(path_for(settings, "reports"), build_llm_report(status=status, out=out, requested_mode=out["run"]["mode"]))
    print(f"wrote {j} and {h} ({status}, {out['metrics']['total']} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
