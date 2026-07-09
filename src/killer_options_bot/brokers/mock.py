"""Deterministic offline data source for development and tests.

Generates plausible quotes and option chains without any network access so the
scanner and paper engine can be exercised end-to-end.

Option prices and deltas come from a real Black-Scholes model (py_vollib), so
theoretical values, put/call parity, and delta behaviour are internally
consistent rather than hand-approximated. Each strike carries a synthesized
implied volatility (a mild smile plus equity skew) and a constant risk-free
rate (``RISK_FREE_RATE``).

Design notes for repeatability across dates:
- Strikes are anchored to a *stable* per-symbol base price so that OCC option
  symbols do not change as ``as_of`` advances. This lets a position opened on
  one date be re-priced on a later date (the symbol still matches).
- The underlying "last" price *does* drift with ``as_of`` so option values move
  over time and exit rules can trigger during a paper simulation.
- Expirations are fixed calendar dates (weekly Fridays + monthly 15ths),
  filtered to those still in the future relative to ``as_of``.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta

from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta

from killer_options_bot.models import OptionContract, Quote, Side

# Anchor month used to generate a stable ladder of monthly expirations.
_ANCHOR = date(2026, 1, 15)

# Assumed constant risk-free rate for Black-Scholes pricing of mock chains.
RISK_FREE_RATE = 0.04


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

    def get_intraday_closes(
        self, symbol: str, interval: str = "5min"
    ) -> list[float]:
        """Synthetic intraday bars for the current session, oldest first.

        Deterministic per symbol/date so mock-mode 0DTE runs and tests are
        repeatable. Produces a gentle intraday drift plus a small oscillation
        around today's price so the intraday momentum signal has something to
        react to. Bar count scales with the interval (a full 6.5h session).
        """
        per_hour = {"1min": 60, "5min": 12, "15min": 4}.get(interval, 12)
        bars = max(2, int(6.5 * per_hour))
        seed = _seed(symbol)
        last = self._current_price(symbol)
        direction = ((seed % 5) - 2) / 1000.0  # per-symbol intraday drift
        out: list[float] = []
        for i in range(bars):
            frac = i / (bars - 1) if bars > 1 else 1.0
            drift = direction * last * frac
            wobble = math.sin((seed % 17) + frac * 6.28) * (last * 0.004)
            out.append(round(max(0.5, last + drift + wobble), 2))
        return out

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

                flag = "c" if side is Side.CALL else "p"

                # Per-strike implied volatility with a mild smile + equity skew,
                # so the chain isn't perfectly flat: the base level varies by
                # symbol, IV rises for strikes far from the money (smile), and
                # downside strikes carry a little extra premium (skew).
                base_iv = 0.20 + (seed % 20) / 100.0  # 0.20 - 0.39
                log_m = math.log(strike / last) if last > 0 else 0.0
                smile = 0.6 * (log_m * log_m)
                skew = 0.05 * max(0.0, -log_m)
                iv = round(min(2.0, max(0.05, base_iv + smile + skew)), 4)

                # Black-Scholes price + delta (py_vollib). ``t`` is year
                # fraction to expiry; at/after expiry only intrinsic remains.
                t = max(dte, 0) / 365.0
                if t <= 0:
                    if side is Side.CALL:
                        mid = max(0.0, last - strike)
                    else:
                        mid = max(0.0, strike - last)
                    if mid <= 0:
                        delta = 0.0
                    else:
                        delta = 1.0 if side is Side.CALL else -1.0
                else:
                    mid = black_scholes(flag, last, strike, t, RISK_FREE_RATE, iv)
                    delta = bs_delta(flag, last, strike, t, RISK_FREE_RATE, iv)

                mid = round(max(0.01, mid), 2)
                delta = round(delta, 3)

                half_spread = round(mid * 0.03, 2)
                bid = round(max(0.01, mid - half_spread), 2)
                ask = round(mid + half_spread, 2)

                volume = 50 + ((seed + k * 37) % 900)
                open_interest = 200 + ((seed + k * 53) % 4000)

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

