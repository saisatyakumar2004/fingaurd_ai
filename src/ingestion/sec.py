"""SEC EDGAR adapter (https://www.sec.gov/edgar/sec-api-documentation)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from src.common.http import FetchResult, HttpFetcher
from src.common.util import pad_cik

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"


class TickerEntry(BaseModel):
    cik_str: int
    ticker: str
    title: str


def parse_tickers(payload: Any) -> list[TickerEntry]:
    """company_tickers.json is {"0": {"cik_str":..., "ticker":..., "title":...}, ...}."""
    items = payload.values() if isinstance(payload, dict) else payload
    out: list[TickerEntry] = []
    bad = 0
    for it in items:
        try:
            out.append(TickerEntry.model_validate(it))
        except ValidationError:
            bad += 1
    if not out:
        raise ValueError("SEC ticker payload contained no valid entries")
    return out


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"


class SecProfile(BaseModel):
    """Independent company facts from SEC submissions: used ONLY to corroborate GLEIF candidates."""
    name: str
    state_of_incorporation: Optional[str] = None
    business_city: Optional[str] = None
    business_state: Optional[str] = None


def parse_submissions(payload: Any) -> SecProfile:
    if not isinstance(payload, dict) or "name" not in payload:
        raise ValueError("submissions payload missing 'name'")
    biz = ((payload.get("addresses") or {}).get("business")) or {}
    return SecProfile(name=payload["name"], state_of_incorporation=payload.get("stateOfIncorporation") or None,
                      business_city=biz.get("city"), business_state=biz.get("stateOrCountry"))


def validate_company_facts_shape(payload: Any, expected_cik: int) -> None:
    if not isinstance(payload, dict) or "facts" not in payload:
        raise ValueError("companyfacts payload missing 'facts'")
    if int(payload.get("cik", -1)) != int(expected_cik):
        raise ValueError(f"companyfacts cik {payload.get('cik')} != requested {expected_cik}")


PLACEHOLDER_MARKERS = ("example.com", "example.org", "your-project-email", "your_email", "yourname", "changeme")


def read_sec_user_agent(env=None) -> str:
    """Return the configured SEC User-Agent or raise an actionable error. Never logs the value."""
    env = os.environ if env is None else env
    ua = (env.get("SEC_USER_AGENT") or "").strip().strip('"').strip("'")
    fix = ("Set it in the local .env file (copy .env.example to .env; .env is gitignored), in the form: "
           "SEC_USER_AGENT=FinGuardAI <your-project-email>. "
           "SEC fair-access policy requires a descriptive User-Agent with real contact information. "
           "Use --offline to run from cached snapshots without contacting the SEC.")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT is not set. " + fix)
    if "@" not in ua or any(m in ua.lower() for m in PLACEHOLDER_MARKERS):
        raise RuntimeError("SEC_USER_AGENT is missing a real contact email (placeholder or no '@' found). " + fix)
    return ua


def make_sec_fetcher(settings: dict, raw_dir: Path, offline: bool, session: Any | None = None) -> HttpFetcher:
    ua = "offline-cache-only" if offline else read_sec_user_agent()
    cfg = settings["sec"]
    return HttpFetcher(
        "sec_edgar", raw_dir,
        headers={"User-Agent": ua or "offline", "Accept-Encoding": "gzip, deflate"},
        min_interval=float(cfg["min_request_interval_seconds"]),
        timeout=float(cfg["timeout_seconds"]),
        max_retries=int(cfg["max_retries"]),
        offline=offline, session=session,
    )


class SecClient:
    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def tickers(self, refresh: bool = False) -> FetchResult:
        return self.fetcher.fetch(SEC_TICKERS_URL, "company_tickers", refresh=refresh)

    def submissions(self, cik: int, refresh: bool = False) -> FetchResult:
        c10 = pad_cik(cik)
        return self.fetcher.fetch(SEC_SUBMISSIONS_URL.format(cik10=c10), f"submissions_CIK{c10}", refresh=refresh)

    def company_facts(self, cik: int, refresh: bool = False) -> FetchResult:
        c10 = pad_cik(cik)
        return self.fetcher.fetch(SEC_FACTS_URL.format(cik10=c10), f"companyfacts_CIK{c10}", refresh=refresh)
