"""The scanner: turn market data + signals + risk into logged candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.config import Config, StrategyConfig
from killer_options_bot.indicators import rsi, sma
from killer_options_bot.models import Bar, Candidate, OptionContract, Quote, Side
from killer_options_bot.risk import RiskEngine
from killer_options_bot.storage import Storage


@dataclass
class Signal:
    side: Side | None
    note: str


def _sma_slope_ok(
    values: list[float], period: int, lookback: int, *, up: bool
) -> bool:
    """True if the SMA is sloping the desired way over ``lookback`` bars.

    ``lookback <= 0`` disables the check (always True). Otherwise compare the
    current SMA to the SMA ``lookback`` bars ago: for ``up`` it must be higher
    (rising trend), for a down trade it must be lower. If there is not enough
    history to measure the older SMA the check fails closed (no trade).
    """
    if lookback <= 0:
        return True
    now = sma(values, period)
    past = sma(values[:-lookback], period)
    if now is None or past is None:
        return False
    return now > past if up else now < past


def momentum_signal(quote: Quote, cfg: Config) -> Signal:
    """Very simple momentum gate.

    Long call when price is above its SMA and RSI sits in a healthy band.
    Long put when price is below its SMA and RSI is weak.
    Otherwise, no trade.

    Two optional quality gates make it far pickier when configured:

    - ``trend_buffer_pct``: price must sit at least this fraction BEYOND the SMA
      (not merely on the right side of it), so borderline noise near the mean
      does not trigger a trade.
    - ``slope_lookback``: the SMA itself must be sloping in the trade direction
      over that many bars, so we only buy calls in a rising trend and puts in a
      falling one.
    """
    s = cfg.signal
    ma = sma(quote.closes, s.sma_period)
    r = rsi(quote.closes, s.rsi_period)
    if ma is None or r is None:
        return Signal(None, "Insufficient history for signal")

    upper = ma * (1 + s.trend_buffer_pct)
    lower = ma * (1 - s.trend_buffer_pct)
    if (
        quote.last > upper
        and s.rsi_min <= r <= s.rsi_max
        and _sma_slope_ok(quote.closes, s.sma_period, s.slope_lookback, up=True)
    ):
        return Signal(
            Side.CALL,
            f"Bullish: last {quote.last:.2f} > SMA{s.sma_period} {ma:.2f} "
            f"(+{s.trend_buffer_pct:.1%} buffer), RSI {r:.1f} in "
            f"[{s.rsi_min:.0f},{s.rsi_max:.0f}]",
        )
    if (
        quote.last < lower
        and r < s.rsi_min
        and _sma_slope_ok(quote.closes, s.sma_period, s.slope_lookback, up=False)
    ):
        return Signal(
            Side.PUT,
            f"Bearish: last {quote.last:.2f} < SMA{s.sma_period} {ma:.2f} "
            f"(-{s.trend_buffer_pct:.1%} buffer), RSI {r:.1f} < {s.rsi_min:.0f}",
        )
    return Signal(
        None,
        f"No edge: last {quote.last:.2f} vs SMA{s.sma_period} {ma:.2f}, "
        f"RSI {r:.1f}",
    )


def intraday_momentum_signal(quote: Quote, cfg: Config) -> Signal:
    """Momentum on intraday bars, for same-day (0DTE) trades.

    Identical in spirit to ``momentum_signal`` but evaluated on the current
    session's intraday bars (``quote.intraday``) with shorter SMA/RSI periods,
    so a same-day directional move is detectable within the session rather than
    off stale daily closes. Returns no trade when there is not yet enough
    intraday history (e.g. right after the open).
    """
    s = cfg.signal
    bars = quote.intraday
    ma = sma(bars, s.intraday_sma_period)
    r = rsi(bars, s.intraday_rsi_period)
    if ma is None or r is None:
        return Signal(
            None, f"Insufficient intraday history ({len(bars)} bars)"
        )
    price = bars[-1]
    upper = ma * (1 + s.trend_buffer_pct)
    lower = ma * (1 - s.trend_buffer_pct)
    if (
        price > upper
        and s.rsi_min <= r <= s.rsi_max
        and _sma_slope_ok(bars, s.intraday_sma_period, s.slope_lookback, up=True)
    ):
        return Signal(
            Side.CALL,
            f"Intraday bullish: {price:.2f} > SMA{s.intraday_sma_period} "
            f"{ma:.2f} (+{s.trend_buffer_pct:.1%}), RSI {r:.1f} in "
            f"[{s.rsi_min:.0f},{s.rsi_max:.0f}]",
        )
    if (
        price < lower
        and r < s.rsi_min
        and _sma_slope_ok(bars, s.intraday_sma_period, s.slope_lookback, up=False)
    ):
        return Signal(
            Side.PUT,
            f"Intraday bearish: {price:.2f} < SMA{s.intraday_sma_period} "
            f"{ma:.2f} (-{s.trend_buffer_pct:.1%}), RSI {r:.1f} < {s.rsi_min:.0f}",
        )
    return Signal(
        None,
        f"No intraday edge: {price:.2f} vs SMA{s.intraday_sma_period} "
        f"{ma:.2f}, RSI {r:.1f}",
    )


def strat_bar_type(bar: Bar, prev: Bar) -> str:
    """Classify ``bar`` relative to ``prev`` using TheSTRAT bar taxonomy.

    - ``"1"``  inside bar: does not exceed the prior bar's high or low.
    - ``"2up"`` directional up: takes out the prior high but not the prior low.
    - ``"2down"`` directional down: takes out the prior low but not the prior high.
    - ``"3"``  outside bar: takes out BOTH the prior high and low.
    """
    breaks_high = bar.high > prev.high
    breaks_low = bar.low < prev.low
    if breaks_high and breaks_low:
        return "3"
    if breaks_high:
        return "2up"
    if breaks_low:
        return "2down"
    return "1"


def _avg_range(bars: list[Bar], period: int) -> float | None:
    """Average high-low range of the last ``period`` bars (a simple ATR)."""
    if period <= 0 or len(bars) < period:
        return None
    window = bars[-period:]
    return sum(b.range for b in window) / period


def strat_breakout_signal(quote: Quote, cfg: Config) -> Signal:
    """TheSTRAT directional breakout, gated by displacement + prior-day bias.

    Distilled from the price-action course into three purely mechanical checks
    on OHLC bars (no discretionary chart reading):

    1. STRAT trigger: the last COMPLETED intraday bar is a directional "2" bar
       (``2up`` breaks the prior bar's high, ``2down`` breaks the prior low).
    2. Displacement: that bar's range clears ``strat_displacement_mult`` x the
       average range of the prior ``strat_atr_period`` bars, so we only act on
       an impulse candle, not noise.
    3. Prior-day bias (PDH/PDL): a ``2up`` only trades long when price sits in
       the upper half of the previous day's range (premium / bullish draw); a
       ``2down`` only trades short in the lower half. This keeps entries with
       the daily liquidity draw rather than fading it.
    """
    s = cfg.signal
    bars = quote.bars
    # Need the trigger bar, the bar before it (to type the trigger), and enough
    # history for the displacement baseline.
    needed = max(2, s.strat_atr_period + 1)
    if len(bars) < needed:
        return Signal(None, f"Insufficient STRAT bars ({len(bars)})")

    trigger = bars[-1]
    prev = bars[-2]
    bar_type = strat_bar_type(trigger, prev)
    if bar_type not in ("2up", "2down"):
        return Signal(None, f"No STRAT trigger: last bar is type {bar_type}")

    # Displacement: measure the average range of the bars BEFORE the trigger.
    avg_rng = _avg_range(bars[:-1], s.strat_atr_period)
    if avg_rng is None or avg_rng <= 0:
        return Signal(None, "Insufficient range history for displacement")
    threshold = s.strat_displacement_mult * avg_rng
    if trigger.range < threshold:
        return Signal(
            None,
            f"{bar_type} lacks displacement: range {trigger.range:.2f} "
            f"< {threshold:.2f}",
        )

    # Prior-day high/low bias. daily_bars[-1] is the most recently completed
    # session; without it we cannot establish bias, so decline.
    if not quote.daily_bars:
        return Signal(None, "No prior-day bar for PDH/PDL bias")
    prior_day = quote.daily_bars[-1]
    mid = prior_day.midpoint
    price = trigger.close

    if bar_type == "2up":
        if price < mid:
            return Signal(
                None,
                f"2up against bias: {price:.2f} below prior-day mid {mid:.2f}",
            )
        return Signal(
            Side.CALL,
            f"STRAT 2up breakout with displacement (range {trigger.range:.2f} "
            f">= {threshold:.2f}), price {price:.2f} above prior-day mid "
            f"{mid:.2f} [PDH {prior_day.high:.2f}]",
        )
    # 2down
    if price > mid:
        return Signal(
            None,
            f"2down against bias: {price:.2f} above prior-day mid {mid:.2f}",
        )
    return Signal(
        Side.PUT,
        f"STRAT 2down breakdown with displacement (range {trigger.range:.2f} "
        f">= {threshold:.2f}), price {price:.2f} below prior-day mid "
        f"{mid:.2f} [PDL {prior_day.low:.2f}]",
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
_SIGNALS = {
    "momentum": momentum_signal,
    "intraday_momentum": intraday_momentum_signal,
    "strat_breakout": strat_breakout_signal,
}

#: Signals that need the current session's intraday bar CLOSES on the quote.
_INTRADAY_SIGNALS = {"intraday_momentum"}

#: Signals that need OHLC bars (intraday + prior-day) attached to the quote.
_BAR_SIGNALS = {"strat_breakout"}


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
        # Intraday strategies (0DTE) need the current session's bars, which the
        # daily quote does not carry. Fetch them on demand when the data source
        # supports it; fall back to an empty list (signal then declines).
        if strategy.signal in _INTRADAY_SIGNALS:
            getter = getattr(self.data, "get_intraday_closes", None)
            bars: list[float] = []
            if getter is not None:
                bars = getter(symbol, scfg.signal.intraday_interval)
            quote = replace(quote, intraday=bars)
        # Price-action strategies (STRAT) need OHLC ranges: the current
        # session's intraday bars for bar typing/displacement plus recent daily
        # bars for prior-day high/low bias. Both are optional on the data
        # source; missing getters just leave the lists empty and the signal
        # declines.
        elif strategy.signal in _BAR_SIGNALS:
            bar_getter = getattr(self.data, "get_intraday_bars", None)
            daily_getter = getattr(self.data, "get_daily_bars", None)
            intraday_bars = (
                bar_getter(symbol, scfg.signal.strat_interval)
                if bar_getter is not None
                else []
            )
            daily_bars = daily_getter(symbol) if daily_getter is not None else []
            quote = replace(quote, bars=intraday_bars, daily_bars=daily_bars)
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
        candidate_id = self.storage.record_candidate(candidate)
        return replace(candidate, id=candidate_id)

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
