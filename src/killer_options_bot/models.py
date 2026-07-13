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
class Bar:
    """A single OHLC price bar (daily or intraday).

    Carries the full range needed by price-action signals (TheSTRAT bar typing,
    fair-value gaps, prior-day high/low bias). Unlike a bare close, the high and
    low are what define a bar's "range" and whether the next bar breaks it.
    """

    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        """Full high-to-low range of the bar (always >= 0)."""
        return max(0.0, self.high - self.low)

    @property
    def midpoint(self) -> float:
        """Midpoint of the bar's range."""
        return (self.high + self.low) / 2

    @property
    def is_up(self) -> bool:
        """True if the bar closed at or above its open."""
        return self.close >= self.open


@dataclass(frozen=True)
class Quote:
    """A minimal snapshot of an underlying symbol."""

    symbol: str
    last: float
    # Historical daily closes, oldest first, used for daily indicators.
    closes: list[float] = field(default_factory=list)
    # Intraday bar closes for the current session, oldest first. Populated only
    # for strategies that use an intraday signal (e.g. 0DTE); empty otherwise.
    intraday: list[float] = field(default_factory=list)
    # Intraday OHLC bars for the current session, oldest first. Populated only
    # for price-action signals that need full ranges (e.g. STRAT bar typing);
    # empty otherwise.
    bars: list["Bar"] = field(default_factory=list)
    # Recent daily OHLC bars, oldest first, ending with the most recently
    # completed session. Used for prior-day high/low (PDH/PDL) bias.
    daily_bars: list["Bar"] = field(default_factory=list)


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
class CostModel:
    """Models the real cost of getting in and out of an option position.

    Backtests that fill at the mid price systematically overstate their edge:
    in reality you BUY at (near) the ask and SELL at (near) the bid, and each
    contract carries a commission. Both effects are folded into the per-share
    fill price so realized P/L (which multiplies by 100) already reflects them.

    - ``slippage_frac`` is the fraction of the half-spread you cross. 1.0 means
      you pay the full ask on entry and receive the full bid on exit (the
      conservative, realistic default for illiquid weeklies). 0.0 means mid.
    - ``commission_per_contract`` is charged on BOTH entry and exit.
    """

    commission_per_contract: float = 0.65
    slippage_frac: float = 1.0

    @classmethod
    def free(cls) -> "CostModel":
        """A zero-cost model (fills at mid, no commission)."""
        return cls(commission_per_contract=0.0, slippage_frac=0.0)

    @staticmethod
    def _half_spread(contract: "OptionContract") -> float:
        return max(0.0, (contract.ask - contract.bid) / 2)

    def _adjustment(self, contract: "OptionContract") -> float:
        """Per-share penalty applied to a fill: half-spread + commission."""
        return (
            self._half_spread(contract) * self.slippage_frac
            + self.commission_per_contract / 100
        )

    def entry_fill(self, contract: "OptionContract") -> float:
        """Per-share price actually paid to BUY one contract (worse than mid)."""
        return round(contract.mid + self._adjustment(contract), 4)

    def exit_fill(self, contract: "OptionContract") -> float:
        """Per-share price actually received to SELL one contract."""
        return round(max(0.0, contract.mid - self._adjustment(contract)), 4)

    def settle_fill(self, intrinsic: float) -> float:
        """Per-share settlement at expiry: commission only, no spread."""
        return round(max(0.0, intrinsic - self.commission_per_contract / 100), 4)


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
    strategy: str = "default"
    created_at: datetime = field(default_factory=datetime.utcnow)
    #: DB row id, set after the candidate is recorded. Lets the open step
    #: annotate this exact row when a downstream guardrail blocks the trade.
    id: int | None = None


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class PaperPosition:
    """A simulated long-option position opened in paper mode.

    All prices are per-share option prices; multiply by 100 * quantity for
    dollars. ``quantity`` is the number of contracts *currently held*.

    Positions can be *scaled out* (trimmed): partial exits sell a portion of the
    contracts and bank that profit. When that happens ``quantity`` shrinks to
    the remaining contracts, ``original_quantity`` keeps the size the position
    was opened at, ``realized_pl_banked`` accumulates the dollars locked in by
    trims, and ``trims_done`` counts how many trim levels have fired. The
    terminal exit then closes whatever remains.
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
    strategy: str = "default"
    original_quantity: int | None = None
    realized_pl_banked: float = 0.0
    trims_done: int = 0
    mode: str = "paper"
    broker_order_id: str | None = None
    high_water_mark: float | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        # A freshly opened position has not been trimmed, so its original size
        # equals its current size unless a stored value is supplied.
        if self.original_quantity is None:
            self.original_quantity = self.quantity
        # The high-water mark tracks the peak option mid seen while held; a
        # fresh position starts at its entry price.
        if self.high_water_mark is None:
            self.high_water_mark = self.entry_price

    @property
    def entry_cost(self) -> float:
        """Total debit currently at risk in dollars (remaining contracts)."""
        return round(self.entry_price * 100 * self.quantity, 2)

    @property
    def initial_cost(self) -> float:
        """Debit paid at entry for the full original size (the risk basis).

        R-multiples are measured against this: a full stop-out of the whole
        original position is -1R, regardless of any trims taken along the way.
        """
        qty = self.original_quantity or self.quantity
        return round(self.entry_price * 100 * qty, 2)

    def value_at(self, option_price: float) -> float:
        """Mark-to-market dollar value of the *remaining* contracts."""
        return round(option_price * 100 * self.quantity, 2)

    def unrealized_pl(self, option_price: float) -> float:
        return round(self.value_at(option_price) - self.entry_cost, 2)

    def realized_pl(self) -> float | None:
        """Total realized P/L once closed: banked trims + the final leg."""
        if self.exit_price is None:
            return None
        final_leg = (self.exit_price - self.entry_price) * 100 * self.quantity
        return round(self.realized_pl_banked + final_leg, 2)

    def pl_pct(self, option_price: float) -> float:
        """Return as a fraction of the entry debit."""
        if self.entry_price <= 0:
            return 0.0
        return round((option_price - self.entry_price) / self.entry_price, 4)

    def holding_days(self, as_of: date) -> int:
        return (as_of - self.entry_date).days

    def dte(self, as_of: date) -> int:
        return (self.expiration - as_of).days

