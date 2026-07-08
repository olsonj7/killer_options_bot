"""Withdrawal advisor: recommend when to take cash off the table.

This module is **advisory only**. It never moves money and has no banking
authority — it computes recommendations from your realized trade log so you can
decide. Everything is driven by :class:`WithdrawConfig`.

Equity here means ``starting_capital + realized P/L``. Unrealized (open) gains
are deliberately excluded: you can only withdraw cash you've actually banked,
and open marks can evaporate. The "high-water mark" (peak equity) is derived
deterministically from the closed-trade history, so no extra state is stored.

Rules (each independent, each inactive when its knob is 0):

- **profit_skim**   – on a new equity high, move a fraction of gains to savings.
- **milestone**     – when equity reaches a configured level, withdraw a set sum.
- **tax_reserve**   – set aside a fraction of realized profit for taxes.
- **drawdown_defense** – defensive: in a deep drawdown, pull capital out of harm.

Profit rules never recommend dipping below ``min_trading_balance`` (so the
account can keep trading). The defensive rule may, because its purpose is to
stop trading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from killer_options_bot.config import WithdrawConfig


@dataclass(frozen=True)
class Recommendation:
    """A single suggested action. ``amount`` is dollars to move."""

    kind: str  # profit_skim | milestone | tax_reserve | drawdown_defense
    amount: float
    reason: str


@dataclass(frozen=True)
class WithdrawAdvice:
    """The full advisor result for a point in time."""

    enabled: bool
    equity: float
    peak_equity: float
    starting_capital: float
    recommendations: list[Recommendation] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return round(self.equity - self.starting_capital, 2)

    @property
    def drawdown_pct(self) -> float:
        """Current equity's drop below the peak, as a fraction (0..1)."""
        if self.peak_equity <= 0:
            return 0.0
        return round(max(0.0, (self.peak_equity - self.equity) / self.peak_equity), 4)

    @property
    def has_action(self) -> bool:
        return bool(self.recommendations)


def compute_equity(
    starting_capital: float, realized_series: Sequence[float]
) -> tuple[float, float]:
    """Return ``(current_equity, peak_equity)`` from a realized-P/L series.

    ``realized_series`` is per-trade realized P/L in exit order. The peak is the
    highest the equity curve ever reached (the high-water mark).
    """
    equity = starting_capital
    peak = starting_capital
    for pl in realized_series:
        equity += pl
        peak = max(peak, equity)
    return round(equity, 2), round(peak, 2)


def _cap(amount: float, equity: float, floor: float) -> float:
    """Clamp a withdrawal so ``equity - amount`` never drops below ``floor``."""
    room = equity - floor
    return round(max(0.0, min(amount, room)), 2)


def advise(
    cfg: WithdrawConfig, equity: float, peak_equity: float
) -> WithdrawAdvice:
    """Compute withdrawal recommendations for the given equity snapshot."""
    recs: list[Recommendation] = []
    if not cfg.enabled:
        return WithdrawAdvice(
            enabled=False,
            equity=round(equity, 2),
            peak_equity=round(peak_equity, 2),
            starting_capital=cfg.starting_capital,
        )

    gain = equity - cfg.starting_capital
    at_new_high = equity >= peak_equity - 1e-9
    floor = cfg.min_trading_balance

    # 1) Profit skim — lock a slice of winnings, but only on a fresh high so we
    #    don't repeatedly nag while equity chops sideways below the peak.
    if cfg.skim_pct > 0 and gain > 0 and at_new_high:
        amount = _cap(cfg.skim_pct * gain, equity, floor)
        if amount > 0:
            recs.append(
                Recommendation(
                    kind="profit_skim",
                    amount=amount,
                    reason=(
                        f"New equity high ${equity:,.2f} (+${gain:,.2f}). "
                        f"Skim {cfg.skim_pct:.0%} of gains to lock winnings; "
                        f"keep ${equity - amount:,.2f} trading."
                    ),
                )
            )

    # 2) Milestone ladder — report the highest level reached; lower rungs are
    #    assumed already actioned.
    reached = [m for m in cfg.milestones if equity >= m[0]]
    if reached:
        level, payout = max(reached, key=lambda m: m[0])
        amount = _cap(payout, equity, floor)
        if amount > 0:
            recs.append(
                Recommendation(
                    kind="milestone",
                    amount=amount,
                    reason=(
                        f"Equity reached ${level:,.2f} milestone. "
                        f"Withdraw ${amount:,.2f}; "
                        f"keep ${equity - amount:,.2f} trading."
                    ),
                )
            )

    # 3) Tax reserve — set aside (not spend) a slice of realized profit. This is
    #    a separate bucket, so it isn't bounded by the trading floor.
    if cfg.tax_reserve_pct > 0 and gain > 0:
        amount = round(cfg.tax_reserve_pct * gain, 2)
        if amount > 0:
            recs.append(
                Recommendation(
                    kind="tax_reserve",
                    amount=amount,
                    reason=(
                        f"Realized profit ${gain:,.2f} is taxable (short-term). "
                        f"Reserve {cfg.tax_reserve_pct:.0%} = ${amount:,.2f} in a "
                        f"separate bucket for taxes."
                    ),
                )
            )

    # 4) Drawdown defense — the opposite of profit-taking. In a deep drawdown,
    #    pull capital out of harm's way. Deliberately allowed below the floor.
    if cfg.drawdown_trigger_pct > 0 and peak_equity > 0:
        dd = (peak_equity - equity) / peak_equity
        if dd >= cfg.drawdown_trigger_pct and cfg.drawdown_defense_pct > 0:
            amount = _cap(cfg.drawdown_defense_pct * equity, equity, 0.0)
            if amount > 0:
                recs.append(
                    Recommendation(
                        kind="drawdown_defense",
                        amount=amount,
                        reason=(
                            f"Drawdown {dd:.0%} from peak ${peak_equity:,.2f}. "
                            f"De-risk: move ${amount:,.2f} to cash to preserve "
                            f"capital."
                        ),
                    )
                )

    return WithdrawAdvice(
        enabled=True,
        equity=round(equity, 2),
        peak_equity=round(peak_equity, 2),
        starting_capital=cfg.starting_capital,
        recommendations=recs,
    )


def advise_from_storage(cfg: WithdrawConfig, storage) -> WithdrawAdvice:
    """Compute advice from a storage backend's closed-position log."""
    closed = storage.closed_positions()
    closed = sorted(
        closed,
        key=lambda p: (p.exit_date or p.entry_date, p.id or 0),
    )
    series = [p.realized_pl() or 0.0 for p in closed]
    equity, peak = compute_equity(cfg.starting_capital, series)
    return advise(cfg, equity, peak)
