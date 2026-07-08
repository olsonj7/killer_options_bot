"""Tests for indicators."""

from __future__ import annotations

from killer_options_bot.indicators import rsi, sma


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([2, 4, 6], 2) == 5.0


def test_sma_insufficient_data():
    assert sma([1, 2], 5) is None
    assert sma([], 3) is None


def test_rsi_all_gains_is_100():
    values = [float(i) for i in range(1, 20)]
    assert rsi(values, 14) == 100.0


def test_rsi_insufficient_data():
    assert rsi([1, 2, 3], 14) is None


def test_rsi_in_range():
    values = [10, 11, 10.5, 11.2, 10.8, 11.5, 11.1, 11.8, 11.3,
              12.0, 11.6, 12.2, 11.9, 12.5, 12.1, 12.7]
    r = rsi(values, 14)
    assert r is not None
    assert 0 <= r <= 100
