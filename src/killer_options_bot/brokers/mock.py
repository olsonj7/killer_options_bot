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
        slope = ((seed % 7) - 3) / 1000.0  # per-symbol drift direction
        # Bounded trend (+/-12% of base): real indices don't trend in a straight
        # line, and this keeps ``last`` near the stable strike ladder so in-band
        # OTM strikes always exist on both sides.
        trend = math.tanh(slope * t * 4) * (base * 0.12)
        wobble = math.sin((seed % 13) + t / 9.0) * (base * 0.03)
        return round(max(1.0, base + trend + wobble), 2)

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
        """Upcoming expirations: weekly Fridays plus monthly (15th) dates.

        Weekly Fridays for the next ~8 weeks enable 0-7 DTE (weekly) strategies;
        the monthly ladder remains for longer-dated swing trades. All values are
        absolute calendar dates, so a given option's OCC symbol stays stable as
        ``as_of`` advances and an open position can be re-priced later.
        """
        result: set[date] = set()

        # Weekly Fridays (weekday(): Mon=0 ... Fri=4).
        days_ahead = (4 - self.as_of.weekday()) % 7
        first_friday = self.as_of + timedelta(days=days_ahead)
        for week in range(8):
            exp = first_friday + timedelta(weeks=week)
            if exp > self.as_of:
                result.add(exp)

        # Monthly (15th) expirations for longer-dated strategies.
        for offset in range(-3, 12):
            month_index = (_ANCHOR.month - 1) + offset
            year = _ANCHOR.year + month_index // 12
            month = month_index % 12 + 1
            exp = date(year, month, 15)
            if exp > self.as_of:
                result.add(exp)

        return sorted(result)

    def get_option_chain(
        self, symbol: str, side: Side
    ) -> list[OptionContract]:
        last = self._current_price(symbol)
        base = _base_price(symbol)  # stable strike anchor
        seed = _seed(symbol)
        contracts: list[OptionContract] = []

        for expiration in self._expirations():
            dte = (expiration - self.as_of).days
            # Fine strike ladder (+/-14% in 1% steps). Anchored to the stable
            # base price, it must be wide enough that in-band (delta) strikes
            # still exist after ``last`` has drifted, and fine enough that a
            # near-target-delta strike is available for short-dated options.
            offsets = [round(-0.14 + 0.01 * i, 2) for i in range(29)]
            for k, offset in enumerate(offsets):
                # Strikes anchored to the STABLE base price -> stable symbols.
                strike = round(base * (1 + offset), 1)

                # ``moneyness`` here is signed ITM-ness for THIS side: positive
                # when the option is in the money, negative when out.
                if side is Side.CALL:
                    moneyness = (last - strike) / last
                else:
                    moneyness = (strike - last) / last
                intrinsic = max(0.0, moneyness) * last

                iv = 0.20 + ((seed + k) % 30) / 100.0  # 0.20 - 0.49

                # Expected move (standard deviation of return) to expiration.
                # Extrinsic value follows an ATM-straddle approximation: it peaks
                # at the money, decays for OTM/ITM strikes, and shrinks with time
                # (~sqrt(dte)) for realistic theta. Because it depends on
                # moneyness, a correct directional move in the underlying
                # actually increases the option's value (real delta behaviour).
                sigma_move = max(iv * math.sqrt(max(dte, 0) / 365.0), 1e-4)
                z = moneyness / sigma_move
                extrinsic = last * sigma_move * 0.3989 * math.exp(-0.5 * z * z)
                mid = round(max(0.01, intrinsic + extrinsic), 2)

                # Delta: smooth 0..1 through 0.5 ATM, consistent with pricing.
                abs_delta = 0.5 + 0.5 * math.tanh(1.1 * z)
                delta = round(abs_delta if side is Side.CALL else -abs_delta, 3)

                half_spread = round(mid * 0.03, 2)
                bid = round(max(0.01, mid - half_spread), 2)
                ask = round(mid + half_spread, 2)

                volume = 50 + ((seed + k * 37) % 900)
                open_interest = 200 + ((seed + k * 53) % 4000)
                iv = round(iv, 4)

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
                        delta=delta,
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

