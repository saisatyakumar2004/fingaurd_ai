"""Inspect which XBRL tags / units / period shapes each cached companyfacts file actually uses.

Run AFTER the first online ingestion and BEFORE trusting concept extraction:
    python scripts/inspect_companyfacts.py
It reads data/raw/sec_edgar/companyfacts_CIK*/ (the newest snapshot of each) and prints, per
company and concept, which candidate tags exist, their units, latest period end and fact counts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.config import ROOT, load_settings, path_for  # noqa: E402
from src.normalization.sec_normalize import CONCEPTS  # noqa: E402


def main() -> int:
    raw = path_for(load_settings(), "raw") / "sec_edgar"
    dirs = sorted(raw.glob("companyfacts_CIK*"))
    if not dirs:
        print(f"No cached companyfacts under {raw}. Run the online ingestion first.")
        return 1
    for d in dirs:
        bodies = sorted(p for p in d.glob("*.json") if not p.name.endswith(".meta.json"))
        if not bodies:
            continue
        payload = json.loads(bodies[-1].read_text(encoding="utf-8"))
        gaap = (payload.get("facts") or {}).get("us-gaap", {})
        print(f"\n{payload.get('entityName')}  ({d.name}, {bodies[-1].name})")
        for concept, spec in CONCEPTS.items():
            found = []
            for tag in spec["tags"]:
                node = gaap.get(tag)
                if not node:
                    continue
                for unit, items in (node.get("units") or {}).items():
                    latest = max((i.get("end", "") for i in items), default="")
                    found.append(f"{tag}[{unit}] n={len(items)} latest_end={latest}")
            print(f"  {concept:13s}", "; ".join(found) if found else "NO CANDIDATE TAG PRESENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
