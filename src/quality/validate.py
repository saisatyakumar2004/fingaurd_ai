"""Run all deterministic checks and expose the evaluation gate.

    python -m src.quality.validate        (exit 1 if the gate is blocked)

The gate blocks dependent evaluation when a confirmed error with a severity listed in
quality.block_on exists in the latest validation run, when no validation has been run, or when
the data changed after the last validation (fingerprint mismatch).
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from collections import Counter
from typing import Any

from src.common.config import load_env, load_settings, path_for
from src.common.db import fetch_dicts, insert_rows, open_db
from src.common.util import stable_id, utcnow
from src.quality.rules import CONFIRMED, RULES, SEVERITY_ORDER, Issue, equivalent_duplicate_stats


def data_fingerprint(con) -> str:
    n_obs, s = con.execute("SELECT count(*), coalesce(sum(CASE WHEN isfinite(value) THEN value ELSE 0 END), 0) "
                           "FROM financial_observations").fetchone()
    n_c = con.execute("SELECT count(*) FROM companies").fetchone()[0]
    n_i = con.execute("SELECT count(*) FROM identity_resolutions").fetchone()[0]
    n_m = con.execute("SELECT count(*) FROM instrument_mappings").fetchone()[0]
    return stable_id(n_obs, round(float(s), 2), n_c, n_i, n_m)


def _cfg(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(settings)
    cfg["_annual_days"] = tuple(settings["sec"]["annual_days"])
    cfg["_quarterly_days"] = tuple(settings["sec"]["quarterly_days"])
    return cfg


def run_validation(con, settings: dict[str, Any]) -> dict[str, Any]:
    cfg = _cfg(settings)
    block_on = set(settings["quality"]["block_on"])
    started = utcnow()
    run_id = f"val-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:6]}"   # sortable, microsecond resolution
    issues: dict[str, Issue] = {}
    for rule_id, fn, desc in RULES:
        try:
            found = fn(con, cfg)
        except Exception as e:  # a crashing rule must never silently pass
            found = [Issue("DQ000", "critical", CONFIRMED, rule_id, f"rule {rule_id} executes",
                           f"rule crashed: {type(e).__name__}: {e}")]
        for i in found:
            issues.setdefault(i.issue_id, i)
    ordered = sorted(issues.values(), key=lambda i: (SEVERITY_ORDER[i.severity], i.rule_id, i.affected_record))
    now = utcnow()
    insert_rows(con, "quality_issues", [{
        "issue_id": i.issue_id, "validation_run_id": run_id, "rule_id": i.rule_id, "severity": i.severity,
        "classification": i.classification, "affected_record": i.affected_record,
        "expected_condition": i.expected_condition, "observed_condition": i.observed_condition,
        "evidence": json.dumps(i.evidence, default=str), "created_at": now} for i in ordered])
    blockers = [i for i in ordered if i.classification == CONFIRMED and i.severity in block_on]
    counts = Counter((i.rule_id, i.severity, i.classification) for i in ordered)
    summary = {
        "validation_run_id": run_id,
        "total_issues": len(ordered),
        "confirmed_errors": sum(1 for i in ordered if i.classification == CONFIRMED),
        "needs_review": sum(1 for i in ordered if i.classification != CONFIRMED),
        "by_classification": dict(Counter(i.classification for i in ordered)),
        "not_reported_by_design": equivalent_duplicate_stats(con),
        "blocking_issues": len(blockers),
        "by_rule": [{"rule_id": k[0], "severity": k[1], "classification": k[2], "count": v} for k, v in sorted(counts.items())],
    }
    insert_rows(con, "validation_runs", [{
        "validation_run_id": run_id, "started_at": started, "finished_at": utcnow(),
        "blocked": bool(blockers), "data_fingerprint": data_fingerprint(con), "summary": json.dumps(summary)}])
    summary["blocked"] = bool(blockers)
    summary["blockers"] = [{"rule_id": i.rule_id, "record": i.affected_record, "observed": i.observed_condition}
                           for i in blockers[:25]]
    return summary


def gate_status(con) -> dict[str, Any]:
    """Return {'blocked': bool, 'reasons': [...], 'validation_run_id': str|None}."""
    rows = fetch_dicts(con, "SELECT * FROM validation_runs ORDER BY validation_run_id DESC LIMIT 1")
    if not rows:
        return {"blocked": True, "validation_run_id": None,
                "reasons": ["no validation run found: run `python -m src.quality.validate` first"]}
    v = rows[0]
    reasons: list[str] = []
    if v["blocked"]:
        crit = fetch_dicts(con, "SELECT rule_id, count(*) AS n FROM quality_issues WHERE validation_run_id = ? "
                                "AND classification = 'confirmed_error' AND severity = 'critical' GROUP BY rule_id", [v["validation_run_id"]])
        reasons.append("critical confirmed data-quality errors: " + ", ".join(f"{c['rule_id']} x{c['n']}" for c in crit))
    if v["data_fingerprint"] != data_fingerprint(con):
        reasons.append("reference data changed since the last validation run: re-run validation")
    return {"blocked": bool(reasons), "reasons": reasons, "validation_run_id": v["validation_run_id"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run deterministic data-quality validation")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    load_env()
    settings = load_settings()
    con = open_db(args.db or path_for(settings, "db"))
    summary = run_validation(con, settings)
    print(f"validation run {summary['validation_run_id']}: {summary['total_issues']} issues "
          f"({summary['confirmed_errors']} confirmed errors, {summary['needs_review']} need review)")
    for r in summary["by_rule"]:
        print(f"  {r['rule_id']} {r['severity']:8s} {r['classification']:15s} x{r['count']}")
    if summary["blocked"]:
        print("GATE: BLOCKED - evaluation will refuse to run until these are resolved:")
        for b in summary["blockers"]:
            print("  ", b)
        return 1
    print("GATE: OPEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
