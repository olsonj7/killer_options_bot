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
    CostModel,
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
        cost_model: CostModel | None = None,
    ):
        self.config = config
        self.data = data
        self.storage = storage
        self.as_of = as_of or date.today()
        # When None, fills happen at the mid with no commission (the original
        # behaviour). A CostModel makes entries/exits fill at bid/ask + fees.
        self.cost_model = cost_model

    # --- Fill pricing ------------------------------------------------------

    def _entry_price(self, contract: OptionContract) -> float:
        if self.cost_model is None:
            return contract.mid
        return self.cost_model.entry_fill(contract)

    def _exit_price(self, contract: OptionContract) -> float:
        if self.cost_model is None:
            return contract.mid
        return self.cost_model.exit_fill(contract)

    def _settle_price(self, intrinsic: float) -> float:
        if self.cost_model is None:
            return round(intrinsic, 2)
        return self.cost_model.settle_fill(intrinsic)

    def _exits_for(self, position: PaperPosition):
        """Exit rules for a position, resolved by the strategy that opened it.

        A 0DTE scalp and a LEAPS hold must be managed with different rules, so
        each position is exited under its own strategy's exits. Falls back to
        the base exits when the strategy is unknown (e.g. removed from config).
        """
        for strat in self.config.active_strategies:
            if strat.name == position.strategy:
                return strat.exits
        return self.config.exits

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
            entry_price=self._entry_price(c),
            entry_date=self.as_of,
            status=PositionStatus.OPEN,
            strategy=candidate.strategy,
        )
        self.storage.open_position(position)
        return position

    # --- Exit decision -----------------------------------------------------

    def exit_reason(
        self, position: PaperPosition, option_price: float
    ) -> str | None:
        """Return an exit reason if any rule triggers, else None."""
        e = self._exits_for(position)
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
            # The option is no longer quoted (typically at/after expiration).
            # Settle at intrinsic value from the underlying rather than assuming
            # a total loss: an option held to expiry realizes max(0, intrinsic).
            if position.dte(self.as_of) <= 0:
                last = self.data.get_quote(position.underlying).last
                if position.side is Side.CALL:
                    intrinsic = max(0.0, last - position.strike)
                else:
                    intrinsic = max(0.0, position.strike - last)
                settle = self._settle_price(intrinsic)
                self.storage.close_position(
                    position.id, settle, self.as_of, "expired at intrinsic"
                )
                return ManageResult(
                    position, settle, True, "expired at intrinsic"
                )
            return ManageResult(position, None, False, "no price available")

        # Exit rules trigger on the current market mid; the actual fill is at
        # the (worse) cost-adjusted exit price.
        reason = self.exit_reason(position, contract.mid)
        if reason is not None:
            fill = self._exit_price(contract)
            self.storage.close_position(position.id, fill, self.as_of, reason)
            return ManageResult(position, fill, True, reason)
        return ManageResult(position, contract.mid, False, "hold")

    def manage_all(self) -> list[ManageResult]:
        return [self.manage_position(p) for p in self.storage.open_positions()]

    # --- Reporting ---------------------------------------------------------

    def mark_to_market(self, position: PaperPosition) -> float | None:
        contract = _find_contract(
            self.data, position.underlying, position.side, position.option_symbol
        )
        return contract.mid if contract else None

    def exit_fill_price(self, position: PaperPosition) -> float | None:
        """Cost-aware price to close a position now (for forced closes)."""
        contract = _find_contract(
            self.data, position.underlying, position.side, position.option_symbol
        )
        return self._exit_price(contract) if contract else None

    def realized_pl_total(self) -> float:
        return round(
            sum(p.realized_pl() or 0.0 for p in self.storage.closed_positions()),
            2,
        )
