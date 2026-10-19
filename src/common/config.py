from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load .env from the project root if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env", override=False)


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    with open(resolve(path or "config/settings.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe(path: str | Path | None = None) -> list[dict[str, Any]]:
    with open(resolve(path or "config/universe.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    default_concepts = list((data.get("defaults") or {}).get("concepts") or [])
    out: list[dict[str, Any]] = []
    seen_t: set[str] = set()
    seen_c: set[int] = set()
    for raw in data["companies"]:
        c = dict(raw)
        c["ticker"] = str(c["ticker"]).upper()
        c["cik"] = int(c["cik"])
        c.setdefault("concepts", default_concepts)
        if c["ticker"] in seen_t or c["cik"] in seen_c:
            raise ValueError(f"Duplicate ticker/CIK in universe: {c['ticker']} / {c['cik']}")
        seen_t.add(c["ticker"])
        seen_c.add(c["cik"])
        out.append(c)
    return out


def path_for(settings: dict[str, Any], key: str) -> Path:
    """Configured path; FINGUARD_<KEY>_DIR (e.g. FINGUARD_REPORTS_DIR) overrides it, e.g. to keep synthetic demo output apart."""
    import os
    override = os.environ.get(f"FINGUARD_{key.upper()}_DIR")
    return resolve(override) if override else resolve(settings["paths"][key])
