"""Ingestion CLI:  python -m src.ingestion.run [--offline] [--refresh] [--limit N]

Requires internet (and SEC_USER_AGENT) for the FIRST collection. After that, `--offline`
re-runs entirely from cached snapshots in data/raw. Raw responses are never overwritten.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from src.common.config import load_env, load_settings, load_universe, path_for
from src.common.db import insert_snapshot, open_db
from src.common.http import FetchError, OfflineCacheMiss
from src.common.log import get_logger, log
from src.common.util import utcnow
from src.identity.resolver import Resolution, resolve_entity
from src.ingestion import gleif as gleif_mod
from src.ingestion import openfigi as figi_mod
from src.ingestion.load import (company_id_for, store_figi_result, store_resolution,
                                store_ticker_map, upsert_company)
from src.ingestion.sec import SecClient, make_sec_fetcher, parse_submissions, parse_tickers, validate_company_facts_shape
from src.normalization.sec_normalize import merge_company_observations, normalize_company_facts

logger = get_logger("ingestion")


def run_ingestion(con, settings: dict[str, Any], universe: list[dict[str, Any]], *, offline: bool = False,
                  refresh: bool = False, limit: int | None = None, skip_gleif: bool = False,
                  skip_figi: bool = False, session: Any | None = None, raw_dir=None) -> dict[str, Any]:
    raw_dir = raw_dir or path_for(settings, "raw")
    universe = universe[:limit] if limit else universe
    sec = SecClient(make_sec_fetcher(settings, raw_dir, offline, session))
    summary: dict[str, Any] = {"started_at": utcnow().isoformat(), "offline": offline,
                               "companies": {}, "failures": [], "gleif": {}, "openfigi": {}}

    t = sec.tickers(refresh=refresh)
    insert_snapshot(con, t.snapshot)
    entries = parse_tickers(t.data)
    store_ticker_map(con, entries, t.snapshot["snapshot_id"])
    summary["sec_ticker_snapshot"] = t.snapshot["snapshot_id"]

    for cfg in universe:
        cid = upsert_company(con, cfg, entries, t.snapshot["snapshot_id"])
        try:
            res = sec.company_facts(cfg["cik"], refresh=refresh)
            insert_snapshot(con, res.snapshot)
            validate_company_facts_shape(res.data, cfg["cik"])
            rows, stats = normalize_company_facts(
                res.data, company_id=cid, cik=cfg["cik"], concepts=cfg["concepts"],
                snapshot=res.snapshot, settings=settings)
            total = merge_company_observations(con, cid, rows)
            summary["companies"][cfg["ticker"]] = {**stats, "stored_total": total, "from_cache": res.from_cache}
            log(logger, logging.INFO, "company_ingested", ticker=cfg["ticker"], **stats)
        except (FetchError, ValueError) as e:
            kind = "cache_miss" if isinstance(e, OfflineCacheMiss) else "error"
            summary["failures"].append({"ticker": cfg["ticker"], "stage": "sec_companyfacts", "kind": kind, "error": str(e)})
            log(logger, logging.ERROR, "company_failed", ticker=cfg["ticker"], error=str(e))

    if not skip_gleif:
        _ingest_gleif(con, settings, universe, offline, refresh, session, raw_dir, summary)
    if not skip_figi:
        _ingest_figi(con, settings, universe, offline, refresh, session, raw_dir, summary)

    summary["finished_at"] = utcnow().isoformat()
    return summary


def _ingest_gleif(con, settings, universe, offline, refresh, session, raw_dir, summary) -> None:
    client = gleif_mod.GleifClient(gleif_mod.make_gleif_fetcher(settings, raw_dir, offline, session),
                                   page_size=int(settings["gleif"]["page_size"]))
    sec = SecClient(make_sec_fetcher(settings, raw_dir, offline, session))
    for cfg in universe:
        cid = company_id_for(cfg["cik"])
        profile = None
        try:                                  # independent corroboration source; failure only removes the evidence
            pres = sec.submissions(cfg["cik"], refresh)
            insert_snapshot(con, pres.snapshot)
            profile = parse_submissions(pres.data).model_dump()
        except (FetchError, ValueError) as e:
            summary["failures"].append({"ticker": cfg["ticker"], "stage": "sec_submissions", "error": str(e)})
        sec_title = con.execute("SELECT sec_entity_name FROM companies WHERE company_id = ?", [cid]).fetchone()
        queries = [cfg["name"]] + ([sec_title[0]] if sec_title and sec_title[0] and sec_title[0] != cfg["name"] else [])
        try:
            resolution, res, used, mode = None, None, None, "legal_name"
            for q in queries:                 # configured name first, then SEC's registered name
                for mode in ("legal_name", "fulltext"):
                    res = client.search(q, mode, refresh)
                    insert_snapshot(con, res.snapshot)
                    resolution = resolve_entity(q, gleif_mod.parse_lei_records(res.data), expected_lei=cfg.get("lei"),
                                                sec_profile=profile)
                    used = q
                    if resolution.status != "UNMATCHED":
                        break
                if resolution.status != "UNMATCHED":
                    break
            if profile:
                resolution.rationale += f" [SEC profile used: state={profile.get('state_of_incorporation')} city={profile.get('business_city')}]"
            store_resolution(con, cid, used, mode, resolution, res.snapshot["snapshot_id"])
            summary["gleif"][cfg["ticker"]] = resolution.status
        except (FetchError, ValueError) as e:
            store_resolution(con, cid, cfg["name"], "none", Resolution("SOURCE_UNAVAILABLE", None, [], str(e)[:300]), None)
            summary["gleif"][cfg["ticker"]] = "SOURCE_UNAVAILABLE"
            summary["failures"].append({"ticker": cfg["ticker"], "stage": "gleif", "error": str(e)})


def _ingest_figi(con, settings, universe, offline, refresh, session, raw_dir, summary) -> None:
    fetcher, batch = figi_mod.make_openfigi_fetcher(settings, raw_dir, offline, session)
    client = figi_mod.OpenFigiClient(fetcher, batch)
    exch = settings["openfigi"]["exch_code"]
    jobs = [{"idType": "TICKER", "idValue": c["ticker"], "exchCode": exch} for c in universe]
    by_ticker = {c["ticker"]: c for c in universe}
    try:
        for chunk, res in client.map_jobs(jobs, refresh=refresh):
            insert_snapshot(con, res.snapshot)
            parsed = figi_mod.parse_mapping_response(res.data, len(chunk))
            for job, p in zip(chunk, parsed):
                cfg = by_ticker[job["idValue"]]
                store_figi_result(con, company_id_for(cfg["cik"]), job, p, res.snapshot["snapshot_id"])
                summary["openfigi"][cfg["ticker"]] = {"status": p["status"], "rows": len(p["rows"])}
    except (FetchError, ValueError) as e:
        summary["failures"].append({"stage": "openfigi", "error": str(e)})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect SEC / GLEIF / OpenFIGI reference data")
    ap.add_argument("--db", default=None)
    ap.add_argument("--offline", action="store_true", help="use cached snapshots only")
    ap.add_argument("--refresh", action="store_true", help="re-fetch and add NEW snapshots if content changed")
    ap.add_argument("--tickers", default=None, help="comma-separated tickers to ingest (e.g. AAPL,MSFT)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N companies")
    ap.add_argument("--skip-gleif", action="store_true")
    ap.add_argument("--skip-figi", action="store_true")
    args = ap.parse_args(argv)

    load_env()
    settings = load_settings()
    db_path = args.db or path_for(settings, "db")
    con = open_db(db_path)
    try:
        universe = load_universe()
        if args.tickers:
            wanted = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
            universe = [c for c in universe if c["ticker"] in wanted]
            if not universe:
                print(f"ERROR: none of {sorted(wanted)} found in config/universe.yaml", file=sys.stderr)
                return 2
        summary = run_ingestion(con, settings, universe, offline=args.offline, refresh=args.refresh,
                                limit=args.limit, skip_gleif=args.skip_gleif, skip_figi=args.skip_figi)
    except OfflineCacheMiss as e:
        print(f"ERROR: {e}. Run once online (needs SEC_USER_AGENT) to populate data/raw, or drop --offline.", file=sys.stderr)
        return 3
    except (RuntimeError, FetchError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    out = path_for(settings, "normalized") / "ingestion_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    try:  # analytical export for ad-hoc inspection
        pq = str(path_for(settings, "normalized") / "financial_observations.parquet").replace("\\", "/")
        con.execute(f"COPY financial_observations TO '{pq}' (FORMAT PARQUET)")
    except Exception as e:  # pragma: no cover
        print(f"warning: parquet export skipped: {e}", file=sys.stderr)

    n_obs = con.execute("SELECT count(*) FROM financial_observations").fetchone()[0]
    print(f"companies ingested: {len(summary['companies'])}  observations stored: {n_obs}  failures: {len(summary['failures'])}")
    for f in summary["failures"]:
        print("  FAILURE:", f)
    print(f"summary written to {out}")
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
