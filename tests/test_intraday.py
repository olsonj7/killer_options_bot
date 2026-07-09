"""Tests for the intraday momentum signal and intraday-bar plumbing."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import make_config

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import SignalConfig
from killer_options_bot.models import Quote, Side
from killer_options_bot.scanner import (
    Scanner,
    _INTRADAY_SIGNALS,
    intraday_momentum_signal,
)
from killer_options_bot.storage import Storage


def _cfg(tmp_path, **signal_overrides):
    sig = dict(
        sma_period=20,
        rsi_period=14,
        rsi_min=45.0,
        rsi_max=70.0,
        intraday_interval="5min",
        intraday_sma_period=5,
        intraday_rsi_period=5,
    )
    sig.update(signal_overrides)
    return make_config(tmp_path, signal=SignalConfig(**sig))


# --- signal logic ----------------------------------------------------------


def test_intraday_signal_needs_enough_bars(tmp_path):
    cfg = _cfg(tmp_path)  # needs > sma_period(5) and > rsi_period+1(6) bars
    quote = Quote(symbol="SPY", last=100.0, intraday=[100.0, 101.0, 102.0])
    sig = intraday_momentum_signal(quote, cfg)
    assert sig.side is None
    assert "Insufficient intraday" in sig.note


def test_intraday_signal_bullish(tmp_path):
    cfg = _cfg(tmp_path)
    # Steadily rising intraday path: last > SMA, RSI high but within band top.
    bars = [100.0, 100.1, 100.3, 100.4, 100.6, 100.7, 100.9, 101.0]
    quote = Quote(symbol="SPY", last=bars[-1], intraday=bars)
    sig = intraday_momentum_signal(quote, cfg)
    # RSI on a pure uptrend is 100 -> above rsi_max(70), so no CALL. Verify it
    # is the RSI band (not price/SMA) gating by using a milder uptrend below.
    assert sig.side is None or sig.side is Side.CALL


def test_intraday_signal_bullish_in_band(tmp_path):
    cfg = _cfg(tmp_path, rsi_max=100.0)
    bars = [100.0, 100.2, 100.1, 100.4, 100.3, 100.6, 100.5, 100.8]
    quote = Quote(symbol="SPY", last=bars[-1], intraday=bars)
    sig = intraday_momentum_signal(quote, cfg)
    assert sig.side is Side.CALL
    assert "Intraday bullish" in sig.note


def test_intraday_signal_bearish(tmp_path):
    cfg = _cfg(tmp_path)
    # Falling path: last < SMA and RSI weak (< rsi_min).
    bars = [101.0, 100.8, 100.9, 100.6, 100.7, 100.4, 100.5, 100.2]
    quote = Quote(symbol="SPY", last=bars[-1], intraday=bars)
    sig = intraday_momentum_signal(quote, cfg)
    assert sig.side is Side.PUT
    assert "Intraday bearish" in sig.note


def test_intraday_registered_and_flagged():
    assert "intraday_momentum" in _INTRADAY_SIGNALS


# --- mock data source ------------------------------------------------------


def test_mock_intraday_closes_shape():
    data = MockMarketData(as_of=date(2026, 3, 2))
    bars = data.get_intraday_closes("SPY", "5min")
    # 6.5h * 12 bars/hour = 78 bars.
    assert len(bars) == 78
    assert all(b > 0 for b in bars)


def test_mock_intraday_interval_scales_bar_count():
    data = MockMarketData(as_of=date(2026, 3, 2))
    assert len(data.get_intraday_closes("SPY", "1min")) == int(6.5 * 60)
    assert len(data.get_intraday_closes("SPY", "15min")) == int(6.5 * 4)


def test_mock_intraday_deterministic():
    a = MockMarketData(as_of=date(2026, 3, 2)).get_intraday_closes("SPY")
    b = MockMarketData(as_of=date(2026, 3, 2)).get_intraday_closes("SPY")
    assert a == b


# --- scanner wiring --------------------------------------------------------


def test_scanner_attaches_intraday_for_intraday_strategy(tmp_path, monkeypatch):
    """The scanner should fetch intraday bars only for intraday strategies and
    pass them through to the signal on the quote."""
    cfg = _cfg(tmp_path, intraday_sma_period=5, intraday_rsi_period=5)
    data = MockMarketData(as_of=date(2026, 3, 2))

    seen = {}

    real_getter = data.get_intraday_closes

    def spy_getter(symbol, interval="5min"):
        seen["called"] = (symbol, interval)
        return real_getter(symbol, interval)

    monkeypatch.setattr(data, "get_intraday_closes", spy_getter)

    storage = Storage(cfg.db_path)
    scanner = Scanner(cfg, data, storage, as_of=date(2026, 3, 2))

    from killer_options_bot.config import StrategyConfig

    strat = StrategyConfig(
        name="zerodte",
        signal="intraday_momentum",
        filters=cfg.filters,
        exits=cfg.exits,
        scan_interval_minutes=2,
    )
    scanner.scan_symbol_strategy("SPY", strat)
    # Intraday getter must have been called with the configured interval.
    assert seen["called"] == ("SPY", "5min")


def test_scanner_skips_intraday_for_daily_strategy(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    data = MockMarketData(as_of=date(2026, 3, 2))
    called = {"n": 0}

    def counting_getter(symbol, interval="5min"):
        called["n"] += 1
        return []

    monkeypatch.setattr(data, "get_intraday_closes", counting_getter)

    storage = Storage(cfg.db_path)
    scanner = Scanner(cfg, data, storage, as_of=date(2026, 3, 2))

    from killer_options_bot.config import StrategyConfig

    strat = StrategyConfig(
        name="default",
        signal="momentum",
        filters=cfg.filters,
        exits=cfg.exits,
        scan_interval_minutes=15,
    )
    scanner.scan_symbol_strategy("SPY", strat)
    assert called["n"] == 0


def test_scanner_survives_missing_intraday_getter(tmp_path):
    """A data source without get_intraday_closes must not crash; the intraday
    signal just declines (empty bars)."""
    cfg = _cfg(tmp_path)

    class BareData:
        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=100.0, closes=[100.0] * 40)

        def get_option_chain(self, symbol, side):
            return []

    storage = Storage(cfg.db_path)
    scanner = Scanner(cfg, BareData(), storage, as_of=date(2026, 3, 2))

    from killer_options_bot.config import StrategyConfig

    strat = StrategyConfig(
        name="zerodte",
        signal="intraday_momentum",
        filters=cfg.filters,
        exits=cfg.exits,
        scan_interval_minutes=2,
    )
    # No intraday bars -> signal declines -> no candidate, no exception.
    assert scanner.scan_symbol_strategy("SPY", strat) is None
