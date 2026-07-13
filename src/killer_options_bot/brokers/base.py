"""Market-data source protocol.

Data sources only *read* market data. Order execution lives behind a separate,
heavily guarded ``OrderBroker`` interface (see ``killer_options_bot.live``) and
is disabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from killer_options_bot.models import OptionContract, Quote, Side


class MarketData(Protocol):
    """Anything that can supply quotes and option chains."""

    def get_quote(self, symbol: str) -> Quote:
        """Return a Quote (last price + recent closes) for an underlying."""
        ...

    def get_option_chain(
        self, symbol: str, side: Side
    ) -> list[OptionContract]:
        """Return available option contracts for a symbol and side."""
        ...

    def get_intraday_closes(
        self, symbol: str, interval: str = "5min"
    ) -> list[float]:
        """Return the current session's intraday bar closes, oldest first.

        Optional: sources that cannot supply intraday bars may omit this (the
        scanner guards with ``getattr`` and intraday signals then decline).
        """
        ...

    def get_intraday_bars(
        self, symbol: str, interval: str = "15min"
    ):
        """Return the current session's intraday OHLC bars, oldest first.

        Optional: like ``get_intraday_closes`` but preserves each bar's full
        open/high/low/close range for price-action signals (STRAT bar typing,
        displacement). Sources that cannot supply bars may omit this; the
        scanner guards with ``getattr`` and such signals then decline.
        """
        ...

    def get_daily_bars(self, symbol: str, lookback: int = 5):
        """Return recent daily OHLC bars, oldest first (most recent last).

        Optional: used for prior-day high/low (PDH/PDL) bias. Sources that
        cannot supply bars may omit this; the scanner guards with ``getattr``.
        """
        ...


@dataclass(frozen=True)
class OrderResult:
    """Outcome of an order request (or a dry-run preview)."""

    accepted: bool
    order_id: str | None
    detail: str
    dry_run: bool = False


class OrderBroker(Protocol):
    """Places limit option orders. Implementations must never use market
    orders. Used only by the guarded live-execution path."""

    def place_option_limit(
        self,
        *,
        option_symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        dry_run: bool,
    ) -> OrderResult:
        """Submit (or preview) a single-leg limit order to open a position."""
        ...

