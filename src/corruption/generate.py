"""CLI: python -m src.corruption.generate --seed 42 [--n-per-category 20]

Writes test cases to DuckDB (test_cases) and to two JSONL files:
  data/corrupted/agent_inputs.jsonl   -- what an agent may see (no labels, no expected answers)
  data/corrupted/hidden_cases.jsonl   -- full cases incl. expected answers (evaluator only)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.common.config import load_env, load_settings, path_for
from src.common.db import insert_rows, open_db
from src.common.util import utcnow
from src.corruption.engine import CATEGORIES, generate_cases


def persist_cases(con, cases: list[dict[str, Any]], out_dir: Path | None = None, scope: str = "base") -> None:
    """Replace only the cases of this scope (base suite vs multi-fault suite); the other suite is left untouched."""
    now = utcnow()
    con.execute("DELETE FROM test_cases WHERE suite_version " + ("LIKE 'multi-%'" if scope == "multi" else "NOT LIKE 'multi-%'"))
    rows = []
    for c in cases:
        rows.append({
            "case_id": c["case_id"], "suite_version": c["suite_version"], "corruption_type": c["corruption_type"],
            "subtype": c["subtype"], "random_seed": c["random_seed"], "affected_record_id": c["affected_record_id"],
            "corruption_parameters": json.dumps(c["corruption_parameters"], default=str),
            "original_value": json.dumps(c["original_value"], default=str),
            "corrupted_value": json.dumps(c["corrupted_value"], default=str),
            "expected_behavior": c["expected_behavior"],
            "expected_answer": json.dumps(c["expected_answer"], default=str),
            "expected_abstention_or_escalation": c["expected_abstention_or_escalation"],
            "source_evidence": json.dumps(c["source_evidence"], default=str),
            "severity": c["severity"], "smoke": c["smoke"],
            "agent_input": json.dumps(c["agent_input"], default=str),
            "store_overrides": json.dumps(c["store_overrides"], default=str),
            "expected": json.dumps(c["expected"], default=str),
            "generated_at": now,
        })
    insert_rows(con, "test_cases", rows)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / ("agent_inputs_multi.jsonl" if scope == "multi" else "agent_inputs.jsonl"), "w", encoding="utf-8") as f:
            for c in cases:
                f.write(json.dumps(c["agent_input"], default=str) + "\n")
        with open(out_dir / ("hidden_cases_multi.jsonl" if scope == "multi" else "hidden_cases.jsonl"), "w", encoding="utf-8") as f:
            for c in cases:
                f.write(json.dumps(c, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate seeded corrupted test cases")
    ap.add_argument("--db", default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--n-per-category", type=int, default=None)
    ap.add_argument("--multi", action="store_true", help="generate the separate MULTI_FAULT suite instead of the base suite")
    ap.add_argument("--examples", type=int, default=2, help="print N example cases per category")
    args = ap.parse_args(argv)
    load_env()
    settings = load_settings()
    con = open_db(args.db or path_for(settings, "db"))
    if args.multi:
        from src.corruption.multi import generate_multi_cases
        cases, suite, shortfalls = generate_multi_cases(con, settings, args.seed, args.n_per_category or 5)
        persist_cases(con, cases, path_for(settings, "corrupted"), scope="multi")
    else:
        cases, suite, shortfalls = generate_cases(con, settings, args.seed, args.n_per_category)
        persist_cases(con, cases, path_for(settings, "corrupted"))
    counts = Counter(c["corruption_type"] for c in cases)
    print(f"suite {suite}: {len(cases)} cases")
    for cat in (CATEGORIES + ["MULTI_FAULT"]):
        if counts.get(cat, 0) == 0 and cat not in shortfalls:
            continue
        print(f"  {cat:14s} {counts.get(cat, 0)}" + (f"  (SHORT: no eligible data)" if cat in shortfalls else ""))
    for cat in (CATEGORIES + ["MULTI_FAULT"]):
        for c in [x for x in cases if x["corruption_type"] == cat][:args.examples]:
            print(f"\n[{cat}/{c['subtype']}] {c['case_id']} severity={c['severity']}")
            print("  agent sees :", json.dumps(c["agent_input"]["records"], default=str)[:400])
            print("  expected   :", c["expected_behavior"])
    return 1 if (shortfalls and not args.multi) else 0


if __name__ == "__main__":
    raise SystemExit(main())
