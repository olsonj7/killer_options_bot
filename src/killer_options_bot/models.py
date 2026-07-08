"""Typed data models shared across the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    """Which direction the underlying signal points."""

    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Quote:
    """A minimal snapshot of an underlying symbol."""

    symbol: str
    last: float
    # Historical closes, oldest first, used for indicators.
    closes: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class OptionContract:
    """A single option contract from a chain."""

    symbol: str  # OCC option symbol
    underlying: str
    side: Side
    strike: float
    expiration: date
    bid: float
    ask: float
    last: float
    delta: float
    implied_volatility: float
    volume: int
    open_interest: int

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return self.last
        return round((self.bid + self.ask) / 2, 4)

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a fraction of the mid price."""
        mid = self.mid
        if mid <= 0:
            return 1.0
        return round((self.ask - self.bid) / mid, 4)

    def dte(self, as_of: date | None = None) -> int:
        """Days to expiration from ``as_of`` (defaults to today)."""
        as_of = as_of or date.today()
        return (self.expiration - as_of).days

    @property
    def cost(self) -> float:
        """Dollar cost to buy one contract (100 multiplier) at the mid."""
        return round(self.mid * 100, 2)


@dataclass(frozen=True)
class RiskDecision:
    """Result of running a contract through the risk engine."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(allowed=False, reasons=[reason])

    @classmethod
    def accept(cls) -> "RiskDecision":
        return cls(allowed=True, reasons=[])


@dataclass(frozen=True)
class Candidate:
    """A scanned contract that passed (or was evaluated by) the risk engine."""

    contract: OptionContract
    side: Side
    signal_note: str
    decision: RiskDecision
    max_loss: float
    created_at: datetime = field(default_factory=datetime.utcnow)


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class PaperPosition:
    """A simulated long-option position opened in paper mode.

    All prices are per-share option prices; multiply by 100 * quantity for
    dollars. ``quantity`` is the number of contracts.
    """

    option_symbol: str
    underlying: str
    side: Side
    strike: float
    expiration: date
    quantity: int
    entry_price: float  # per-share option mid at entry
    entry_date: date
    status: PositionStatus = PositionStatus.OPEN
    exit_price: float | None = None
    exit_date: date | None = None
    exit_reason: str | None = None
    id: int | None = None

    @property
    def entry_cost(self) -> float:
        """Total debit paid in dollars."""
        return round(self.entry_price * 100 * self.quantity, 2)

    def value_at(self, option_price: float) -> float:
        """Mark-to-market dollar value at a given per-share option price."""
        return round(option_price * 100 * self.quantity, 2)

    def unrealized_pl(self, option_price: float) -> float:
        return round(self.value_at(option_price) - self.entry_cost, 2)

    def realized_pl(self) -> float | None:
        if self.exit_price is None:
            return None
        return round(
            self.value_at(self.exit_price) - self.entry_cost, 2
        )

    def pl_pct(self, option_price: float) -> float:
        """Return as a fraction of the entry debit."""
        if self.entry_price <= 0:
            return 0.0
        return round((option_price - self.entry_price) / self.entry_price, 4)

    def holding_days(self, as_of: date) -> int:
        return (as_of - self.entry_date).days

    def dte(self, as_of: date) -> int:
        return (self.expiration - as_of).days

