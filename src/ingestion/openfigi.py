"""OpenFIGI v3 mapping adapter (https://www.openfigi.com/api/documentation, POST /v3/mapping).

Request : JSON array of jobs, e.g. [{"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}]
Response: JSON array aligned 1:1 with the jobs; each element is one of
          {"data": [ {figi, name, ticker, exchCode, compositeFIGI, shareClassFIGI,
                      securityType, securityType2, marketSector, securityDescription}, ... ]}
          {"warning": "No identifier found."}     -> unmatched (kept as unmatched)
          {"error": "..."}                        -> error
A single ticker legitimately maps to MANY rows (one per listing/instrument); we never pick one.
Anonymous limits are lower than keyed limits (see config/settings.yaml, verify against the docs).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from src.common.http import FetchResult, HttpFetcher
from src.common.util import canonical_json, stable_id

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


class FigiRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    figi: Optional[str] = None
    name: Optional[str] = None
    ticker: Optional[str] = None
    exchCode: Optional[str] = None
    compositeFIGI: Optional[str] = None
    shareClassFIGI: Optional[str] = None
    securityType: Optional[str] = None
    securityType2: Optional[str] = None
    marketSector: Optional[str] = None


def parse_mapping_response(payload: Any, n_jobs: int) -> list[dict[str, Any]]:
    """Return one dict per job: {'status': matched|unmatched|error, 'rows': [FigiRow], 'message': str}."""
    if not isinstance(payload, list) or len(payload) != n_jobs:
        raise ValueError(f"OpenFIGI response must be a list of length {n_jobs}")
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            out.append({"status": "error", "rows": [], "message": "malformed item"})
        elif "data" in item and isinstance(item["data"], list) and item["data"]:
            out.append({"status": "matched", "rows": [FigiRow.model_validate(r) for r in item["data"]], "message": ""})
        elif "error" in item:
            out.append({"status": "error", "rows": [], "message": str(item["error"])})
        else:
            out.append({"status": "unmatched", "rows": [], "message": str(item.get("warning", "no data"))})
    return out


def make_openfigi_fetcher(settings: dict, raw_dir: Path, offline: bool, session: Any | None = None) -> tuple[HttpFetcher, int]:
    cfg = settings["openfigi"]
    key = os.environ.get("OPENFIGI_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    interval = float(cfg["min_interval_with_key" if key else "min_interval_no_key"])
    batch = int(cfg["batch_size_with_key" if key else "batch_size_no_key"])
    f = HttpFetcher("openfigi", raw_dir, headers=headers, min_interval=interval,
                    timeout=float(cfg["timeout_seconds"]), max_retries=int(cfg["max_retries"]),
                    offline=offline, session=session)
    return f, batch


class OpenFigiClient:
    def __init__(self, fetcher: HttpFetcher, batch_size: int) -> None:
        self.fetcher = fetcher
        self.batch_size = batch_size

    def map_jobs(self, jobs: list[dict[str, Any]], refresh: bool = False) -> list[tuple[list[dict], FetchResult]]:
        out = []
        for i in range(0, len(jobs), self.batch_size):
            chunk = jobs[i:i + self.batch_size]
            res = self.fetcher.fetch(
                OPENFIGI_URL, f"map_{stable_id(canonical_json(chunk))}",
                method="POST", json_body=chunk, refresh=refresh,
            )
            out.append((chunk, res))
        return out
