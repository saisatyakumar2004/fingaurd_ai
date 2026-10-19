import pytest

from src.common.http import HttpFetcher
from src.ingestion.sec import SecClient, make_sec_fetcher, read_sec_user_agent
from src.common.config import load_settings


class Resp:
    status_code, headers, content = 200, {}, b'{"cik": 1, "facts": {}}'


class Spy:
    def __init__(self):
        self.headers = []

    def request(self, method, url, **kw):
        self.headers.append(kw["headers"])
        return Resp()


@pytest.mark.parametrize("value", [None, "", "FinGuardAI", "FinGuardAI your-project-email@example.com", "x@example.com"])
def test_missing_or_placeholder_user_agent_is_rejected_with_actionable_message(value):
    env = {} if value is None else {"SEC_USER_AGENT": value}
    with pytest.raises(RuntimeError) as e:
        read_sec_user_agent(env)
    assert ".env" in str(e.value) and "SEC_USER_AGENT" in str(e.value)


def test_valid_user_agent_accepted():
    assert read_sec_user_agent({"SEC_USER_AGENT": "FinGuardAI team@project.test"}) == "FinGuardAI team@project.test"


def test_every_sec_request_carries_configured_user_agent_and_none_sent_when_misconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "FinGuardAI team@project.test")
    spy = Spy()
    f = make_sec_fetcher(load_settings(), tmp_path, offline=False, session=spy)
    f.min_interval = 0
    c = SecClient(f)
    c.tickers()
    c.company_facts(1)
    assert len(spy.headers) == 2 and all(h["User-Agent"] == "FinGuardAI team@project.test" for h in spy.headers)

    monkeypatch.delenv("SEC_USER_AGENT")
    spy2 = Spy()
    with pytest.raises(RuntimeError):
        make_sec_fetcher(load_settings(), tmp_path, offline=False, session=spy2)
    assert spy2.headers == []


def test_offline_mode_needs_no_user_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert isinstance(make_sec_fetcher(load_settings(), tmp_path, offline=True), HttpFetcher)


def test_ingestion_cli_fails_cleanly_without_config_or_cache(tmp_path, monkeypatch, capsys):
    from src.ingestion.run import main
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("FINGUARD_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr("src.common.config.load_env", lambda: None)
    monkeypatch.setattr("src.ingestion.run.load_env", lambda: None)
    assert main(["--db", str(tmp_path / "x.duckdb"), "--tickers", "AAPL"]) == 2          # missing configuration
    assert "SEC_USER_AGENT" in capsys.readouterr().err
    assert main(["--db", str(tmp_path / "y.duckdb"), "--tickers", "AAPL", "--offline"]) == 3  # nothing cached
    assert "not cached" in capsys.readouterr().err
