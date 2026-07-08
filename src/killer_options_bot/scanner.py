"""The scanner: turn market data + signals + risk into logged candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.config import Config, StrategyConfig
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


#: Entry-signal dispatch by ``StrategyConfig.signal`` name. New signal types
#: (e.g. an intraday/breakout signal for 0DTE) can be registered here.
_SIGNALS = {"momentum": momentum_signal}


def _signal_for(strategy: StrategyConfig, quote: Quote, cfg: Config) -> Signal:
    return _SIGNALS.get(strategy.signal, momentum_signal)(quote, cfg)


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
        self.as_of = as_of or date.today()

    def scan_symbol_strategy(
        self, symbol: str, strategy: StrategyConfig
    ) -> Candidate | None:
        """Scan one symbol under one strategy profile.

        The strategy's own filters/exits drive contract selection and risk, so
        a 0DTE scalp and a LEAPS hold evaluate independently on the same name.
        """
        # A per-strategy view of the config so RiskEngine and contract picking
        # use this strategy's DTE/delta window instead of the base one.
        scfg = replace(self.config, filters=strategy.filters, exits=strategy.exits)

        quote = self.data.get_quote(symbol)
        signal = _signal_for(strategy, quote, scfg)
        if signal.side is None:
            return None

        chain = self.data.get_option_chain(symbol, signal.side)
        contract = _best_contract(chain, scfg, self.as_of)
        if contract is None:
            return None

        risk = RiskEngine(scfg)
        decision = risk.evaluate(
            contract,
            trades_this_week=self.storage.trades_in_trailing_week(self.as_of),
            as_of=self.as_of,
        )
        note = signal.note
        if strategy.name != "default":
            note = f"[{strategy.name}] {note}"
        candidate = Candidate(
            contract=contract,
            side=signal.side,
            signal_note=note,
            decision=decision,
            max_loss=risk.max_loss(contract),
            strategy=strategy.name,
        )
        self.storage.record_candidate(candidate)
        return candidate

    def scan_symbol(self, symbol: str) -> Candidate | None:
        """Scan a symbol under the first active strategy (compat helper)."""
        for strategy in self.config.active_strategies:
            candidate = self.scan_symbol_strategy(symbol, strategy)
            if candidate is not None:
                return candidate
        return None

    def scan(self) -> list[Candidate]:
        results: list[Candidate] = []
        for strategy in self.config.active_strategies:
            for symbol in self.config.watchlist:
                candidate = self.scan_symbol_strategy(symbol, strategy)
                if candidate is not None:
                    results.append(candidate)
        return results

    def scan_strategy(self, strategy: StrategyConfig) -> list[Candidate]:
        """Scan the whole watchlist under a single strategy profile.

        Used by the run loop, which paces each strategy's entry scans on its
        own ``scan_interval_minutes`` while managing exits every tick.
        """
        results: list[Candidate] = []
        for symbol in self.config.watchlist:
            candidate = self.scan_symbol_strategy(symbol, strategy)
            if candidate is not None:
                results.append(candidate)
        return results
