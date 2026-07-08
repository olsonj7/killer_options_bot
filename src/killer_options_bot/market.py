"""US equity/options market hours.

A deliberately small helper so the run loop can act only while the market is
open. Regular session is weekdays 09:30-16:00 America/New_York.

Holidays are NOT handled yet: on a market holiday this reports "open" during
regular hours. That is safe for paper trading (a real data source simply
returns no fresh quotes), but must be revisited before any live use.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

#: Regular trading session in Eastern time.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def now_eastern() -> datetime:
    """Current wall-clock time in US Eastern."""
    return datetime.now(EASTERN)


def _as_eastern(moment: datetime) -> datetime:
    """Return ``moment`` in Eastern time (assume Eastern if naive)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=EASTERN)
    return moment.astimezone(EASTERN)


def is_weekday(moment: datetime) -> bool:
    return _as_eastern(moment).weekday() < 5  # Mon=0 .. Fri=4


def is_market_open(moment: datetime | None = None) -> bool:
    """True if ``moment`` (default: now) is within the regular session."""
    et = _as_eastern(moment or now_eastern())
    if et.weekday() >= 5:
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
    # If we're already past today's open (or it's the weekend), roll forward a
    # day until we land on a weekday open that is still in the future.
    if et.time() >= MARKET_OPEN:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5 or candidate <= et:
        candidate += timedelta(days=1)
    return candidate


def seconds_until_open(moment: datetime | None = None) -> float:
    """Seconds from ``moment`` until the next session open (0 if open now)."""
    et = _as_eastern(moment or now_eastern())
    if is_market_open(et):
        return 0.0
    return (next_open(et) - et).total_seconds()
