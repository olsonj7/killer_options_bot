"""Tests for TradierMarketData's retry-on-timeout behaviour.

A single dropped connection to Tradier used to fail the whole scan cycle and
show up as a loop error on the dashboard (e.g. a ReadTimeout after 10s). The
adapter now retries a network-level timeout/connection error a couple of times
before giving up, so a one-off blip recovers within the same cycle instead of
waiting for the run loop's outer retry.
"""

from __future__ import annotations

import requests
import pytest

from killer_options_bot.brokers.tradier import TradierError, TradierMarketData


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = "ok"

    def json(self) -> dict:
        return self._payload


def _make_data(monkeypatch, get_side_effects):
    data = TradierMarketData(api_token="fake-token", retries=2)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        effect = get_side_effects[min(i, len(get_side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(data.session, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # skip real backoff
    return data, calls


def test_retries_on_timeout_then_succeeds(monkeypatch):
    payload = {"quotes": {"quote": {"last": "123.45"}}}
    data, calls = _make_data(
        monkeypatch,
        [requests.Timeout("boom"), requests.ConnectionError("boom"), _FakeResponse(payload)],
    )
    result = data._get("/markets/quotes", {"symbols": "SPY"})
    assert result == payload
    assert calls["n"] == 3


def test_gives_up_after_configured_retries(monkeypatch):
    data, calls = _make_data(
        monkeypatch,
        [requests.Timeout("boom")] * 10,
    )
    with pytest.raises(TradierError):
        data._get("/markets/quotes", {"symbols": "SPY"})
    # Initial attempt + 2 retries = 3 total calls.
    assert calls["n"] == 3


def test_non_timeout_errors_are_not_retried(monkeypatch):
    data, calls = _make_data(
        monkeypatch,
        [_FakeResponse({}, status_code=500)],
    )
    with pytest.raises(TradierError):
        data._get("/markets/quotes", {"symbols": "SPY"})
    assert calls["n"] == 1
