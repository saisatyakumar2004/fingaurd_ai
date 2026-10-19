"""Environment check:  python -m src.common.doctor"""
from __future__ import annotations

import importlib
import os
import sys
from importlib import metadata

REQUIRED = {"duckdb": "duckdb", "pydantic": "pydantic", "requests": "requests", "yaml": "PyYAML", "pytest": "pytest"}
OPTIONAL = {"dotenv": "python-dotenv", "anthropic": "anthropic", "streamlit": "streamlit"}


def check() -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    v = sys.version_info
    notes.append(f"python {v.major}.{v.minor}.{v.micro}")
    if (v.major, v.minor) < (3, 11):
        problems.append("Python 3.11+ is required")
    elif (v.major, v.minor) != (3, 11):
        notes.append("tested: 3.11.17 and 3.12.10 (docs/TEST_RESULTS.md); other versions untested")
    for mod, dist in REQUIRED.items():
        try:
            importlib.import_module(mod)
            notes.append(f"{dist} {metadata.version(dist)}")
        except Exception as e:
            problems.append(f"missing required package {dist} ({type(e).__name__}); run: pip install -r requirements.txt")
    for mod, dist in OPTIONAL.items():
        try:
            importlib.import_module(mod)
            notes.append(f"{dist} {metadata.version(dist)} (optional)")
        except Exception:
            notes.append(f"{dist} not installed (optional)")
    ua = os.environ.get("SEC_USER_AGENT", "")
    notes.append("SEC_USER_AGENT: " + ("set" if "@" in ua else "NOT SET (needed for online SEC ingestion only)"))
    notes.append("OPENFIGI_API_KEY: " + ("set" if os.environ.get("OPENFIGI_API_KEY") else "not set (anonymous, slower)"))
    notes.append("ANTHROPIC_API_KEY: " + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "not set (real-LLM evaluation = NOT RUN)"))
    return problems, notes


def main() -> int:
    from src.common.config import load_env
    load_env()
    problems, notes = check()
    for n in notes:
        print("  ", n)
    for p in problems:
        print("PROBLEM:", p)
    print("doctor:", "FAIL" if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
