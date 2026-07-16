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
    trimmed: int = 0
    banked: float = 0.0


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

    def _note_blocked(self, candidate: Candidate, reason: str) -> None:
        """Annotate the candidate row when a guardrail blocks the open.

        No-op when the candidate has no DB id (e.g. constructed in a test or a
        code path that didn't persist it), so callers never need to guard.
        """
        if candidate.id is not None:
            self.storage.mark_candidate_blocked(candidate.id, reason)

    def open_from_candidate(
        self, candidate: Candidate, quantity: int | None = None
    ) -> PaperPosition | None:
        """Open a paper position from an allowed candidate.

        When ``quantity`` is None the size is chosen by the config's position
        sizing (a single contract unless sizing is enabled). Returns None if
        guardrails block the open (max open positions, duplicate symbol, or the
        candidate was not allowed by risk).
        """
        if not candidate.decision.allowed:
            return None
        if self.storage.count_open_positions() >= self.config.risk.max_open_positions:
            self._note_blocked(candidate, "max open positions reached")
            return None

        # Per-strategy daily trade limit (0 = unlimited).
        strat_cfg = next(
            (s for s in self.config.active_strategies
             if s.name == (candidate.strategy or "default")),
            None,
        )
        if strat_cfg and strat_cfg.max_trades_per_day > 0:
            as_of = self.as_of or date.today()
            if self.storage.trades_today_for_strategy(candidate.strategy, as_of) >= strat_cfg.max_trades_per_day:
                self._note_blocked(
                    candidate,
                    f"daily limit ({strat_cfg.max_trades_per_day}) reached "
                    f"for {candidate.strategy}",
                )
                return None
        # One position per (strategy, underlying): never stack multiple
        # strikes/sides on the same name within a strategy. Different
        # strategies may hold the same underlying on different timeframes
        # (weekly swing + 0DTE scalp are independent trades).
        if self.storage.has_open_underlying(
            candidate.contract.underlying, candidate.strategy
        ):
            self._note_blocked(
                candidate,
                f"blocked: {candidate.strategy} already holding "
                f"{candidate.contract.underlying}",
            )
            return None

        c = candidate.contract
        entry_price = self._entry_price(c)
        if quantity is None:
            quantity = self.config.contracts_for(entry_price * 100)
        position = PaperPosition(
            option_symbol=c.symbol,
            underlying=c.underlying,
            side=candidate.side,
            strike=c.strike,
            expiration=c.expiration,
            quantity=quantity,
            entry_price=entry_price,
            entry_date=self.as_of,
            status=PositionStatus.OPEN,
            strategy=candidate.strategy,
            original_quantity=quantity,
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

        if e.trailing_enabled:
            # Trailing stop replaces the fixed profit target so the runner can
            # ride. It arms only once profit reaches trail_activate_pct, then
            # exits when the mid gives back trail_pct from its high-water mark.
            peak = position.high_water_mark or position.entry_price
            peak_pl = (peak - position.entry_price) / position.entry_price
            if peak_pl >= e.trail_activate_pct:
                trigger = peak * (1 - e.trail_pct)
                if option_price <= trigger:
                    return (
                        f"trailing stop hit ({pl_pct:+.0%}, "
                        f"peak +{peak_pl:.0%})"
                    )
        elif pl_pct >= e.profit_target_pct:
            return f"profit target hit (+{pl_pct:.0%})"

        if pl_pct <= -e.stop_loss_pct:
            return f"stop loss hit ({pl_pct:.0%})"

        # Calendar-based forced exits (time-in-trade and expiration zone) must
        # not fire on the ENTRY day. Otherwise a same-day / 0DTE strategy
        # (max_holding_days=0, or a 0DTE option where dte==min_dte_exit==0)
        # would be closed on the very first manage tick, seconds after opening,
        # before its profit/stop/trim/trail rules can work. Intraday risk is
        # already covered by the profit target, stop loss and trailing stop
        # above; these two rules only bound how long a trade may be *carried*.
        held = position.holding_days(self.as_of)
        if held >= 1:
            if held >= e.max_holding_days:
                return f"max holding days ({e.max_holding_days}d) reached"
            if position.dte(self.as_of) <= e.min_dte_exit:
                return f"DTE <= {e.min_dte_exit}, exiting expiration zone"
        return None

    # --- Managing open positions ------------------------------------------

    def _maybe_trim(
        self, position: PaperPosition, contract
    ) -> tuple[int, float]:
        """Scale out of a winning position per the strategy's trim ladder.

        Sells fractions of the *original* size as profit thresholds are hit,
        banking the realized P/L while leaving a runner. Returns
        ``(contracts_trimmed, dollars_banked)`` for this tick. If a trim would
        take the entire remaining position it becomes a full close (the final
        leg) and ``position.status`` is set to CLOSED.
        """
        e = self._exits_for(position)
        if not e.trims:
            return 0, 0.0
        orig = position.original_quantity or position.quantity
        if orig < 2:
            # Cannot scale out of a single contract.
            return 0, 0.0

        pl_pct = position.pl_pct(contract.mid)
        trimmed_total = 0
        banked_total = 0.0
        while position.trims_done < len(e.trims):
            rule = e.trims[position.trims_done]
            if pl_pct < rule.at_pct:
                break
            contracts = max(1, int(orig * rule.fraction))
            fill = self._exit_price(contract)
            if contracts >= position.quantity:
                # Trim would empty the position: close the remainder outright.
                reason = (
                    f"trim {position.trims_done + 1} closed runner "
                    f"({pl_pct:+.0%})"
                )
                remainder = position.quantity
                self.storage.close_position(
                    position.id, fill, self.as_of, reason
                )
                position.status = PositionStatus.CLOSED
                position.exit_price = fill
                position.exit_date = self.as_of
                position.exit_reason = reason
                return trimmed_total + remainder, banked_total
            # Partial trim: bank the P/L on the sold contracts, keep the rest.
            banked_add = (fill - position.entry_price) * 100 * contracts
            new_qty = position.quantity - contracts
            new_banked = round(position.realized_pl_banked + banked_add, 2)
            new_trims_done = position.trims_done + 1
            self.storage.reduce_position(
                position.id, new_qty, new_banked, new_trims_done
            )
            position.quantity = new_qty
            position.realized_pl_banked = new_banked
            position.trims_done = new_trims_done
            trimmed_total += contracts
            banked_total += banked_add
        return trimmed_total, round(banked_total, 2)

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

        # Scale out on strength first: bank partial profit before checking the
        # terminal exit on whatever runner remains.
        trimmed, banked = self._maybe_trim(position, contract)
        if position.status is PositionStatus.CLOSED:
            return ManageResult(
                position,
                position.exit_price,
                True,
                position.exit_reason or "trim closed runner",
                trimmed=trimmed,
                banked=banked,
            )

        # Ratchet the high-water mark up as the mid makes new peaks; this feeds
        # the trailing stop. Persist only when it actually advances.
        prev_hwm = position.high_water_mark or position.entry_price
        if contract.mid > prev_hwm:
            position.high_water_mark = contract.mid
            self.storage.update_high_water_mark(position.id, contract.mid)

        # Exit rules trigger on the current market mid; the actual fill is at
        # the (worse) cost-adjusted exit price.
        reason = self.exit_reason(position, contract.mid)
        if reason is not None:
            fill = self._exit_price(contract)
            self.storage.close_position(position.id, fill, self.as_of, reason)
            return ManageResult(
                position, fill, True, reason, trimmed=trimmed, banked=banked
            )
        hold_reason = "hold" if trimmed == 0 else f"trimmed {trimmed}"
        return ManageResult(
            position, contract.mid, False, hold_reason,
            trimmed=trimmed, banked=banked,
        )

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
        closed = sum(
            p.realized_pl() or 0.0 for p in self.storage.closed_positions()
        )
        # Profit already banked from partial exits on still-open runners counts
        # as realized too.
        banked_open = sum(
            p.realized_pl_banked for p in self.storage.open_positions()
        )
        return round(closed + banked_open, 2)
