"""Guarded live-execution scaffolding.

This module is the ONLY place that can place real broker orders, and it is
disabled by default. The whole project is paper-first; live trading exists
here only behind multiple, independent safety gates:

1. ``config.trading_mode`` must be ``"live"``.
2. ``config.live.enabled`` must be true.
3. A kill-switch file (``config.live.kill_switch_file``) must NOT exist. Create
   that file at any time to immediately block all new live orders.
4. Daily and weekly realized-loss lockouts (``config.live.max_daily_loss`` /
   ``max_weekly_loss``) must not be breached.
5. Order size must be <= ``config.live.max_contracts_per_order``.
6. The caller must pass ``confirm_live=True`` explicitly. Without it, orders
   run as a non-transmitting dry run (preview only).
7. Orders are LIMIT orders only. Market orders are never sent.

If any gate fails, opening raises ``LiveGuardError`` (or ``KillSwitchError``)
and no order is placed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from killer_options_bot.brokers.base import MarketData, OrderBroker, OrderResult
from killer_options_bot.config import Config
from killer_options_bot.models import (
    Candidate,
    OptionContract,
    PaperPosition,
    PositionStatus,
    Side,
)
from killer_options_bot.storage import BaseStorage


class LiveGuardError(RuntimeError):
    """A live guardrail blocked the action."""


class KillSwitchError(LiveGuardError):
    """The kill-switch file is present; all live orders are blocked."""


@dataclass
class LiveOpenResult:
    accepted: bool
    reason: str
    order: OrderResult | None = None
    position: PaperPosition | None = None


def _find_contract(
    data: MarketData, underlying: str, side: Side, option_symbol: str
) -> OptionContract | None:
    for contract in data.get_option_chain(underlying, side):
        if contract.symbol == option_symbol:
            return contract
    return None


class LiveGuard:
    """Evaluates every independent safety gate before a live order."""

    def __init__(self, config: Config, storage: BaseStorage):
        self.config = config
        self.storage = storage

    def kill_switch_active(self) -> bool:
        return self.config.live.kill_switch_file.exists()

    def check(self, *, quantity: int, as_of: date, confirm_live: bool) -> None:
        """Raise if any gate fails. Returns None when it is safe to proceed
        (as a dry run when ``confirm_live`` is False)."""
        if self.config.trading_mode != "live":
            raise LiveGuardError(
                "trading_mode is not 'live'; refusing to place live orders."
            )
        if not self.config.live.enabled:
            raise LiveGuardError(
                "live.enabled is false; live trading is turned off."
            )
        if self.kill_switch_active():
            raise KillSwitchError(
                f"kill switch present ({self.config.live.kill_switch_file}); "
                "remove it to allow live orders."
            )
        if quantity < 1:
            raise LiveGuardError("quantity must be >= 1.")
        if quantity > self.config.live.max_contracts_per_order:
            raise LiveGuardError(
                f"quantity {quantity} exceeds max_contracts_per_order "
                f"({self.config.live.max_contracts_per_order})."
            )

        # Loss lockouts (realized losses are negative dollar amounts).
        daily = self.storage.realized_pl_since(as_of, mode="live")
        if daily <= -abs(self.config.live.max_daily_loss):
            raise LiveGuardError(
                f"daily loss lockout: realized {daily:+.2f} <= "
                f"-{self.config.live.max_daily_loss:.2f}."
            )
        weekly = self.storage.realized_pl_since(
            as_of - timedelta(days=7), mode="live"
        )
        if weekly <= -abs(self.config.live.max_weekly_loss):
            raise LiveGuardError(
                f"weekly loss lockout: realized {weekly:+.2f} <= "
                f"-{self.config.live.max_weekly_loss:.2f}."
            )


class LiveEngine:
    """Opens live positions through a broker, behind ``LiveGuard``.

    Order placement is a limit order at the contract ask (buy-to-open). When
    ``confirm_live`` is False the broker is asked for a dry-run preview only and
    nothing is transmitted.
    """

    def __init__(
        self,
        config: Config,
        data: MarketData,
        storage: BaseStorage,
        broker: OrderBroker,
        as_of: date | None = None,
    ):
        self.config = config
        self.data = data
        self.storage = storage
        self.broker = broker
        self.guard = LiveGuard(config, storage)
        self.as_of = as_of or date.today()

    def open_from_candidate(
        self,
        candidate: Candidate,
        quantity: int = 1,
        confirm_live: bool = False,
    ) -> LiveOpenResult:
        if not candidate.decision.allowed:
            return LiveOpenResult(False, "candidate not allowed by risk engine")

        # Guardrails first — raises on any failure.
        self.guard.check(
            quantity=quantity, as_of=self.as_of, confirm_live=confirm_live
        )

        if self.storage.count_open_positions() >= self.config.risk.max_open_positions:
            return LiveOpenResult(False, "max open positions reached")
        if self.storage.has_open_position(candidate.contract.symbol):
            return LiveOpenResult(False, "already holding this contract")

        c = candidate.contract
        # Limit at the ask for a marketable-but-capped buy. Never a market order.
        limit_price = c.ask if c.ask > 0 else c.mid
        dry_run = not confirm_live

        order = self.broker.place_option_limit(
            option_symbol=c.symbol,
            side="buy_to_open",
            quantity=quantity,
            limit_price=round(limit_price, 2),
            dry_run=dry_run,
        )
        if not order.accepted:
            return LiveOpenResult(False, f"broker rejected: {order.detail}", order)

        if dry_run:
            return LiveOpenResult(
                True, "dry-run preview only; no order transmitted", order
            )

        position = PaperPosition(
            option_symbol=c.symbol,
            underlying=c.underlying,
            side=candidate.side,
            strike=c.strike,
            expiration=c.expiration,
            quantity=quantity,
            entry_price=limit_price,
            entry_date=self.as_of,
            status=PositionStatus.OPEN,
        )
        self.storage.open_position(
            position, mode="live", broker_order_id=order.order_id
        )
        return LiveOpenResult(
            True, f"live order placed (id={order.order_id})", order, position
        )
