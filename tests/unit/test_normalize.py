from datetime import date, datetime

from src.common.config import load_settings
from src.normalization.sec_normalize import classify_period, normalize_company_facts, select_observations
from tests.helpers import synthetic_company_facts

SNAP = {"snapshot_id": "s1", "url": "u", "retrieval_timestamp": datetime(2026, 1, 1)}


def test_classify_period():
    assert classify_period("duration", date(2023, 1, 1), date(2023, 12, 31), "10-K") == "annual"
    assert classify_period("duration", date(2023, 1, 1), date(2023, 3, 31), "10-Q") == "quarterly"
    assert classify_period("duration", date(2023, 1, 1), date(2023, 6, 30), "10-Q") is None   # 6-month YTD excluded
    assert classify_period("duration", None, date(2023, 12, 31), "10-K") is None
    assert classify_period("instant", None, date(2023, 12, 31), "10-K") == "annual"
    assert classify_period("instant", None, date(2023, 3, 31), "10-Q") == "quarterly"
    assert classify_period("instant", date(2023, 1, 1), date(2023, 3, 31), "10-Q") is None


def _rows():
    s = load_settings()
    payload = synthetic_company_facts(9000001, "Alpha Test Corp", 0, restate_fy=2021)
    rows, stats = normalize_company_facts(payload, company_id="CIK0009000001", cik=9000001,
                                          concepts=["revenue", "net_income", "total_assets"], snapshot=SNAP, settings=s)
    return rows, stats


def test_normalize_keeps_provenance_and_units():
    rows, stats = _rows()
    assert stats["kept"] == len(rows) > 0
    r = rows[0]
    assert r["identifier"] == "0009000001" and r["unit"] == "USD" and r["source_system"] == "sec_edgar"
    assert r["accession_number"] and r["filing_date"] and r["snapshot_id"] == "s1"
    assert {x["period_type"] for x in rows} == {"annual", "quarterly"}
    assert all(x["value"] is not None for x in rows)


def test_selection_one_per_period_latest_filing_wins():
    rows, _ = _rows()
    select_observations(rows)
    rev21 = [r for r in rows if r["concept"] == "revenue" and r["period_type"] == "annual"
             and r["fiscal_period_end"] == date(2021, 12, 31)]
    assert len(rev21) == 2                                   # original + restated comparative both kept
    sel = [r for r in rev21 if r["is_selected"]]
    assert len(sel) == 1
    assert sel[0]["filing_date"] == max(r["filing_date"] for r in rev21)
    assert sel[0]["value"] != min(r["value"] for r in rev21)  # restated (1.02x) value selected
    groups = {(r["concept"], r["period_type"], r["fiscal_period_start"], r["fiscal_period_end"]) for r in rows}
    assert sum(1 for r in rows if r["is_selected"]) == len(groups)


def test_bad_facts_are_counted_not_zeroed():
    s = load_settings()
    payload = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"end": "2023-12-31", "val": None, "accn": "a", "form": "10-K", "filed": "2024-02-01"},
        {"end": "not-a-date", "val": 5, "accn": "b", "form": "10-K", "filed": "2024-02-01"},
        {"end": "2023-12-31", "val": 5, "accn": "c", "form": "8-K", "filed": "2024-02-01"}]}}}}}
    rows, stats = normalize_company_facts(payload, company_id="C", cik=1, concepts=["total_assets"], snapshot=SNAP, settings=s)
    assert rows == [] and stats["invalid_fact"] == 2 and stats["form_not_allowed"] == 1
