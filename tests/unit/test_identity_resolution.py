"""Identity-state behaviour with deterministic, clearly SYNTHETIC fixtures (not live data)."""
import copy

import pytest
import requests

from src.common.db import fetch_dicts
from src.identity.resolver import corroborate, resolve_entity
from src.ingestion.load import store_figi_result
from src.ingestion.openfigi import FigiRow
from src.ingestion.run import _ingest_gleif
from tests.helpers import build_db, synthetic_universe


class Cand(dict):
    def __getattr__(self, k):
        return self.get(k)


def cand(lei, name, jur="US-DE", city="Wilmington", hq_city="Houston", hq_region="US-TX", status="ACTIVE"):
    return Cand(lei=lei, legal_name=name, jurisdiction=jur, entity_status=status, legal_city=city, hq_city=hq_city, hq_region=hq_region)


PROFILE = {"name": "ACME CORP", "state_of_incorporation": "DE", "business_city": "Houston", "business_state": "TX"}


def test_correct_mapping_verified_only_with_independent_sec_evidence():
    r = resolve_entity("Acme Corp", [cand("L1", "Acme Corporation")], sec_profile=PROFILE)
    assert r.status == "VERIFIED" and r.selected_lei == "L1" and "SEC" in r.rationale
    assert resolve_entity("Acme Corp", [cand("L1", "Acme Corporation")]).status == "CANDIDATE_UNIQUE"   # name alone is not enough


def test_incorrect_mapping_contradicting_sec_profile_is_not_selected():
    wrong_state = cand("L1", "Acme Corporation", jur="US-NV")
    r = resolve_entity("Acme Corp", [wrong_state], sec_profile=PROFILE)
    assert r.status == "AMBIGUOUS" and r.selected_lei is None and "contradicts" in r.rationale
    r2 = resolve_entity("Acme Corp", [cand("L1", "Acme Corporation")], expected_lei="L-OTHER")
    assert r2.status == "AMBIGUOUS" and r2.selected_lei is None


def test_similar_company_names_are_never_auto_selected():
    near = [cand("L1", "Delta Air Lines Holdings"), cand("L2", "Delta Apparel")]
    r = resolve_entity("Delta Air Lines", near, sec_profile={**PROFILE, "business_city": "Atlanta"})
    assert r.status == "AMBIGUOUS" and r.selected_lei is None


def test_same_group_different_legal_entities_disambiguated_by_address_not_name():
    parent = cand("L-PARENT", "Acme Corp", city="Wilmington", hq_city="Houston")
    sub = cand("L-SUB", "Acme Corp", jur="US-TX", city="Dallas", hq_city="Dallas", hq_region="US-TX")
    r = resolve_entity("Acme Corp", [parent, sub], sec_profile=PROFILE)
    assert r.status == "VERIFIED" and r.selected_lei == "L-PARENT"
    twin = copy.deepcopy(parent)
    twin["lei"] = "L-TWIN"
    assert resolve_entity("Acme Corp", [parent, twin], sec_profile=PROFILE).status == "AMBIGUOUS"     # indistinguishable entities
    assert resolve_entity("Acme Corp", [parent, sub]).status == "AMBIGUOUS"                            # no independent evidence


def test_unmatched_and_inactive():
    assert resolve_entity("Acme Corp", []).status == "UNMATCHED"
    assert resolve_entity("Acme Corp", [cand("L9", "Unrelated Mining")], sec_profile=PROFILE).status == "UNMATCHED"
    assert resolve_entity("Acme Corp", [cand("L1", "Acme Corp", status="INACTIVE")], sec_profile=PROFILE).status == "AMBIGUOUS"


def test_corroboration_never_ok_on_missing_data():
    c = corroborate(cand("L", "X", jur=None, city=None, hq_city=None), PROFILE)
    assert c["ok"] is False and c["contradicts"] is False
    assert corroborate(cand("L", "X"), {"name": "X"})["ok"] is False


def test_multiple_share_classes_register_no_figi_and_all_rows_are_kept():
    con, _ = build_db(n=1, with_identity=False)
    cid = "CIK0009000001"
    rows = [FigiRow(figi="BBG-A", compositeFIGI="BBGC-A", shareClassFIGI="BBGS-A", securityType2="Common Stock", ticker="TSTA", exchCode="US"),
            FigiRow(figi="BBG-B", compositeFIGI="BBGC-B", shareClassFIGI="BBGS-B", securityType2="Common Stock", ticker="TSTA", exchCode="US")]
    store_figi_result(con, cid, {"idType": "TICKER", "idValue": "TSTA"}, {"status": "matched", "rows": rows, "message": ""}, "s")
    assert len(fetch_dicts(con, "SELECT * FROM instrument_mappings WHERE company_id = ?", [cid])) == 2
    assert not fetch_dicts(con, "SELECT * FROM company_identifiers WHERE company_id = ? AND id_type LIKE '%FIGI%'", [cid])


def test_unmatched_instrument_preserved_as_unmatched():
    con, _ = build_db(n=1, with_identity=False)
    store_figi_result(con, "CIK0009000001", {"idType": "TICKER", "idValue": "ZZZZ"},
                      {"status": "unmatched", "rows": [], "message": "No identifier found."}, "s")
    r = fetch_dicts(con, "SELECT status, figi, message FROM instrument_mappings")[0]
    assert r["status"] == "unmatched" and r["figi"] is None


class DownSession:
    """Every request fails like an unreachable service / rate limit that never clears."""
    def __init__(self, exc=None, status=None):
        self.exc, self.status, self.calls = exc, status, 0

    def request(self, *a, **k):
        self.calls += 1
        if self.exc:
            raise self.exc
        return type("R", (), {"status_code": self.status, "headers": {"Retry-After": "0"}, "content": b""})()


@pytest.mark.parametrize("session", [DownSession(exc=requests.ConnectionError("down")), DownSession(status=429), DownSession(status=503)])
def test_api_failure_or_rate_limit_yields_source_unavailable_never_a_fabricated_mapping(tmp_path, monkeypatch, session):
    monkeypatch.setenv("SEC_USER_AGENT", "FinGuardAI team@project.test")
    monkeypatch.setattr("time.sleep", lambda s: None)
    con, settings = build_db(n=1, with_identity=False)
    settings = copy.deepcopy(settings)
    for k in ("gleif", "sec"):
        settings[k]["max_retries"] = 1
        settings[k]["min_request_interval_seconds"] = 0
    summary = {"gleif": {}, "failures": []}
    _ingest_gleif(con, settings, synthetic_universe(1), False, False, session, tmp_path, summary)
    row = fetch_dicts(con, "SELECT status, selected_lei FROM identity_resolutions")[0]
    assert row["status"] == "SOURCE_UNAVAILABLE" and row["selected_lei"] is None
    assert not fetch_dicts(con, "SELECT * FROM company_identifiers WHERE id_type = 'LEI'")
    assert session.calls >= 2 and summary["failures"]
