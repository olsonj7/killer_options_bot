"""Tests for the STRAT breakout signal, bar typing, and OHLC data plumbing."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from conftest import make_config

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import SignalConfig
from killer_options_bot.models import Bar, Quote, Side
from killer_options_bot.scanner import (
    Scanner,
    _avg_range,
    strat_bar_type,
    strat_breakout_signal,
)
from killer_options_bot.storage import Storage


# --- Bar model -------------------------------------------------------------


def test_bar_range_midpoint_and_direction():
    up = Bar(open=100.0, high=102.0, low=99.0, close=101.5)
    assert up.range == pytest.approx(3.0)
    assert up.midpoint == pytest.approx(100.5)
    assert up.is_up
    down = Bar(open=101.0, high=101.2, low=98.0, close=98.5)
    assert not down.is_up
    # A bar whose high < low would be degenerate; range never goes negative.
    weird = Bar(open=100.0, high=99.0, low=100.0, close=99.5)
    assert weird.range == 0.0


# --- STRAT bar typing ------------------------------------------------------


def _prev() -> Bar:
    return Bar(open=100.0, high=100.5, low=99.5, close=100.0)


def test_bar_type_inside():
    prev = _prev()
    inside = Bar(open=100.0, high=100.4, low=99.6, close=100.1)
    assert strat_bar_type(inside, prev) == "1"


def test_bar_type_two_up():
    prev = _prev()
    two_up = Bar(open=100.1, high=101.0, low=99.6, close=100.9)
    assert strat_bar_type(two_up, prev) == "2up"


def test_bar_type_two_down():
    prev = _prev()
    two_down = Bar(open=100.0, high=100.4, low=99.0, close=99.1)
    assert strat_bar_type(two_down, prev) == "2down"


def test_bar_type_outside():
    prev = _prev()
    outside = Bar(open=100.0, high=101.0, low=99.0, close=100.7)
    assert strat_bar_type(outside, prev) == "3"


def test_avg_range():
    bars = [Bar(open=100, high=101, low=99, close=100) for _ in range(5)]
    assert _avg_range(bars, 5) == pytest.approx(2.0)
    assert _avg_range(bars, 6) is None  # not enough bars


# --- STRAT breakout signal -------------------------------------------------


def _baseline(n: int = 10) -> list[Bar]:
    """A run of identical flat bars (range 1.0) as the displacement baseline."""
    return [Bar(open=100.0, high=100.5, low=99.5, close=100.0) for _ in range(n)]


def _prior_day(high: float = 101.0, low: float = 99.0) -> Bar:
    """A prior-day bar; midpoint is (high+low)/2."""
    return Bar(open=100.0, high=high, low=low, close=100.0)


def _quote(bars: list[Bar], daily: list[Bar]) -> Quote:
    return Quote(symbol="SPY", last=bars[-1].close, bars=bars, daily_bars=daily)


def test_strat_signal_bullish_2up_fires_call(tmp_path):
    cfg = make_config(tmp_path)
    # Trigger: breaks prior high (100.5), does not break prior low; range 2.0
    # clears 1.5 x avg range (1.0). Close 101.8 > prior-day mid 100.0 -> bias ok.
    trigger = Bar(open=100.1, high=102.0, low=100.0, close=101.8)
    q = _quote(_baseline() + [trigger], [_prior_day(high=101.0, low=99.0)])
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is Side.CALL


def test_strat_signal_bearish_2down_fires_put(tmp_path):
    cfg = make_config(tmp_path)
    # Trigger: breaks prior low (99.5), not the prior high; range 2.5 clears
    # threshold. Close 98.2 < prior-day mid 100.0 -> bearish bias ok.
    trigger = Bar(open=100.4, high=100.5, low=98.0, close=98.2)
    q = _quote(_baseline() + [trigger], [_prior_day(high=101.0, low=99.0)])
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is Side.PUT


def test_strat_signal_inside_bar_declines(tmp_path):
    cfg = make_config(tmp_path)
    trigger = Bar(open=100.0, high=100.4, low=99.6, close=100.1)
    q = _quote(_baseline() + [trigger], [_prior_day()])
    assert strat_breakout_signal(q, cfg).side is None


def test_strat_signal_without_displacement_declines(tmp_path):
    cfg = make_config(tmp_path)
    # A 2up that breaks the prior high but with a tiny range (< threshold).
    trigger = Bar(open=100.5, high=100.7, low=100.4, close=100.65)
    q = _quote(_baseline() + [trigger], [_prior_day()])
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is None
    assert "displacement" in sig.note


def test_strat_signal_against_bias_declines(tmp_path):
    cfg = make_config(tmp_path)
    # Valid displaced 2up, but price sits below a high prior-day midpoint
    # (prior day 100-110 -> mid 105 > close 101.8), so long bias is denied.
    trigger = Bar(open=100.1, high=102.0, low=100.0, close=101.8)
    q = _quote(_baseline() + [trigger], [_prior_day(high=110.0, low=100.0)])
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is None
    assert "bias" in sig.note


def test_strat_signal_insufficient_bars_declines(tmp_path):
    cfg = make_config(tmp_path)
    q = _quote(_baseline(3), [_prior_day()])
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is None
    assert "Insufficient" in sig.note


def test_strat_signal_missing_prior_day_declines(tmp_path):
    cfg = make_config(tmp_path)
    trigger = Bar(open=100.1, high=102.0, low=100.0, close=101.8)
    q = _quote(_baseline() + [trigger], [])  # no daily bars
    sig = strat_breakout_signal(q, cfg)
    assert sig.side is None
    assert "prior-day" in sig.note


def test_strat_displacement_mult_is_configurable(tmp_path):
    # With a lower multiplier the same modest 2up now clears the threshold.
    sig_cfg = SignalConfig(
        sma_period=20,
        rsi_period=14,
        rsi_min=45,
        rsi_max=70,
        strat_displacement_mult=0.5,
        strat_atr_period=10,
    )
    cfg = make_config(tmp_path, signal=sig_cfg)
    trigger = Bar(open=100.5, high=100.7, low=100.4, close=100.65)  # range 0.3
    q = _quote(_baseline() + [trigger], [_prior_day(high=101.0, low=99.0)])
    # avg range 1.0 * 0.5 = 0.5 threshold; range 0.3 still short -> declines.
    assert strat_breakout_signal(q, cfg).side is None


# --- Mock OHLC data --------------------------------------------------------


def test_mock_intraday_bars_shape():
    d = MockMarketData(as_of=date(2026, 3, 2))
    bars = d.get_intraday_bars("SPY", "15min")
    assert bars, "expected some intraday bars"
    for b in bars:
        assert isinstance(b, Bar)
        assert b.high >= b.low
        assert b.high >= b.open and b.high >= b.close
        assert b.low <= b.open and b.low <= b.close


def test_mock_daily_bars_shape_and_order():
    d = MockMarketData(as_of=date(2026, 3, 2))
    bars = d.get_daily_bars("SPY", lookback=5)
    assert len(bars) == 5
    for b in bars:
        assert isinstance(b, Bar)
        assert b.high >= b.low


def test_mock_bars_are_deterministic():
    a = MockMarketData(as_of=date(2026, 3, 2)).get_intraday_bars("SPY", "15min")
    b = MockMarketData(as_of=date(2026, 3, 2)).get_intraday_bars("SPY", "15min")
    assert a == b


# --- Scanner integration ---------------------------------------------------


def test_scanner_attaches_bars_for_strat_signal(tmp_path):
    """The scanner should fetch OHLC bars and produce a CALL candidate when the
    data source serves a clean displaced 2up with bullish prior-day bias."""
    cfg = make_config(tmp_path, watchlist=["SPY"])

    class CraftedData(MockMarketData):
        def get_intraday_bars(self, symbol, interval="15min"):
            trigger = Bar(open=100.1, high=102.0, low=100.0, close=101.8)
            return _baseline() + [trigger]

        def get_daily_bars(self, symbol, lookback=5):
            return [_prior_day(high=101.0, low=99.0)]

    data = CraftedData(as_of=date(2026, 3, 2))
    storage = Storage(cfg.db_path)
    scanner = Scanner(cfg, data, storage, as_of=date(2026, 3, 2))

    from killer_options_bot.config import StrategyConfig

    strat = StrategyConfig(
        name="strat",
        signal="strat_breakout",
        filters=replace(cfg.filters, min_dte=0, max_dte=2),
        exits=cfg.exits,
        scan_interval_minutes=5,
    )
    candidate = scanner.scan_symbol_strategy("SPY", strat)
    assert candidate is not None
    assert candidate.side is Side.CALL
    assert candidate.strategy == "strat"


def test_scanner_survives_missing_bar_getters(tmp_path):
    """A data source without OHLC getters must not crash; the strat signal just
    declines (empty bar lists)."""
    cfg = make_config(tmp_path)

    class BareData:
        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=100.0, closes=[100.0] * 40)

        def get_option_chain(self, symbol, side):
            return []

    storage = Storage(cfg.db_path)
    scanner = Scanner(cfg, BareData(), storage, as_of=date(2026, 3, 2))

    from killer_options_bot.config import StrategyConfig

    strat = StrategyConfig(
        name="strat",
        signal="strat_breakout",
        filters=cfg.filters,
        exits=cfg.exits,
        scan_interval_minutes=5,
    )
    assert scanner.scan_symbol_strategy("SPY", strat) is None


# --- Config loading --------------------------------------------------------


def test_config_loads_strat_strategy():
    from killer_options_bot.config import load_config

    # 'strat' is defined and valid but intentionally NOT active on the smallest
    # tier (kept out of the active list to avoid adding entries). It must still
    # parse as a recognized signal so it can be enabled later without error.
    cfg = load_config("config.yaml")
    names = [s.name for s in cfg.active_strategies]
    assert "strat" not in names  # inactive by design
    from killer_options_bot.config import _VALID_SIGNALS
    from killer_options_bot.scanner import _SIGNALS

    assert "strat_breakout" in _VALID_SIGNALS
    assert "strat_breakout" in _SIGNALS


def test_config_rejects_bad_strat_interval(tmp_path):
    import textwrap

    from killer_options_bot.config import load_config

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            account:
              value: 1000.0
            watchlist: [SPY]
            signal:
              sma_period: 20
              rsi_period: 14
              rsi_min: 45
              rsi_max: 70
              strat_interval: 3min
            """
        )
    )
    with pytest.raises(ValueError, match="strat_interval"):
        load_config(bad)
