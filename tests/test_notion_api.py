"""The Notion transport layer: throttle, retry, and what it refuses to swallow.

One member's digest is ~20 pages and never came near Notion's limits. Ten members
in one run is 200+ requests against a documented average of three per second, and
the previous code raised on the first 429 — losing every page not yet written.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

import paper_digest.notion_api as api
from tests.notion_fake import FakeResponse


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Retry backoff is real seconds; tests assert on the shape, not the wall."""
    slept = []
    monkeypatch.setattr(api.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(api, "MIN_REQUEST_INTERVAL", 0.0)
    return slept


def _responses(monkeypatch, *responses):
    """Serve the given responses in order, and record how many calls happened."""
    calls = {"n": 0}

    def _get(url, **kwargs):
        calls["n"] += 1
        return responses[min(calls["n"] - 1, len(responses) - 1)]

    monkeypatch.setattr(api.requests, "get", _get)
    return calls


class TestRetry:
    def test_a_429_is_retried_and_then_succeeds(self, monkeypatch, _no_waiting):
        calls = _responses(monkeypatch,
                           FakeResponse(429), FakeResponse(200, {"ok": True}))

        resp = api.request("get", "/pages/x", "tok", what="probe")

        assert resp.status_code == 200
        assert calls["n"] == 2
        assert _no_waiting, "a 429 must actually wait before retrying"

    def test_a_500_is_retried(self, monkeypatch):
        calls = _responses(monkeypatch,
                           FakeResponse(502), FakeResponse(200, {"ok": True}))
        assert api.request("get", "/pages/x", "tok", what="probe").status_code == 200
        assert calls["n"] == 2

    def test_retry_after_is_honoured_over_the_backoff(self, monkeypatch,
                                                     _no_waiting):
        throttled = FakeResponse(429)
        throttled.headers = {"Retry-After": "7"}
        _responses(monkeypatch, throttled, FakeResponse(200, {}))

        api.request("get", "/pages/x", "tok", what="probe")
        assert 7 in _no_waiting, "Notion's own instruction beats our guess"

    def test_a_nonsense_retry_after_falls_back_to_the_backoff(self, monkeypatch,
                                                             _no_waiting):
        throttled = FakeResponse(429)
        throttled.headers = {"Retry-After": "soon"}
        _responses(monkeypatch, throttled, FakeResponse(200, {}))

        api.request("get", "/pages/x", "tok", what="probe")
        assert _no_waiting == [api.BACKOFF_BASE]

    def test_a_dropped_connection_is_retried(self, monkeypatch):
        calls = {"n": 0}

        def _get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("reset by peer")
            return FakeResponse(200, {"ok": True})

        monkeypatch.setattr(api.requests, "get", _get)
        assert api.request("get", "/pages/x", "tok", what="probe").status_code == 200

    def test_exhausted_retries_on_a_transport_error_raise(self, monkeypatch):
        def _get(url, **kwargs):
            raise requests.ConnectionError("still down")

        monkeypatch.setattr(api.requests, "get", _get)
        with pytest.raises(RuntimeError, match="after 5 attempts"):
            api.request("get", "/pages/x", "tok", what="probe")

    def test_exhausted_retries_on_a_429_return_the_response(self, monkeypatch):
        """So `check` reports it in the same shape as every other Notion failure."""
        _responses(monkeypatch, FakeResponse(429))

        resp = api.request("get", "/pages/x", "tok", what="probe")
        assert resp.status_code == 429
        with pytest.raises(RuntimeError, match="Notion probe failed"):
            api.check(resp, "probe")


class TestNoRetry:
    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_a_client_error_is_returned_immediately(self, monkeypatch, status):
        """Retrying a bad token or an unshared page can only waste time."""
        calls = _responses(monkeypatch, FakeResponse(status, {"message": "nope"}))

        resp = api.request("get", "/pages/x", "tok", what="probe")
        assert resp.status_code == status
        assert calls["n"] == 1


class TestThrottle:
    """The gap between requests is what keeps a 300-page run under the rate.

    Asserted on the *requested* durations rather than on elapsed wall time: a
    test that sleeps to prove sleeping is slow and, worse, flaky on a loaded
    machine.
    """

    def test_each_request_after_the_first_waits_for_the_gap(self, monkeypatch,
                                                           _no_waiting):
        gap = 0.34
        monkeypatch.setattr(api, "MIN_REQUEST_INTERVAL", gap)
        monkeypatch.setattr(api.requests, "get",
                            lambda url, **kw: FakeResponse(200, {}))
        api._last_request_at = 0.0   # "long ago", so the first call goes straight out

        for _ in range(3):
            api.request("get", "/pages/x", "tok", what="probe")

        waits = [s for s in _no_waiting if s > 0]
        assert len(waits) == 2, f"expected two waits between three calls, got {waits}"
        assert all(w <= gap for w in waits)

    def test_a_request_after_a_long_pause_does_not_wait(self, monkeypatch,
                                                       _no_waiting):
        monkeypatch.setattr(api, "MIN_REQUEST_INTERVAL", 0.34)
        monkeypatch.setattr(api.requests, "get",
                            lambda url, **kw: FakeResponse(200, {}))
        api._last_request_at = 0.0

        api.request("get", "/pages/x", "tok", what="probe")
        assert [s for s in _no_waiting if s > 0] == []


class TestCheck:
    def test_notions_own_message_is_what_surfaces(self):
        resp = FakeResponse(403, {
            "message": "Make sure the relevant pages are shared with your integration"
        })
        with pytest.raises(RuntimeError, match="shared with your integration"):
            api.check(resp, "page lookup")

    def test_a_body_that_is_not_json_still_produces_a_message(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        resp.json.side_effect = ValueError("no json")
        resp.text = "upstream exploded"

        with pytest.raises(RuntimeError, match="upstream exploded"):
            api.check(resp, "database query")

    def test_a_success_passes_through(self):
        api.check(FakeResponse(200, {}), "anything")


class TestState:
    def test_a_missing_file_reads_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert api.load_state() == {}

    def test_a_corrupt_file_reads_as_empty_rather_than_crashing(self, tmp_path,
                                                               monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
        assert api.load_state() == {}

    def test_a_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        api.save_state({"news_database_id": "db-1", "members": {"a": {}}})
        assert api.load_state()["news_database_id"] == "db-1"
