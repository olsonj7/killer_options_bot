"""US equity/options market hours.

A deliberately small helper so the run loop can act only while the market is
open. Regular session is weekdays 09:30-16:00 America/New_York, excluding the
NYSE/Nasdaq holiday calendar (computed below, no external dependency).

Early-close days (e.g. the day after Thanksgiving, Christmas Eve) are treated
as normal full sessions here; that only means the bot may keep scanning for the
final ~3 hours those days, which is harmless for paper trading.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

#: Regular trading session in Eastern time.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the ``n``-th ``weekday`` (Mon=0) in a month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the date of the last ``weekday`` (Mon=0) in a month."""
    # Start at the first of the next month, step back to the target weekday.
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian / Meeus-Jones-Butcher)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """Apply the NYSE weekend-observance rule.

    Saturday holidays are observed the preceding Friday; Sunday holidays the
    following Monday.
    """
    if holiday.weekday() == 5:  # Saturday -> Friday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday -> Monday
        return holiday + timedelta(days=1)
    return holiday


@lru_cache(maxsize=64)
def market_holidays(year: int) -> frozenset[date]:
    """Full-day NYSE/Nasdaq market closures for a given year.

    Includes Juneteenth from 2022 onward (its first observance as a market
    holiday). Does not include one-off closures (e.g. national days of
    mourning) or early-close half days.
    """
    days = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Jr. Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday (3rd Mon Feb)
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day (last Mon May)
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


def is_market_holiday(day: date) -> bool:
    """True if ``day`` is a full-day market closure."""
    return day in market_holidays(day.year)


def now_eastern() -> datetime:
    """Current wall-clock time in US Eastern."""
    return datetime.now(EASTERN)


def _as_eastern(moment: datetime) -> datetime:
    """Return ``moment`` in Eastern time (assume Eastern if naive)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=EASTERN)
    return moment.astimezone(EASTERN)


def is_trading_day(day: date) -> bool:
    """True if ``day`` is a weekday and not a market holiday."""
    return day.weekday() < 5 and not is_market_holiday(day)


def is_weekday(moment: datetime) -> bool:
    return _as_eastern(moment).weekday() < 5  # Mon=0 .. Fri=4


def is_market_open(moment: datetime | None = None) -> bool:
    """True if ``moment`` (default: now) is within a regular open session."""
    et = _as_eastern(moment or now_eastern())
    if not is_trading_day(et.date()):
        return False
    return MARKET_OPEN <= et.time() < MARKET_CLOSE


def next_open(moment: datetime | None = None) -> datetime:
    """Return the next session open at/after ``moment`` (Eastern)."""
    et = _as_eastern(moment or now_eastern())
    candidate = et.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )
    # If we're already past today's open, roll forward a day. Then advance until
    # we land on a trading day (skipping weekends and holidays) still ahead.
    if et.time() >= MARKET_OPEN:
        candidate += timedelta(days=1)
    while not is_trading_day(candidate.date()) or candidate <= et:
        candidate += timedelta(days=1)
    return candidate


def seconds_until_open(moment: datetime | None = None) -> float:
    """Seconds from ``moment`` until the next session open (0 if open now)."""
    et = _as_eastern(moment or now_eastern())
    if is_market_open(et):
        return 0.0
    return (next_open(et) - et).total_seconds()
