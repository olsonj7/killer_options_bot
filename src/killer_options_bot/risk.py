"""The risk engine: hard guardrails evaluated before any trade is logged.

Every rule returns a rejection reason. The engine collects *all* failing
reasons so the log explains exactly why a contract was refused. For a small
account the default posture is to reject; a contract must clear every rule.
"""

from __future__ import annotations

from datetime import date

from killer_options_bot.config import Config
from killer_options_bot.models import OptionContract, RiskDecision


class RiskEngine:
    def __init__(self, config: Config):
        self.config = config

    def evaluate(
        self,
        contract: OptionContract,
        *,
        trades_this_week: int,
        as_of: date | None = None,
    ) -> RiskDecision:
        """Return a RiskDecision. ``allowed`` is True only if all rules pass."""
        reasons: list[str] = []
        cfg = self.config
        f = cfg.filters

        # --- Weekly cadence -------------------------------------------------
        if trades_this_week >= cfg.risk.max_trades_per_week:
            reasons.append(
                f"Weekly trade limit reached "
                f"({trades_this_week}/{cfg.risk.max_trades_per_week})"
            )

        # --- Position sizing vs account ------------------------------------
        max_trade_risk = cfg.account_value * cfg.risk.max_trade_risk_pct
        # For long options the max loss is the full debit paid.
        if contract.cost > max_trade_risk:
            reasons.append(
                f"Contract cost ${contract.cost:.2f} exceeds max trade risk "
                f"${max_trade_risk:.2f} ({cfg.risk.max_trade_risk_pct:.0%} of "
                f"${cfg.account_value:.2f})"
            )

        # --- Days to expiration --------------------------------------------
        dte = contract.dte(as_of)
        if dte < f.min_dte or dte > f.max_dte:
            reasons.append(
                f"DTE {dte} outside range [{f.min_dte}, {f.max_dte}]"
            )

        # --- Delta band -----------------------------------------------------
        abs_delta = abs(contract.delta)
        if not (f.min_delta <= abs_delta <= f.max_delta):
            reasons.append(
                f"Delta {abs_delta:.2f} outside band "
                f"[{f.min_delta:.2f}, {f.max_delta:.2f}]"
            )

        # --- Liquidity: spread, volume, open interest ----------------------
        if contract.spread_pct > f.max_spread_pct:
            reasons.append(
                f"Bid/ask spread {contract.spread_pct:.0%} exceeds max "
                f"{f.max_spread_pct:.0%}"
            )
        if contract.volume < f.min_volume:
            reasons.append(
                f"Volume {contract.volume} below min {f.min_volume}"
            )
        if contract.open_interest < f.min_open_interest:
            reasons.append(
                f"Open interest {contract.open_interest} below min "
                f"{f.min_open_interest}"
            )

        # --- Sanity: a real, priced contract -------------------------------
        if contract.mid <= 0:
            reasons.append("Non-positive mid price")

        if reasons:
            return RiskDecision(allowed=False, reasons=reasons)
        return RiskDecision.accept()

    def max_loss(self, contract: OptionContract) -> float:
        """Defined max loss for a long option is the debit paid."""
        return contract.cost
