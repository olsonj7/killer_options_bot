"""Tests for the STRAT 2-1-2 / 3-1-2 combo signal and its confirmations."""

from __future__ import annotations

from conftest import make_config

from killer_options_bot.models import Bar, Quote, Side
from killer_options_bot.scanner import strat_combo_signal

YDAY = "2026-11-04"
TODAY = "2026-11-05"


def _bar(t: str, high: float, low: float, close: float | None = None,
         volume: float = 1000.0) -> Bar:
    mid = (high + low) / 2
    return Bar(open=mid, high=high, low=low, close=mid if close is None else close,
               volume=volume, time=t)


def _yesterday(n: int = 10) -> list[Bar]:
    return [_bar(f"{YDAY}T15:{i:02d}:00", 100.5, 99.5) for i in range(n)]


def _daily(high: float = 105.0, low: float = 95.0) -> list[Bar]:
    return [_bar(YDAY, high, low)]


def _quote(bars: list[Bar], daily: list[Bar] | None = None) -> Quote:
    return Quote(symbol="SPY", last=bars[-1].close, bars=bars,
                 daily_bars=_daily() if daily is None else daily)


def _two_one_two(setup_volume: float = 2000.0, setup_day: str = TODAY) -> list[Bar]:
    return _yesterday() + [
        _bar(f"{TODAY}T09:30:00", 100.5, 99.5),                          # before
        _bar(f"{setup_day}T09:45:00", 101.5, 100.0, volume=setup_volume),  # 2up
        _bar(f"{TODAY}T10:00:00", 101.2, 100.3, volume=800),             # inside
        _bar(f"{TODAY}T10:15:00", 101.8, 100.9, close=101.6),            # breaks high
    ]


def test_2_1_2_continuation_fires_call(tmp_path):
    sig = strat_combo_signal(_quote(_two_one_two()), make_config(tmp_path))
    assert sig.side is Side.CALL
    assert "2-1-2 (2up-1-2up)" in sig.note


def test_3_1_2_reversal_fires_put(tmp_path):
    bars = _yesterday() + [
        _bar(f"{TODAY}T09:30:00", 100.5, 99.5),
        _bar(f"{TODAY}T09:45:00", 101.0, 99.0, volume=2000),   # outside (3)
        _bar(f"{TODAY}T10:00:00", 100.6, 99.4, volume=800),    # inside
        _bar(f"{TODAY}T10:15:00", 100.2, 99.0, close=99.2),    # breaks low
    ]
    sig = strat_combo_signal(_quote(bars), make_config(tmp_path))
    assert sig.side is Side.PUT
    assert "3-1-2 (3-1-2down)" in sig.note


def test_inside_bar_not_broken_waits(tmp_path):
    bars = _two_one_two()
    bars[-1] = _bar(f"{TODAY}T10:15:00", 101.1, 100.5, close=100.8)
    assert strat_combo_signal(_quote(bars), make_config(tmp_path)).side is None


def test_break_that_failed_to_hold_declines(tmp_path):
    bars = _two_one_two()
    bars[-1] = _bar(f"{TODAY}T10:15:00", 101.8, 100.9, close=101.0)  # back inside
    assert strat_combo_signal(_quote(bars), make_config(tmp_path)).side is None


def test_low_setup_volume_rejected(tmp_path):
    sig = strat_combo_signal(_quote(_two_one_two(setup_volume=1000)),
                             make_config(tmp_path))
    assert sig.side is None
    assert "volume" in sig.note


def test_call_right_under_prior_day_high_rejected(tmp_path):
    sig = strat_combo_signal(_quote(_two_one_two(), _daily(high=102.0)),
                             make_config(tmp_path))
    assert sig.side is None
    assert "PDH" in sig.note


def test_call_already_through_prior_day_high_allowed(tmp_path):
    sig = strat_combo_signal(_quote(_two_one_two(), _daily(high=101.0)),
                             make_config(tmp_path))
    assert sig.side is Side.CALL


def test_setup_from_prior_session_ignored(tmp_path):
    sig = strat_combo_signal(_quote(_two_one_two(setup_day=YDAY)),
                             make_config(tmp_path))
    assert sig.side is None
    assert "prior session" in sig.note


def test_todays_partial_daily_bar_is_not_used_as_prior_day(tmp_path):
    # Today's in-progress daily bar (high 101.8) would leave no room; it must be
    # skipped in favor of yesterday's (high 105).
    daily = _daily() + [_bar(TODAY, 101.8, 99.0)]
    sig = strat_combo_signal(_quote(_two_one_two(), daily), make_config(tmp_path))
    assert sig.side is Side.CALL


def test_fires_on_first_combo_of_the_morning(tmp_path):
    # Setup 9:45, inside 10:00, trigger 10:15: yesterday's bars supply the
    # volume/range baseline, so no waiting until midday.
    sig = strat_combo_signal(_quote(_two_one_two()), make_config(tmp_path))
    assert sig.side is Side.CALL
