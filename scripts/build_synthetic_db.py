"""Build a SYNTHETIC reference database (fake companies/CIKs) for offline demos and regression proofs.

    python scripts/build_synthetic_db.py data/synthetic.duckdb

Nothing here is real financial data. Reports produced from it must be labelled synthetic.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import build_db  # noqa: E402

if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/synthetic.duckdb")
    for suffix in ("", ".wal"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    con, _ = build_db(path=str(target))
    n = con.execute("SELECT count(*) FROM financial_observations").fetchone()[0]
    print(f"synthetic database written to {target} ({n} observations)")
