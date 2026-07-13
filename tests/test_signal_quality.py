"""Tests for the signal-strength quality gates (buffer + SMA slope).

These make the momentum signals far pickier so the bot stops firing on marginal
"barely past the mean" reads. The RSI band is neutralized (0..100) in the gate
tests so each assertion isolates the price-buffer / slope behaviour.
"""

from __future__ import annotations

from conftest import make_config

from killer_options_bot.config import SignalConfig
from killer_options_bot.models import Quote, Side
from killer_options_bot.scanner import _sma_slope_ok, momentum_signal


def _cfg(tmp_path, **signal_overrides):
    sig = dict(
        sma_period=5,
        rsi_period=5,
        rsi_min=0.0,   # neutralize RSI so only price-buffer / slope gate
        rsi_max=100.0,
    )
    sig.update(signal_overrides)
    return make_config(tmp_path, signal=SignalConfig(**sig))


# --- _sma_slope_ok helper --------------------------------------------------


def test_slope_disabled_always_ok():
    assert _sma_slope_ok([1, 2, 3, 4, 5], period=3, lookback=0, up=True)
    assert _sma_slope_ok([5, 4, 3, 2, 1], period=3, lookback=0, up=False)


def test_slope_rising_detected():
    rising = [100, 101, 102, 103, 104, 105, 106, 107]
    assert _sma_slope_ok(rising, period=5, lookback=3, up=True)
    assert not _sma_slope_ok(rising, period=5, lookback=3, up=False)


def test_slope_falling_detected():
    falling = [107, 106, 105, 104, 103, 102, 101, 100]
    assert _sma_slope_ok(falling, period=5, lookback=3, up=False)
    assert not _sma_slope_ok(falling, period=5, lookback=3, up=True)


def test_slope_fails_closed_without_history():
    # Not enough bars to measure the older SMA -> no trade (fail closed).
    assert not _sma_slope_ok([100, 101, 102], period=5, lookback=3, up=True)


# --- trend buffer ----------------------------------------------------------


def test_buffer_zero_allows_marginal_call(tmp_path):
    cfg = _cfg(tmp_path, trend_buffer_pct=0.0)
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.3]  # last just above SMA
    quote = Quote(symbol="SPY", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is Side.CALL


def test_buffer_blocks_marginal_call(tmp_path):
    cfg = _cfg(tmp_path, trend_buffer_pct=0.01)  # require 1% beyond the SMA
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.3]  # only ~0.24% above SMA
    quote = Quote(symbol="SPY", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is None


# --- slope alignment -------------------------------------------------------


def test_slope_blocks_call_when_sma_falling(tmp_path):
    # Price ticks up on the last bar (above the SMA) but the SMA is still
    # falling over the lookback -> a call into a rolling-over average is denied.
    cfg = _cfg(tmp_path, slope_lookback=3)
    closes = [104.0, 103.0, 102.0, 101.0, 100.0, 99.0, 100.0, 101.0]
    quote = Quote(symbol="SPY", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is None


def test_slope_disabled_allows_same_series(tmp_path):
    # Same series with the slope gate off fires the CALL (price is above SMA).
    cfg = _cfg(tmp_path, slope_lookback=0)
    closes = [104.0, 103.0, 102.0, 101.0, 100.0, 99.0, 100.0, 101.0]
    quote = Quote(symbol="SPY", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is Side.CALL
