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


# --- higher-timeframe (weekly) trend alignment -----------------------------
#
# Regression coverage for a live QQQ trade: the daily SMA/RSI read was
# bearish (a multi-day pullback) while the weekly bar was still green, and the
# bot fired a PUT straight into an uptrend. weekly_bar_days/weekly_sma_period
# bucket daily closes into synthetic weekly bars so this countertrend case is
# rejected instead.

# Four synthetic "weeks" (5 trading days each) of daily closes: three rising
# weeks followed by a pullback within week 4 that still closes the week (day
# 20) above the trailing weekly average -- i.e. "the weekly bar is green"
# while the daily read has turned sharply bearish.
_WEEKLY_UP_PULLBACK_CLOSES = [
    40.0, 42.0, 44.0, 46.0, 48.0,       # week 1
    55.0, 60.0, 65.0, 70.0, 75.0,       # week 2
    85.0, 90.0, 95.0, 98.0, 100.0,      # week 3
    99.0, 97.0, 95.0, 93.0, 90.0,       # week 4 (daily pullback)
]


def test_weekly_trend_blocks_countertrend_put(tmp_path):
    # Daily SMA(5)/RSI(5) alone want a PUT (last < SMA, RSI deeply oversold),
    # but the synthetic weekly trend is still up -- the higher-timeframe gate
    # must refuse the countertrend short.
    cfg = _cfg(
        tmp_path,
        rsi_min=100.0,  # always satisfies "r < rsi_min" so only the SMA/
        # weekly-trend gate is under test.
        weekly_bar_days=5,
        weekly_sma_period=3,
    )
    closes = _WEEKLY_UP_PULLBACK_CLOSES
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    signal = momentum_signal(quote, cfg)
    assert signal.side is None
    assert "weekly" in signal.note.lower() or "trend" in signal.note.lower()


def test_weekly_trend_allows_aligned_put(tmp_path):
    # Same setup, but week 4 crashes hard enough that the weekly trend itself
    # has turned down -- the PUT is no longer countertrend, so it fires.
    closes = _WEEKLY_UP_PULLBACK_CLOSES[:15] + [70.0, 60.0, 50.0, 40.0, 30.0]
    cfg = _cfg(
        tmp_path, rsi_min=100.0, weekly_bar_days=5, weekly_sma_period=3
    )
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is Side.PUT


def test_weekly_trend_blocks_countertrend_call(tmp_path):
    # Mirror image: daily SMA(5) wants a CALL (last > SMA) after a bounce, but
    # the synthetic weekly trend is still down -- refuse the countertrend long.
    closes = [140.0 - c for c in _WEEKLY_UP_PULLBACK_CLOSES]
    cfg = _cfg(tmp_path, weekly_bar_days=5, weekly_sma_period=3)
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    signal = momentum_signal(quote, cfg)
    assert signal.side is None
    assert "weekly" in signal.note.lower() or "trend" in signal.note.lower()


# --- support / resistance guard ---------------------------------------------

# A steady decline (support test) with one much lower close far in the past
# (idx 0) so shrinking/growing the lookback window changes whether that older
# low counts as "the" support level.
_SUPPORT_TEST_CLOSES = [
    70.0, 100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 89.0, 88.0, 87.0, 86.0, 85.0,
    84.0,
]


def test_support_blocks_countertrend_put(tmp_path):
    # Daily SMA/RSI want a PUT, and price (84) sits right at the lookback low
    # (also 84 within the 10-bar window) -- likely support/bounce zone.
    cfg = _cfg(
        tmp_path, rsi_min=100.0, sr_lookback=10, sr_buffer_pct=0.02
    )
    closes = _SUPPORT_TEST_CLOSES
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    signal = momentum_signal(quote, cfg)
    assert signal.side is None
    assert "support" in signal.note.lower()


def test_support_allows_put_away_from_level(tmp_path):
    # Widen the lookback to reach the much-lower close at the start of the
    # series (70): the current price (84) is no longer near that level, so
    # the PUT is no longer blocked.
    cfg = _cfg(
        tmp_path, rsi_min=100.0, sr_lookback=13, sr_buffer_pct=0.02
    )
    closes = _SUPPORT_TEST_CLOSES
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    assert momentum_signal(quote, cfg).side is Side.PUT


def test_resistance_blocks_countertrend_call(tmp_path):
    # Mirror of the support case: a steady rally with price (86) sitting right
    # at the lookback high -- likely resistance/rejection zone for a CALL.
    closes = [170.0 - c for c in _SUPPORT_TEST_CLOSES]
    cfg = _cfg(tmp_path, sr_lookback=10, sr_buffer_pct=0.02)
    quote = Quote(symbol="QQQ", last=closes[-1], closes=closes)
    signal = momentum_signal(quote, cfg)
    assert signal.side is None
    assert "resistance" in signal.note.lower()
