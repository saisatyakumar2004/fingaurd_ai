"""GLEIF LEI adapter (https://www.gleif.org/en/lei-data/gleif-api, base https://api.gleif.org/api/v1).

Field mapping follows the JSON:API shape of GET /lei-records:
  data[].id / data[].attributes.lei
  data[].attributes.entity.legalName.name
  data[].attributes.entity.jurisdiction
  data[].attributes.entity.status            (ACTIVE / INACTIVE / ...)
  data[].attributes.entity.legalAddress.country
  data[].attributes.registration.status      (ISSUED / LAPSED / ...)
Verify these against the live API on first use (see DATA_SOURCES.md); unknown/missing fields
are tolerated and recorded as None rather than invented.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from src.common.http import FetchResult, HttpFetcher
from src.common.util import stable_id

GLEIF_BASE = "https://api.gleif.org/api/v1"


class GleifCandidate(BaseModel):
    lei: str
    legal_name: str
    jurisdiction: Optional[str] = None
    entity_status: Optional[str] = None
    registration_status: Optional[str] = None
    legal_country: Optional[str] = None
    legal_city: Optional[str] = None
    hq_city: Optional[str] = None
    hq_region: Optional[str] = None


def parse_lei_records(payload: Any) -> list[GleifCandidate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("GLEIF payload missing 'data' list")
    out: list[GleifCandidate] = []
    for rec in payload["data"]:
        attrs = rec.get("attributes") or {}
        ent = attrs.get("entity") or {}
        lei = attrs.get("lei") or rec.get("id")
        name = (ent.get("legalName") or {}).get("name")
        if not lei or not name:
            continue
        out.append(GleifCandidate(
            lei=lei,
            legal_name=name,
            jurisdiction=ent.get("jurisdiction"),
            entity_status=ent.get("status"),
            registration_status=(attrs.get("registration") or {}).get("status"),
            legal_country=(ent.get("legalAddress") or {}).get("country"),
            legal_city=(ent.get("legalAddress") or {}).get("city"),
            hq_city=(ent.get("headquartersAddress") or {}).get("city"),
            hq_region=(ent.get("headquartersAddress") or {}).get("region"),
        ))
    return out


def make_gleif_fetcher(settings: dict, raw_dir: Path, offline: bool, session: Any | None = None) -> HttpFetcher:
    cfg = settings["gleif"]
    return HttpFetcher(
        "gleif", raw_dir, headers={"Accept": "application/vnd.api+json"},
        min_interval=float(cfg["min_request_interval_seconds"]),
        timeout=float(cfg["timeout_seconds"]), max_retries=int(cfg["max_retries"]),
        offline=offline, session=session,
    )


class GleifClient:
    def __init__(self, fetcher: HttpFetcher, page_size: int = 10) -> None:
        self.fetcher = fetcher
        self.page_size = page_size

    def search(self, name: str, mode: str = "legal_name", refresh: bool = False) -> FetchResult:
        key = "filter[entity.legalName]" if mode == "legal_name" else "filter[fulltext]"
        params = {key: name, "page[size]": self.page_size}
        return self.fetcher.fetch(
            f"{GLEIF_BASE}/lei-records", f"{mode}_{stable_id(name)}", params=params, refresh=refresh
        )
