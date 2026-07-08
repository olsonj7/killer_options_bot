"""Deterministic offline data source for development and tests.

Generates plausible quotes and option chains without any network access so the
scanner and paper engine can be exercised end-to-end.

Design notes for repeatability across dates:
- Strikes are anchored to a *stable* per-symbol base price so that OCC option
  symbols do not change as ``as_of`` advances. This lets a position opened on
  one date be re-priced on a later date (the symbol still matches).
- The underlying "last" price *does* drift with ``as_of`` so option values move
  over time and exit rules can trigger during a paper simulation.
- Expirations are fixed calendar dates (the 15th of each month), filtered to
  those still in the future relative to ``as_of``.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta

from killer_options_bot.models import OptionContract, Quote, Side

# Anchor month used to generate a stable ladder of monthly expirations.
_ANCHOR = date(2026, 1, 15)


def _seed(symbol: str) -> int:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _base_price(symbol: str) -> float:
    """Stable reference price for a symbol (drives the strike ladder)."""
    return 50 + (_seed(symbol) % 400)  # roughly 50-450


class MockMarketData:
    """A repeatable, network-free MarketData implementation."""

    def __init__(self, as_of: date | None = None):
        self.as_of = as_of or date.today()

    # --- Underlying --------------------------------------------------------

    def _price_on(self, symbol: str, day: date) -> float:
        seed = _seed(symbol)
        base = _base_price(symbol)
        t = (day - _ANCHOR).days
        drift = ((seed % 7) - 3) / 1000.0  # small per-symbol daily drift
        wobble = math.sin((seed % 13) + t / 9.0) * (base * 0.03)
        return round(max(1.0, base * (1 + drift * t) + wobble), 2)

    def _current_price(self, symbol: str) -> float:
        return self._price_on(symbol, self.as_of)

    def get_quote(self, symbol: str) -> Quote:
        # Reconstruct recent closes ending at the current price.
        closes: list[float] = []
        for back in range(40, -1, -1):
            closes.append(self._price_on(symbol, self.as_of - timedelta(days=back)))
        last = closes[-1]
        return Quote(symbol=symbol, last=last, closes=closes)

    # --- Options -----------------------------------------------------------

    def _expirations(self) -> list[date]:
        """Monthly expirations (15th) still in the future relative to as_of."""
        result: list[date] = []
        for offset in range(-3, 12):
            month_index = (_ANCHOR.month - 1) + offset
            year = _ANCHOR.year + month_index // 12
            month = month_index % 12 + 1
            exp = date(year, month, 15)
            if exp > self.as_of:
                result.append(exp)
        return result

    def get_option_chain(
        self, symbol: str, side: Side
    ) -> list[OptionContract]:
        last = self._current_price(symbol)
        base = _base_price(symbol)  # stable strike anchor
        seed = _seed(symbol)
        contracts: list[OptionContract] = []

        for expiration in self._expirations():
            dte = (expiration - self.as_of).days
            for k, offset in enumerate((-0.10, -0.05, 0.0, 0.05, 0.10)):
                # Strikes anchored to the STABLE base price -> stable symbols.
                strike = round(base * (1 + offset), 1)

                if side is Side.CALL:
                    moneyness = (last - strike) / last
                    intrinsic = max(0.0, last - strike)
                else:
                    moneyness = (strike - last) / last
                    intrinsic = max(0.0, strike - last)

                delta = max(0.05, min(0.9, 0.5 + moneyness * 4))
                time_value = base * 0.02 * (max(dte, 0) / 45.0)
                mid = round(max(0.05, intrinsic + time_value), 2)
                half_spread = round(mid * 0.03, 2)
                bid = round(max(0.01, mid - half_spread), 2)
                ask = round(mid + half_spread, 2)

                volume = 50 + ((seed + k * 37) % 900)
                open_interest = 200 + ((seed + k * 53) % 4000)
                iv = round(0.20 + ((seed + k) % 30) / 100.0, 4)

                occ = (
                    f"{symbol}{expiration:%y%m%d}"
                    f"{'C' if side is Side.CALL else 'P'}"
                    f"{int(strike * 1000):08d}"
                )
                contracts.append(
                    OptionContract(
                        symbol=occ,
                        underlying=symbol,
                        side=side,
                        strike=strike,
                        expiration=expiration,
                        bid=bid,
                        ask=ask,
                        last=mid,
                        delta=round(delta if side is Side.CALL else -delta, 3),
                        implied_volatility=iv,
                        volume=volume,
                        open_interest=open_interest,
                    )
                )
        return contracts


class MockBroker:
    """A network-free ``OrderBroker`` for tests and dry runs.

    Records every request and returns a deterministic accepted result. It never
    contacts a real brokerage.
    """

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self._counter = 0

    def place_option_limit(
        self,
        *,
        option_symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        dry_run: bool,
    ):
        from killer_options_bot.brokers.base import OrderResult

        self._counter += 1
        self.orders.append(
            {
                "option_symbol": option_symbol,
                "side": side,
                "quantity": quantity,
                "limit_price": limit_price,
                "dry_run": dry_run,
            }
        )
        order_id = None if dry_run else f"MOCK-{self._counter}"
        detail = "preview" if dry_run else "submitted"
        return OrderResult(
            accepted=True,
            order_id=order_id,
            detail=detail,
            dry_run=dry_run,
        )

