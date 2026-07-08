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


def test_2026_holiday_calendar():
    from datetime import date

    expected = {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed; Jul 4 is Saturday)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    }
    assert set(market.market_holidays(2026)) == expected


def test_market_closed_on_holiday():
    from datetime import date

    # Independence Day observed 2026-07-03 (a Friday) is a full closure.
    assert market.is_market_holiday(date(2026, 7, 3))
    assert not market.is_market_open(datetime(2026, 7, 3, 11, 0, tzinfo=ET))
    assert not market.is_trading_day(date(2026, 7, 3))


def test_next_open_skips_holiday():
    from datetime import date

    # Thursday July 2 2026 after close -> skip Fri Jul 3 (holiday) and the
    # weekend -> Monday July 6 at 09:30.
    nxt = market.next_open(datetime(2026, 7, 2, 17, 0, tzinfo=ET))
    assert nxt.date() == date(2026, 7, 6)
    assert (nxt.hour, nxt.minute) == (9, 30)


def test_juneteenth_only_from_2022():
    from datetime import date

    assert date(2021, 6, 18) not in market.market_holidays(2021)
    assert date(2022, 6, 20) in market.market_holidays(2022)  # observed Monday
