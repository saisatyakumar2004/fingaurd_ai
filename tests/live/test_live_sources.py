"""Opt-in LIVE tests. Skipped (NOT RUN) unless:  python -m pytest -m live --run-live
Requires internet; SEC needs SEC_USER_AGENT; LLM test needs ANTHROPIC_API_KEY.
A skipped live test is reported as skipped, never as passed."""
import os

import pytest

from src.common.config import load_env, load_settings
from src.common.http import HttpFetcher
from src.ingestion.gleif import GleifClient, make_gleif_fetcher, parse_lei_records
from src.ingestion.openfigi import OpenFigiClient, make_openfigi_fetcher, parse_mapping_response
from src.ingestion.sec import SecClient, make_sec_fetcher, parse_tickers, validate_company_facts_shape

pytestmark = pytest.mark.live
load_env()


def _need(var):
    if not os.environ.get(var):
        pytest.skip(f"{var} not set")


def test_live_sec_tickers_and_companyfacts(tmp_path):
    _need("SEC_USER_AGENT")
    s = load_settings()
    c = SecClient(make_sec_fetcher(s, tmp_path, offline=False))
    entries = parse_tickers(c.tickers().data)
    assert any(e.ticker == "AAPL" and e.cik_str == 320193 for e in entries)
    facts = c.company_facts(320193).data
    validate_company_facts_shape(facts, 320193)
    assert "Assets" in facts["facts"]["us-gaap"]


def test_live_gleif(tmp_path):
    s = load_settings()
    res = GleifClient(make_gleif_fetcher(s, tmp_path, offline=False)).search("Apple Inc.")
    assert isinstance(parse_lei_records(res.data), list)


def test_live_openfigi(tmp_path):
    s = load_settings()
    fetcher, batch = make_openfigi_fetcher(s, tmp_path, offline=False)
    jobs = [{"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}]
    (chunk, res), = OpenFigiClient(fetcher, batch).map_jobs(jobs)
    assert parse_mapping_response(res.data, 1)[0]["status"] == "matched"


def test_live_llm_smoke():
    _need("ANTHROPIC_API_KEY")
    from src.agent.llm_agent import AnthropicProvider
    out = AnthropicProvider(max_tokens=20).complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}])
    assert out.strip()
