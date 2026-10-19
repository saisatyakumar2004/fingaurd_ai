import json

import pytest
import requests

from src.common.http import FetchError, HttpFetcher, OfflineCacheMiss


class Resp:
    def __init__(self, status=200, body=b'{"ok": 1}', headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make(tmp_path, script, **kw):
    sleeps = []
    f = HttpFetcher("src", tmp_path, session=FakeSession(script), sleep=sleeps.append, min_interval=0, **kw)
    return f, sleeps


def test_cache_reuse_no_second_request(tmp_path):
    f, _ = make(tmp_path, [Resp()])
    a = f.fetch("http://x", "n")
    b = f.fetch("http://x", "n")
    assert not a.from_cache and b.from_cache and len(f.session.calls) == 1
    assert a.snapshot["snapshot_id"] == b.snapshot["snapshot_id"] and a.snapshot["sha256"]


def test_retry_after_honoured_on_429(tmp_path):
    f, sleeps = make(tmp_path, [Resp(429, headers={"Retry-After": "7"}), Resp()])
    assert f.fetch("http://x", "n").data == {"ok": 1}
    assert 7.0 in sleeps


def test_transient_error_then_success(tmp_path):
    f, sleeps = make(tmp_path, [requests.Timeout("t"), Resp(503), Resp()])
    assert f.fetch("http://x", "n").data == {"ok": 1}
    assert len(sleeps) == 2


def test_gives_up_and_records_failure(tmp_path):
    f, _ = make(tmp_path, [Resp(500)] * 3, max_retries=2)
    with pytest.raises(FetchError):
        f.fetch("http://x", "n")
    assert (tmp_path / "_failures.jsonl").exists()
    assert not list((tmp_path / "src").glob("**/*.json")) if (tmp_path / "src").exists() else True


def test_invalid_json_is_partial_download(tmp_path):
    f, _ = make(tmp_path, [Resp(body=b'{"trunc')])
    with pytest.raises(FetchError):
        f.fetch("http://x", "n")


def test_refresh_never_overwrites_existing_snapshot(tmp_path):
    f, _ = make(tmp_path, [Resp(body=b'{"v": 1}'), Resp(body=b'{"v": 2}')])
    first = f.fetch("http://x", "n")
    second = f.fetch("http://x", "n", refresh=True)
    files = sorted(p.name for p in (tmp_path / "src" / "n").glob("*.json") if not p.name.endswith(".meta.json"))
    assert len(files) == 2 and first.snapshot["sha256"] != second.snapshot["sha256"]
    assert json.loads((tmp_path / "src" / "n" / first.snapshot["file"]).read_text()) == {"v": 1}


def test_refresh_with_identical_content_creates_no_duplicate(tmp_path):
    f, _ = make(tmp_path, [Resp(), Resp()])
    f.fetch("http://x", "n")
    f.fetch("http://x", "n", refresh=True)
    assert len([p for p in (tmp_path / "src" / "n").glob("*.json") if not p.name.endswith(".meta.json")]) == 1


def test_offline_miss_and_hit(tmp_path):
    f, _ = make(tmp_path, [Resp()])
    f.fetch("http://x", "n")
    off = HttpFetcher("src", tmp_path, offline=True)
    assert off.fetch("http://x", "n").from_cache
    with pytest.raises(OfflineCacheMiss):
        off.fetch("http://x", "other")
