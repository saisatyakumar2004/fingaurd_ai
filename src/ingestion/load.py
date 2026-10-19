"""Database loaders shared by the ingestion CLI and the offline test fixtures."""
from __future__ import annotations

import json
from typing import Any

from src.common.db import insert_rows, upsert_row
from src.common.util import canonical_json, pad_cik, stable_id, utcnow
from src.identity.resolver import Resolution, name_similarity
from src.ingestion.sec import TickerEntry


def company_id_for(cik: int) -> str:
    return f"CIK{pad_cik(cik)}"


def verify_universe_entry(cfg: dict[str, Any], entries: list[TickerEntry]) -> tuple[bool, str, str | None]:
    """Check (ticker, cik, name) from the universe file against the SEC ticker file."""
    matches = [e for e in entries if e.ticker.upper() == cfg["ticker"].upper()]
    if not matches:
        return False, "ticker not found in SEC company_tickers.json", None
    for e in matches:
        if e.cik_str == cfg["cik"]:
            sim = name_similarity(cfg["name"], e.title)
            note = "cik matches SEC ticker file" + ("" if sim >= 0.5 else f"; name differs (similarity {sim:.2f}: '{e.title}')")
            return True, note, e.title
    return False, f"CIK mismatch: SEC lists {[e.cik_str for e in matches]} for {cfg['ticker']}, universe says {cfg['cik']}", matches[0].title


def set_identifier(con, company_id: str, id_type: str, id_value: str, level: str, source: str,
                   status: str, snapshot_id: str | None = None) -> None:
    row = {
        "identifier_id": stable_id(company_id, id_type, id_value),
        "company_id": company_id,
        "id_type": id_type,
        "id_value": id_value,
        "level": level,
        "source_system": source,
        "verification_status": status,
        "evidence_snapshot_id": snapshot_id,
    }
    upsert_row(con, "company_identifiers", ["identifier_id"], row)


def store_ticker_map(con, entries: list[TickerEntry], snapshot_id: str) -> None:
    con.execute("DELETE FROM sec_ticker_map")
    seen: set[tuple[str, int]] = set()
    rows = []
    for e in entries:
        key = (e.ticker.upper(), e.cik_str)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"ticker": key[0], "cik": key[1], "title": e.title, "snapshot_id": snapshot_id})
    insert_rows(con, "sec_ticker_map", rows)


def upsert_company(con, cfg: dict[str, Any], entries: list[TickerEntry], snapshot_id: str | None) -> str:
    verified, note, sec_title = verify_universe_entry(cfg, entries)
    cid = company_id_for(cfg["cik"])
    upsert_row(con, "companies", ["company_id"], {
        "company_id": cid,
        "cik": cfg["cik"],
        "ticker": cfg["ticker"],
        "name": cfg["name"],
        "sec_entity_name": sec_title,
        "sector": cfg.get("sector"),
        "enabled_concepts": ",".join(cfg["concepts"]),
        "universe_verified": verified,
        "universe_note": note,
        "updated_at": utcnow(),
    })
    set_identifier(con, cid, "CIK", pad_cik(cfg["cik"]), "entity", "sec_edgar",
                   "verified_sec_ticker_file" if verified else "unverified", snapshot_id)
    set_identifier(con, cid, "TICKER", cfg["ticker"], "listing", "universe_config",
                   "verified_sec_ticker_file" if verified else "unverified", snapshot_id)
    if cfg.get("lei"):
        set_identifier(con, cid, "LEI", str(cfg["lei"]), "entity", "universe_config", "configured_unverified")
    if cfg.get("figi"):
        set_identifier(con, cid, "FIGI", str(cfg["figi"]), "instrument", "universe_config", "configured_unverified")
    return cid


def store_resolution(con, company_id: str, query: str, mode: str, res: Resolution, snapshot_id: str | None) -> None:
    upsert_row(con, "identity_resolutions", ["resolution_id"], {
        "resolution_id": stable_id(company_id, "gleif"),
        "company_id": company_id,
        "query": query,
        "search_mode": mode,
        "status": res.status,
        "selected_lei": res.selected_lei,
        "candidates": json.dumps(res.candidates),
        "rationale": res.rationale,
        "snapshot_id": snapshot_id,
        "created_at": utcnow(),
    })
    if res.selected_lei:
        # A unique exact *name* match is candidate evidence, not proof of identity.
        set_identifier(con, company_id, "LEI", res.selected_lei, "entity", "gleif",
                       ("verified_corroborated" if res.status == "VERIFIED" else "candidate_unique_name_match"), snapshot_id)


def store_figi_result(con, company_id: str, job: dict[str, Any], parsed: dict[str, Any], snapshot_id: str | None) -> None:
    """Persist every returned row; register FIGI identifiers only when the instrument is unambiguous."""
    con.execute("DELETE FROM instrument_mappings WHERE company_id = ?", [company_id])
    qj = canonical_json(job)
    rows: list[dict[str, Any]] = []
    if parsed["status"] != "matched":
        rows.append({"mapping_id": stable_id(company_id, qj, "none"), "company_id": company_id, "query_json": qj,
                     "status": parsed["status"], "figi": None, "composite_figi": None, "share_class_figi": None,
                     "ticker": None, "name": None, "exch_code": None, "security_type": None, "security_type2": None,
                     "market_sector": None, "snapshot_id": snapshot_id, "message": parsed["message"]})
    for r in parsed["rows"]:  # r is a FigiRow
        rows.append({"mapping_id": stable_id(company_id, qj, r.figi), "company_id": company_id, "query_json": qj,
                     "status": "matched", "figi": r.figi, "composite_figi": r.compositeFIGI,
                     "share_class_figi": r.shareClassFIGI, "ticker": r.ticker, "name": r.name,
                     "exch_code": r.exchCode, "security_type": r.securityType, "security_type2": r.securityType2,
                     "market_sector": r.marketSector, "snapshot_id": snapshot_id, "message": ""})
    insert_rows(con, "instrument_mappings", rows)

    # Unambiguous = exactly one distinct share-class FIGI among common-stock rows.
    common = [r for r in parsed["rows"] if (r.securityType2 or "").lower() == "common stock" and r.figi]
    share_classes = {r.shareClassFIGI for r in common if r.shareClassFIGI}
    if len(share_classes) == 1:
        sc = next(iter(share_classes))
        set_identifier(con, company_id, "SHARE_CLASS_FIGI", sc, "instrument", "openfigi", "unique_common_stock", snapshot_id)
        composites = {r.compositeFIGI for r in common if r.compositeFIGI}
        if len(composites) == 1:
            comp = next(iter(composites))
            set_identifier(con, company_id, "COMPOSITE_FIGI", comp, "instrument", "openfigi", "unique_common_stock", snapshot_id)
            # Agent-facing 'FIGI' identifier: the composite FIGI of the US common stock listing.
            set_identifier(con, company_id, "FIGI", comp, "instrument", "openfigi", "unique_common_stock", snapshot_id)
