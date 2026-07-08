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

