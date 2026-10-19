"""Offline test fixtures.

Everything here is SYNTHETIC: fake companies ('Alpha Test Corp' ...), fake CIKs (9,000,0xx) and
fake FIGIs. The payloads copy the *shape* of SEC companyfacts so the real normalization code is
exercised, but no number here is real financial data.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.common.config import load_settings
from src.common.db import insert_snapshot, open_db
from src.common.util import pad_cik
from src.ingestion.load import company_id_for, store_figi_result, store_resolution, store_ticker_map, upsert_company
from src.ingestion.openfigi import FigiRow
from src.ingestion.sec import TickerEntry
from src.identity.resolver import Resolution
from src.normalization.sec_normalize import merge_company_observations, normalize_company_facts

WORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
YEARS = range(2019, 2024)


def synthetic_universe(n: int = 8) -> list[dict[str, Any]]:
    return [{"ticker": f"TST{chr(65 + i)}", "name": f"{WORDS[i]} Test Corp", "cik": 9000001 + i,
             "sector": "Synthetic", "concepts": ["revenue", "net_income", "total_assets"], "lei": None, "figi": None}
            for i in range(n)]


def _fact(start, end, val, accn, fy, form, filed, fp="FY"):
    d = {"end": end, "val": int(val), "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def synthetic_company_facts(cik: int, name: str, idx: int, restate_fy: int | None = None) -> dict[str, Any]:
    rev0, ni0, as0, g = 1.0e9 * (idx + 1), 1.0e8 * (idx + 1), 5.0e9 * (idx + 1), 1.10 + 0.01 * idx
    revenue, income, assets = [], [], []
    for i, y in enumerate(YEARS):
        rev, ni, ast = rev0 * g ** i, ni0 * g ** i, as0 * g ** i
        reports = [(y, f"{cik:010d}-{(y + 1) % 100:02d}-000001", f"{y + 1}-02-15", 1.0)]
        if y < YEARS[-1]:   # comparative re-reported in the next 10-K
            factor = 1.02 if restate_fy == y else 1.0
            reports.append((y + 1, f"{cik:010d}-{(y + 2) % 100:02d}-000001", f"{y + 2}-02-15", factor))
        for fy, accn, filed, factor in reports:
            revenue.append(_fact(f"{y}-01-01", f"{y}-12-31", rev * factor, accn, fy, "10-K", filed))
            income.append(_fact(f"{y}-01-01", f"{y}-12-31", ni * factor, accn, fy, "10-K", filed))
            assets.append(_fact(None, f"{y}-12-31", ast * factor, accn, fy, "10-K", filed))
    q = f"{cik:010d}-24-000009"
    revenue.append(_fact("2023-01-01", "2023-03-31", rev0 / 4, q, 2023, "10-Q", "2024-05-05", "Q1"))
    assets.append(_fact(None, "2023-03-31", as0, q, 2023, "10-Q", "2024-05-05", "Q1"))
    return {"cik": cik, "entityName": name.upper(), "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": revenue}},
        "NetIncomeLoss": {"units": {"USD": income}},
        "Assets": {"units": {"USD": assets}},
    }}}


def build_db(n: int = 8, path: str = ":memory:", with_identity: bool = True):
    """Fully populated reference DB built through the real loaders / normalizer."""
    settings = load_settings()
    con = open_db(path)
    universe = synthetic_universe(n)
    entries = [TickerEntry(cik_str=c["cik"], ticker=c["ticker"], title=c["name"].upper()) for c in universe]
    tick_snap = {"snapshot_id": "sec_edgar-tickers0000000", "source_system": "sec_edgar",
                 "url": "https://www.sec.gov/files/company_tickers.json", "request_params": None,
                 "retrieval_timestamp": datetime(2026, 1, 1), "http_status": 200, "sha256": "0" * 64,
                 "byte_size": 1, "raw_path": "sec_edgar/company_tickers/x.json", "status": "ok"}
    insert_snapshot(con, tick_snap)
    store_ticker_map(con, entries, tick_snap["snapshot_id"])
    for i, cfg in enumerate(universe):
        cid = upsert_company(con, cfg, entries, tick_snap["snapshot_id"])
        payload = synthetic_company_facts(cfg["cik"], cfg["name"], i, restate_fy=2021 if i == 0 else None)
        snap = {"snapshot_id": f"sec_edgar-fact{i:012d}", "source_system": "sec_edgar",
                "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pad_cik(cfg['cik'])}.json", "request_params": None,
                "retrieval_timestamp": datetime(2026, 1, 1), "http_status": 200, "sha256": f"{i:064d}", "byte_size": 1,
                "raw_path": f"sec_edgar/companyfacts_CIK{pad_cik(cfg['cik'])}/x.json", "status": "ok"}
        insert_snapshot(con, snap)
        rows, _ = normalize_company_facts(payload, company_id=cid, cik=cfg["cik"], concepts=cfg["concepts"],
                                          snapshot=snap, settings=settings)
        merge_company_observations(con, cid, rows)
        if with_identity:
            store_resolution(con, cid, cfg["name"], "legal_name",
                             Resolution("CANDIDATE_UNIQUE", f"LEI{i:016d}XX", [], "synthetic"), snap["snapshot_id"])
            figi = FigiRow(figi=f"BBG{i:09d}", ticker=cfg["ticker"], exchCode="US", compositeFIGI=f"BBGC{i:08d}",
                           shareClassFIGI=f"BBGS{i:08d}", securityType2="Common Stock", marketSector="Equity", name=cfg["name"])
            store_figi_result(con, cid, {"idType": "TICKER", "idValue": cfg["ticker"], "exchCode": "US"},
                              {"status": "matched", "rows": [figi], "message": ""}, snap["snapshot_id"])
    return con, settings
