"""Paper-trading engine: simulate fills, mark-to-market, and apply exit rules.

This is Phase 2. It never touches real money and never sends broker orders.
Fills are simulated at the current option mid price. Exits are driven purely by
the rules in ``config.exits``:

- profit target (option up X% from entry)
- stop loss (option down Y% from entry)
- max holding days
- minimum DTE (exit before the final expiration zone)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.config import Config
from killer_options_bot.models import (
    Candidate,
    OptionContract,
    PaperPosition,
    PositionStatus,
    Side,
)
from killer_options_bot.storage import Storage


@dataclass
class ManageResult:
    """Outcome of managing a single open position on a given day."""

    position: PaperPosition
    price: float | None
    closed: bool
    reason: str


def _find_contract(
    data: MarketData, underlying: str, side: Side, option_symbol: str
) -> OptionContract | None:
    for contract in data.get_option_chain(underlying, side):
        if contract.symbol == option_symbol:
            return contract
    return None


class PaperEngine:
    def __init__(
        self,
        config: Config,
        data: MarketData,
        storage: Storage,
        as_of: date | None = None,
    ):
        self.config = config
        self.data = data
        self.storage = storage
        self.as_of = as_of or date.today()

    # --- Opening -----------------------------------------------------------

    def open_from_candidate(
        self, candidate: Candidate, quantity: int = 1
    ) -> PaperPosition | None:
        """Open a paper position from an allowed candidate.

        Returns None if guardrails block the open (max open positions,
        duplicate symbol, or the candidate was not allowed by risk).
        """
        if not candidate.decision.allowed:
            return None
        if self.storage.count_open_positions() >= self.config.risk.max_open_positions:
            return None
        if self.storage.has_open_position(candidate.contract.symbol):
            return None

        c = candidate.contract
        position = PaperPosition(
            option_symbol=c.symbol,
            underlying=c.underlying,
            side=candidate.side,
            strike=c.strike,
            expiration=c.expiration,
            quantity=quantity,
            entry_price=c.mid,
            entry_date=self.as_of,
            status=PositionStatus.OPEN,
        )
        self.storage.open_position(position)
        return position

    # --- Exit decision -----------------------------------------------------

    def exit_reason(
        self, position: PaperPosition, option_price: float
    ) -> str | None:
        """Return an exit reason if any rule triggers, else None."""
        e = self.config.exits
        pl_pct = position.pl_pct(option_price)

        if pl_pct >= e.profit_target_pct:
            return f"profit target hit (+{pl_pct:.0%})"
        if pl_pct <= -e.stop_loss_pct:
            return f"stop loss hit ({pl_pct:.0%})"
        if position.holding_days(self.as_of) >= e.max_holding_days:
            return f"max holding days ({e.max_holding_days}d) reached"
        if position.dte(self.as_of) <= e.min_dte_exit:
            return f"DTE <= {e.min_dte_exit}, exiting expiration zone"
        return None

    # --- Managing open positions ------------------------------------------

    def manage_position(self, position: PaperPosition) -> ManageResult:
        contract = _find_contract(
            self.data, position.underlying, position.side, position.option_symbol
        )
        if contract is None:
            # No live price. As a safety fallback, exit at expiration only.
            if position.dte(self.as_of) <= 0:
                self.storage.close_position(
                    position.id, 0.0, self.as_of, "expired, no market price"
                )
                return ManageResult(position, None, True, "expired (no price)")
            return ManageResult(position, None, False, "no price available")

        price = contract.mid
        reason = self.exit_reason(position, price)
        if reason is not None:
            self.storage.close_position(position.id, price, self.as_of, reason)
            return ManageResult(position, price, True, reason)
        return ManageResult(position, price, False, "hold")

    def manage_all(self) -> list[ManageResult]:
        return [self.manage_position(p) for p in self.storage.open_positions()]

    # --- Reporting ---------------------------------------------------------

    def mark_to_market(self, position: PaperPosition) -> float | None:
        contract = _find_contract(
            self.data, position.underlying, position.side, position.option_symbol
        )
        return contract.mid if contract else None

    def realized_pl_total(self) -> float:
        return round(
            sum(p.realized_pl() or 0.0 for p in self.storage.closed_positions()),
            2,
        )
