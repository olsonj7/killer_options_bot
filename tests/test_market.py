"""Tests for market-hours helpers used by the run loop."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from killer_options_bot import market

ET = ZoneInfo("America/New_York")


def test_open_during_regular_session():
    assert market.is_market_open(datetime(2026, 7, 8, 10, 0, tzinfo=ET))
    assert market.is_market_open(datetime(2026, 7, 8, 9, 30, tzinfo=ET))
    assert market.is_market_open(datetime(2026, 7, 8, 15, 59, tzinfo=ET))


def test_closed_before_open_and_after_close():
    assert not market.is_market_open(datetime(2026, 7, 8, 9, 29, tzinfo=ET))
    # 16:00 is the close boundary (exclusive).
    assert not market.is_market_open(datetime(2026, 7, 8, 16, 0, tzinfo=ET))
    assert not market.is_market_open(datetime(2026, 7, 8, 16, 30, tzinfo=ET))


def test_closed_on_weekend():
    assert not market.is_market_open(datetime(2026, 7, 11, 12, 0, tzinfo=ET))
    assert not market.is_market_open(datetime(2026, 7, 12, 12, 0, tzinfo=ET))


def test_next_open_rolls_over_weekend():
    # Saturday noon -> Monday 09:30.
    nxt = market.next_open(datetime(2026, 7, 11, 12, 0, tzinfo=ET))
    assert nxt.weekday() == 0
    assert (nxt.hour, nxt.minute) == (9, 30)


def test_next_open_same_day_before_open():
    # Wednesday 08:00 -> same day 09:30.
    nxt = market.next_open(datetime(2026, 7, 8, 8, 0, tzinfo=ET))
    assert nxt.date() == datetime(2026, 7, 8).date()
    assert (nxt.hour, nxt.minute) == (9, 30)


def test_seconds_until_open_zero_when_open():
    assert (
        market.seconds_until_open(datetime(2026, 7, 8, 10, 0, tzinfo=ET)) == 0.0
    )


def test_naive_datetime_treated_as_eastern():
    # A naive datetime should be interpreted as Eastern, not raise.
    assert market.is_market_open(datetime(2026, 7, 8, 10, 0))
