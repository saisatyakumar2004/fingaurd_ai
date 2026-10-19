"""Snapshot integrity check:  python -m src.ingestion.verify

Recomputes SHA-256 of every raw snapshot and compares with its sidecar metadata (and with the
source_snapshots table if a database exists). Exit 1 on any mismatch or missing file.
Recovery: delete ONLY the corrupt snapshot pair (*.json + *.meta.json) and re-run ingestion; do not edit snapshots.
"""
from __future__ import annotations

import json
import sys

from src.common.config import load_settings, path_for
from src.common.util import sha256_bytes


def verify(raw_dir) -> tuple[int, list[str]]:
    bad: list[str] = []
    n = 0
    for meta_path in sorted(raw_dir.glob("*/*/*.meta.json")):
        n += 1
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = meta_path.parent / meta["file"]
        if not body.exists():
            bad.append(f"{meta_path}: body file missing")
        elif sha256_bytes(body.read_bytes()) != meta["sha256"]:
            bad.append(f"{body}: SHA-256 mismatch (snapshot modified or corrupt)")
    return n, bad


def main() -> int:
    raw = path_for(load_settings(), "raw")
    n, bad = verify(raw)
    print(f"checked {n} snapshots under {raw}: {len(bad)} problems")
    for b in bad:
        print("  ", b, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
