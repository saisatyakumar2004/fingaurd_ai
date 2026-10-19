import pytest

from src.ingestion.gleif import parse_lei_records
from src.ingestion.openfigi import parse_mapping_response
from src.ingestion.sec import parse_tickers, validate_company_facts_shape


def test_sec_tickers_parse_skips_malformed():
    payload = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, "1": {"bad": 1}}
    out = parse_tickers(payload)
    assert [e.ticker for e in out] == ["AAPL"] and out[0].cik_str == 320193
    with pytest.raises(ValueError):
        parse_tickers({"0": {"bad": 1}})


def test_company_facts_shape_validation():
    validate_company_facts_shape({"cik": 5, "facts": {}}, 5)
    with pytest.raises(ValueError):
        validate_company_facts_shape({"cik": 6, "facts": {}}, 5)
    with pytest.raises(ValueError):
        validate_company_facts_shape({"cik": 5}, 5)


def test_gleif_parse():
    payload = {"data": [{"id": "L1", "attributes": {"lei": "L1", "entity": {
        "legalName": {"name": "Test Co"}, "jurisdiction": "US-DE", "status": "ACTIVE",
        "legalAddress": {"country": "US"}}, "registration": {"status": "ISSUED"}}}, {"id": "L2", "attributes": {}}]}
    out = parse_lei_records(payload)
    assert len(out) == 1 and out[0].legal_name == "Test Co" and out[0].registration_status == "ISSUED"
    with pytest.raises(ValueError):
        parse_lei_records({"nope": 1})


def test_openfigi_unmatched_preserved_and_length_checked():
    payload = [{"data": [{"figi": "BBG1", "ticker": "AAA", "securityType2": "Common Stock"}]},
               {"warning": "No identifier found."}, {"error": "bad idType"}]
    out = parse_mapping_response(payload, 3)
    assert [o["status"] for o in out] == ["matched", "unmatched", "error"]
    assert out[1]["rows"] == []
    with pytest.raises(ValueError):
        parse_mapping_response(payload, 2)
