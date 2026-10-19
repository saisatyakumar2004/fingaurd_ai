"""Rate-limited, retrying, snapshot-caching HTTP JSON fetcher.

Snapshot rules
- Every successful response is written to  <raw_dir>/<source>/<name>/<UTC ts>__<sha12>.json
  plus a sidecar  .meta.json  (url, params, timestamp, sha256, size, status).
- An existing snapshot is NEVER overwritten. `refresh=True` fetches again and, if the content
  differs, adds a NEW snapshot file; the newest file is what later runs read.
- Writes are atomic (temp file + os.replace) so a crash cannot leave a half-written snapshot.
- Failures are appended to <raw_dir>/_failures.jsonl.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from src.common.log import get_logger, log
from src.common.util import canonical_json, sha256_bytes, utcnow

logger = get_logger("http")


class FetchError(Exception):
    pass


class OfflineCacheMiss(FetchError):
    pass


@dataclass
class FetchResult:
    data: Any
    snapshot: dict[str, Any]
    from_cache: bool


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


class HttpFetcher:
    def __init__(
        self,
        source: str,
        raw_dir: Path,
        headers: dict[str, str] | None = None,
        min_interval: float = 0.25,
        timeout: float = 30.0,
        max_retries: int = 4,
        offline: bool = False,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.raw_dir = Path(raw_dir)
        self.headers = dict(headers or {})
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.offline = offline
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self._last_request = float("-inf")

    # ---- cache --------------------------------------------------------
    def _dir(self, name: str) -> Path:
        return self.raw_dir / self.source / _safe(name)

    def _read_latest(self, name: str) -> FetchResult | None:
        d = self._dir(name)
        if not d.exists():
            return None
        metas = sorted(d.glob("*.meta.json"))
        for meta_path in reversed(metas):
            snap = json.loads(meta_path.read_text(encoding="utf-8"))
            body_path = d / snap["file"]
            if body_path.exists():
                try:
                    data = json.loads(body_path.read_bytes())
                except ValueError as e:
                    raise FetchError(f"Corrupt cached snapshot {body_path}: {e}") from e
                return FetchResult(data=data, snapshot=snap, from_cache=True)
        return None

    def _save(self, url: str, name: str, params: Any, body: bytes, status: int) -> FetchResult:
        sha = sha256_bytes(body)
        now = utcnow()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")   # microseconds keep 'latest' ordering stable
        stem = f"{stamp}__{sha[:12]}"
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        body_path = d / f"{stem}.json"
        tmp = d / f"{stem}.json.part"
        tmp.write_bytes(body)
        os.replace(tmp, body_path)
        snap = {
            "snapshot_id": f"{self.source}-{sha[:16]}",
            "source_system": self.source,
            "url": url,
            "request_params": canonical_json(params) if params is not None else None,
            "retrieval_timestamp": now,
            "http_status": status,
            "sha256": sha,
            "byte_size": len(body),
            "raw_path": f"{self.source}/{_safe(name)}/{body_path.name}",
            "status": "ok",
            "file": body_path.name,
        }
        meta_tmp = d / f"{stem}.meta.json.part"
        meta_tmp.write_text(json.dumps(snap, default=str), encoding="utf-8")
        os.replace(meta_tmp, d / f"{stem}.meta.json")
        return FetchResult(data=json.loads(body), snapshot=snap, from_cache=False)

    def _record_failure(self, url: str, name: str, reason: str) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": utcnow().isoformat(), "source": self.source, "name": name, "url": url, "reason": reason}
        with open(self.raw_dir / "_failures.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log(logger, 40, "fetch_failed", **entry)

    # ---- network ------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval - (self.clock() - self._last_request)
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.clock()

    @staticmethod
    def _retry_after(resp: Any) -> float | None:
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(60.0, 1.5 * (2 ** attempt)) * (1.0 + random.uniform(0.0, 0.25))   # exponential backoff + jitter

    def _request(self, url: str, name: str, params: Any, method: str, json_body: Any, headers: dict | None) -> tuple[bytes, int]:
        merged = {**self.headers, **(headers or {})}
        last_err = "unknown"
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(
                    method, url, params=params if method == "GET" else None,
                    json=json_body, headers=merged, timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = f"{type(e).__name__}: {e}"
                log(logger, 30, "transient_error", url=url, attempt=attempt, error=last_err)
                self.sleep(self._backoff(attempt))
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                last_err = f"HTTP {status}"
                delay = self._retry_after(resp)
                delay = delay if delay is not None else self._backoff(attempt)
                log(logger, 30, "retrying", url=url, attempt=attempt, status=status, delay=delay)
                self.sleep(delay)
                continue
            if status != 200:
                self._record_failure(url, name, f"HTTP {status}")
                raise FetchError(f"HTTP {status} for {url}")
            body = resp.content
            try:
                json.loads(body)
            except ValueError:
                self._record_failure(url, name, "invalid JSON body (possible partial download)")
                raise FetchError(f"Invalid JSON body from {url}")
            return body, status
        self._record_failure(url, name, f"giving up after {self.max_retries + 1} attempts: {last_err}")
        raise FetchError(f"Giving up on {url}: {last_err}")

    def fetch(
        self,
        url: str,
        name: str,
        params: Any = None,
        method: str = "GET",
        json_body: Any = None,
        refresh: bool = False,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        if not refresh:
            cached = self._read_latest(name)
            if cached is not None:
                return cached
        if self.offline:
            raise OfflineCacheMiss(f"{self.source}:{name} is not cached and offline mode is on")
        body, status = self._request(url, name, params, method, json_body, headers)
        prior = self._read_latest(name)
        if prior is not None and prior.snapshot["sha256"] == sha256_bytes(body):
            return prior  # unchanged content: do not create a duplicate snapshot
        return self._save(url, name, params if params is not None else json_body, body, status)
