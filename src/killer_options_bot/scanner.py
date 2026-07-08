"""The scanner: turn market data + signals + risk into logged candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.config import Config
from killer_options_bot.indicators import rsi, sma
from killer_options_bot.models import Candidate, OptionContract, Quote, Side
from killer_options_bot.risk import RiskEngine
from killer_options_bot.storage import Storage


@dataclass
class Signal:
    side: Side | None
    note: str


def momentum_signal(quote: Quote, cfg: Config) -> Signal:
    """Very simple momentum gate.

    Long call when price is above its SMA and RSI sits in a healthy band.
    Long put when price is below its SMA and RSI is weak.
    Otherwise, no trade.
    """
    s = cfg.signal
    ma = sma(quote.closes, s.sma_period)
    r = rsi(quote.closes, s.rsi_period)
    if ma is None or r is None:
        return Signal(None, "Insufficient history for signal")

    if quote.last > ma and s.rsi_min <= r <= s.rsi_max:
        return Signal(
            Side.CALL,
            f"Bullish: last {quote.last:.2f} > SMA{s.sma_period} {ma:.2f}, "
            f"RSI {r:.1f} in [{s.rsi_min:.0f},{s.rsi_max:.0f}]",
        )
    if quote.last < ma and r < s.rsi_min:
        return Signal(
            Side.PUT,
            f"Bearish: last {quote.last:.2f} < SMA{s.sma_period} {ma:.2f}, "
            f"RSI {r:.1f} < {s.rsi_min:.0f}",
        )
    return Signal(
        None,
        f"No edge: last {quote.last:.2f} vs SMA{s.sma_period} {ma:.2f}, "
        f"RSI {r:.1f}",
    )


def _best_contract(
    contracts: list[OptionContract], cfg: Config, as_of: date
) -> OptionContract | None:
    """Pick the in-band contract whose delta is closest to the band midpoint."""
    f = cfg.filters
    target_delta = (f.min_delta + f.max_delta) / 2
    in_band = [
        c
        for c in contracts
        if f.min_dte <= c.dte(as_of) <= f.max_dte
        and f.min_delta <= abs(c.delta) <= f.max_delta
    ]
    pool = in_band or contracts
    if not pool:
        return None
    return min(pool, key=lambda c: abs(abs(c.delta) - target_delta))


class Scanner:
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
        self.risk = RiskEngine(config)
        self.as_of = as_of or date.today()

    def scan_symbol(self, symbol: str) -> Candidate | None:
        quote = self.data.get_quote(symbol)
        signal = momentum_signal(quote, self.config)
        if signal.side is None:
            return None

        chain = self.data.get_option_chain(symbol, signal.side)
        contract = _best_contract(chain, self.config, self.as_of)
        if contract is None:
            return None

        decision = self.risk.evaluate(
            contract,
            trades_this_week=self.storage.trades_in_trailing_week(self.as_of),
            as_of=self.as_of,
        )
        candidate = Candidate(
            contract=contract,
            side=signal.side,
            signal_note=signal.note,
            decision=decision,
            max_loss=self.risk.max_loss(contract),
        )
        self.storage.record_candidate(candidate)
        return candidate

    def scan(self) -> list[Candidate]:
        results: list[Candidate] = []
        for symbol in self.config.watchlist:
            candidate = self.scan_symbol(symbol)
            if candidate is not None:
                results.append(candidate)
        return results
