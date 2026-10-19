import pytest

from tests.helpers import build_db


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="run opt-in live tests (internet / API credentials required)")


def pytest_collection_modifyitems(config, items):
    run_live = config.getoption("--run-live")
    for item in items:
        p = str(item.fspath).replace("\\", "/")
        if "/tests/unit/" in p:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in p:
            item.add_marker(pytest.mark.e2e if "test_pipeline" in p or "test_e2e" in p else pytest.mark.integration)
        if "live" in item.keywords and not run_live:
            item.add_marker(pytest.mark.skip(reason="live test: pass --run-live (needs internet/credentials) - NOT RUN"))


@pytest.fixture(scope="module")
def synth():
    """(connection, settings) for a validated synthetic reference database."""
    con, settings = build_db()
    from src.quality.validate import run_validation
    run_validation(con, settings)
    return con, settings
