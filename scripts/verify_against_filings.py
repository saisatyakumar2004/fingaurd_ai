"""Independent check of stored observations against the ACTUAL SEC filing documents.

    python scripts/verify_against_filings.py [--db data/reference.duckdb] [--out data/normalized/filing_verification.json]

For each sampled (ticker, concept, fiscal period end) it
  1. reads the stored observation(s) (original report and currently selected report),
  2. downloads the filing's primary HTML document named by the accession number (NOT the XBRL API),
  3. searches the filing text for the value in the filing's own scale (units / thousands / millions) in a context
     that contains a concept-specific line label (e.g. "Total assets"), and records the matched snippet.
Result per row: FOUND_IN_FILING (value + label context located), NOT_FOUND (unresolved - never silently passed).
This is text-presence evidence: it proves the filing prints that number next to that label, which is independent of the
companyfacts API, but it does not by itself prove which period COLUMN the number sits in (the snippet is recorded for review).
Requires SEC_USER_AGENT; honours the SEC rate limit (>= 0.3 s between requests); documents are cached under data/raw/filings.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402
import requests  # noqa: E402

from src.common.config import load_env, load_settings, path_for  # noqa: E402
from src.common.util import sha256_bytes  # noqa: E402
from src.ingestion.sec import read_sec_user_agent  # noqa: E402

SAMPLE = [  # chosen to include non-calendar fiscal years (AAPL Sep, MSFT Jun, WMT Jan, COST Aug/Sep, NKE May)
    ("AAPL", "revenue", "2023-09-30"), ("AAPL", "total_assets", "2024-09-28"),
    ("MSFT", "net_income", "2024-06-30"), ("MSFT", "revenue", "2023-06-30"),
    ("WMT", "revenue", "2025-01-31"), ("WMT", "net_income", "2024-01-31"),
    ("COST", "revenue", "2023-09-03"), ("NKE", "net_income", "2024-05-31"), ("NKE", "total_assets", "2023-05-31"),
    ("KO", "revenue", "2022-12-31"), ("JPM", "net_income", "2023-12-31"), ("JNJ", "revenue", "2023-12-31"),
]
LABELS = {
    "revenue": r"(total\s+)?(net\s+)?(sales|revenues?)|net sales",
    "net_income": r"net\s+(income|earnings|\(loss\)|loss)",
    "total_assets": r"total\s+assets",
}
UA_SLEEP = 0.3


def get(session, url, headers, retries=3):
    for i in range(retries):
        time.sleep(UA_SLEEP)
        r = session.get(url, headers=headers, timeout=(10, 60))
        if r.status_code == 200:
            return r.content
        if r.status_code in (429, 503):
            time.sleep(float(r.headers.get("Retry-After", 2 ** (i + 1))))
            continue
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    raise RuntimeError(f"gave up on {url}")


def primary_document(session, headers, cik: int, accn: str, cache: Path) -> tuple[str, str, bytes]:
    nodash = accn.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/index.json"
    idx = json.loads(get(session, idx_url, headers))
    items = [i for i in idx["directory"]["item"] if i["name"].lower().endswith((".htm", ".html"))
             and not re.match(r"(r\d+\.htm|ex[-_]?\d|.*-index)", i["name"].lower())]
    if not items:
        raise RuntimeError("no primary html document in filing index")
    items.sort(key=lambda i: int(i.get("size") or 0), reverse=True)
    name = items[0]["name"]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{name}"
    path = cache / f"{nodash}_{name}"
    if path.exists():
        return url, name, path.read_bytes()
    body = get(session, url, headers)
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return url, name, body


def clean(body: bytes) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", body.decode("utf-8", "ignore"))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def find_value(text: str, concept: str, value: float) -> dict:
    label_re = re.compile(LABELS[concept], re.I)
    forms = []
    for scale, tag in ((1, "units"), (1e3, "thousands"), (1e6, "millions")):
        x = value / scale
        if abs(x - round(x)) < 1e-6:
            forms.append((f"{abs(round(x)):,}", tag))
    for num, tag in forms:
        for m in re.finditer(rf"(?<![\d,.]){re.escape(num)}(?![\d,]|\.\d)", text):
            ctx = text[max(0, m.start() - 260): m.end() + 40]
            if label_re.search(ctx):
                return {"result": "FOUND_IN_FILING", "printed_as": num, "filing_scale": tag, "snippet": ctx[-300:]}
    return {"result": "NOT_FOUND", "tried": [n for n, _ in forms]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    load_env()
    settings = load_settings()
    ua = read_sec_user_agent()
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    con = duckdb.connect(str(args.db or path_for(settings, "db")), read_only=True)
    cache = path_for(settings, "raw") / "filings"
    session = requests.Session()
    out = []
    for ticker, concept, end in SAMPLE:
        rows = con.execute(
            "SELECT o.observation_id, o.value, o.unit, o.xbrl_tag, o.accession_number, o.filing_date, o.form, o.is_selected, o.source_url, "
            "o.fiscal_period_start, c.cik, c.name FROM financial_observations o JOIN companies c USING (company_id) "
            "WHERE c.ticker = ? AND o.concept = ? AND o.period_type = 'annual' AND o.fiscal_period_end = CAST(? AS DATE) "
            "ORDER BY o.filing_date, o.accession_number", [ticker, concept, end]).fetchall()
        if not rows:
            out.append({"ticker": ticker, "concept": concept, "period_end": end, "result": "NOT_IN_DATABASE"})
            continue
        targets = {rows[0][0]: ("original_report", rows[0])}
        sel = next(r for r in rows if r[7])
        targets.setdefault(sel[0], ("selected_report", sel))
        for oid, (kind, r) in targets.items():
            rec = {"ticker": ticker, "company": r[11], "cik": r[10], "concept": concept, "period_start": str(r[9]), "period_end": end,
                   "kind": kind, "value": r[1], "unit": r[2], "xbrl_tag": r[3], "accession": r[4], "filing_date": str(r[5]),
                   "form": r[6], "api_url": r[8]}
            try:
                url, name, body = primary_document(session, headers, r[10], r[4], cache)
                text = clean(body)
                rec.update(filing_url=url, filing_doc=name, filing_sha256=sha256_bytes(body), **find_value(text, concept, abs(r[1])))
            except Exception as e:
                rec.update(result="ERROR", error=f"{type(e).__name__}: {e}")
            out.append(rec)
            print(f"{ticker:5s} {concept:12s} {end} {kind:15s} {r[1]:>20,.0f} {r[4]}  -> {rec['result']}")
    target = Path(args.out or path_for(settings, "normalized") / "filing_verification.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    ok = sum(1 for r in out if r["result"] == "FOUND_IN_FILING")
    print(f"{ok}/{len(out)} rows found in filing text; details in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
