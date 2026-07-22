"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class ContractFilters:
    min_dte: int
    max_dte: int
    min_delta: float
    max_delta: float
    max_spread_pct: float
    min_volume: int
    min_open_interest: int


@dataclass(frozen=True)
class RiskConfig:
    max_trade_risk_pct: float
    max_open_positions: int
    max_trades_per_week: int


@dataclass(frozen=True)
class SignalConfig:
    sma_period: int
    rsi_period: int
    rsi_min: float
    rsi_max: float
    # Minimum distance price must sit BEYOND the SMA to count as a real trend,
    # as a fraction of price (0.005 = 0.5%). Filters out "barely above the line"
    # entries that are really just noise around the mean. 0 disables the buffer.
    trend_buffer_pct: float = 0.0
    # If > 0, require the SMA itself to be sloping in the trade's direction over
    # this many bars (rising for calls, falling for puts). Enforces trend
    # alignment so we don't buy calls into a rolling-over average. 0 disables.
    slope_lookback: int = 0
    # Intraday-signal settings (used by the 'intraday_momentum' signal). Bars
    # are Tradier timesales at this interval; the SMA/RSI use shorter periods
    # than the daily signal so a same-day trend is detectable early in the
    # session rather than 100+ minutes in.
    intraday_interval: str = "5min"
    intraday_sma_period: int = 9
    intraday_rsi_period: int = 9
    # STRAT breakout settings (used by the 'strat_breakout' signal). Bars are
    # OHLC (Tradier timesales) at ``strat_interval``; an entry fires when the
    # last completed bar is a directional STRAT "2" bar (breaks the prior bar's
    # high or low) whose range clears ``strat_displacement_mult`` x the average
    # range of the last ``strat_atr_period`` bars (a displacement/impulse), and
    # the move aligns with the prior-day high/low (PDH/PDL) bias.
    strat_interval: str = "15min"
    strat_displacement_mult: float = 1.5
    strat_atr_period: int = 10
    # Higher-timeframe trend alignment (used by 'momentum'/'daily_reversal').
    # If > 0, daily closes are bucketed into ``weekly_bar_days``-day groups to
    # build a synthetic weekly series; a CALL is refused while that weekly
    # trend is still down and a PUT is refused while it is still up. This
    # stops the daily signal from fighting an obvious higher-timeframe trend
    # (e.g. shorting into a green weekly bar). 0 disables (default).
    weekly_bar_days: int = 0
    weekly_sma_period: int = 8
    # Support/resistance guard. If > 0, refuse a PUT within ``sr_buffer_pct``
    # of the lowest close over the last ``sr_lookback`` daily bars (likely
    # support / bounce zone) and refuse a CALL within that buffer of the
    # highest close (likely resistance / rejection zone). 0 disables (default).
    sr_lookback: int = 0
    sr_buffer_pct: float = 0.01


@dataclass(frozen=True)
class TradierConfig:
    api_token: str | None
    base_url: str


@dataclass(frozen=True)
class TrimRule:
    """One scale-out level: sell part of a position when profit reaches a level.

    ``at_pct`` is the profit threshold as a fraction of the entry price (0.25 =
    +25%). ``fraction`` is the portion of the *original* position size to sell
    at that level (0.5 = half). Trims fire in ascending ``at_pct`` order.
    """

    at_pct: float
    fraction: float


@dataclass(frozen=True)
class ExitConfig:
    profit_target_pct: float
    stop_loss_pct: float
    max_holding_days: int
    min_dte_exit: int
    #: Optional scale-out ladder. Empty means all-or-nothing exits. Contracts
    #: are trimmed on strength before the terminal target/stop closes the rest.
    trims: tuple["TrimRule", ...] = ()
    #: Optional trailing stop on the runner. ``trail_pct`` is the give-back from
    #: the position's high-water option mid that triggers an exit (0.20 = exit
    #: once the mid falls 20% below its peak). ``trail_activate_pct`` is the
    #: profit level that must first be reached before the trail arms (0.30 =
    #: only start trailing after +30%). When ``trail_pct`` > 0 the trailing stop
    #: REPLACES the fixed ``profit_target_pct`` so winners can run; the stop
    #: loss, max-hold and DTE exits still apply. 0 disables trailing.
    trail_pct: float = 0.0
    trail_activate_pct: float = 0.0

    @property
    def trailing_enabled(self) -> bool:
        return self.trail_pct > 0.0



@dataclass(frozen=True)
class SizingConfig:
    """Position sizing: how many contracts to buy per signal.

    When disabled the engine buys a single contract (the original behaviour).
    When enabled it spends a risk budget (``risk_per_trade_pct`` of the account,
    falling back to ``risk.max_trade_risk_pct``) on as many contracts as fit,
    capped by ``max_contracts`` and floored at 1. Buying multiples is what makes
    scale-out (trim) strategies possible.
    """

    enabled: bool = False
    max_contracts: int = 1
    risk_per_trade_pct: float | None = None



@dataclass(frozen=True)
class CostConfig:
    """Transaction-cost assumptions for paper fills.

    Fills at the mid overstate edge: in reality you buy near the ask and sell
    near the bid, plus commission. These knobs feed a ``CostModel`` so paper
    entries/exits (and the run loop) reflect that friction.
    """

    commission_per_contract: float = 0.65
    slippage_frac: float = 1.0
    enabled: bool = True


@dataclass(frozen=True)
class StrategyConfig:
    """A named strategy "method": its own contract filters + exit rules.

    Strategies let higher account tiers unlock additional playbooks (0DTE
    scalps, swings, LEAPS, ...) alongside the base weekly momentum trade. Each
    strategy carries the DTE/delta window it hunts in and the exit rules used to
    manage the positions it opens, so a 0DTE scalp and a LEAPS hold can coexist
    with completely different management. ``signal`` selects which entry signal
    generates its trades (currently only "momentum").
    """

    name: str
    signal: str
    filters: "ContractFilters"
    exits: "ExitConfig"
    #: How often (minutes) the run loop hunts for NEW entries with this
    #: strategy. Exit management always runs every loop tick regardless; this
    #: only paces entries, so a 0DTE scalp can scan every minute while LEAPS
    #: only look once a day.
    scan_interval_minutes: int = 5
    #: Maximum positions this strategy may open in a single calendar day.
    #: 0 means unlimited. Used to cap 0DTE overtrading.
    max_trades_per_day: int = 0
    #: When True, no new entries are opened between noon and 2 pm ET — the
    #: low-volume chop window where both momentum and reversal signals are
    #: noisiest. Exit management still runs; only entries are suppressed.
    skip_midday: bool = False
    #: Optional label shared by strategies that should never hold opposite
    #: sides (a PUT and a CALL) on the same underlying at once, even though
    #: they are independent strategies (e.g. "default" and "weekly_reversion"
    #: both hold for about a week -- a countertrend bet from one fighting the
    #: other's live position is a wash, not a hedge). None disables the check.
    conflict_group: str | None = None


@dataclass(frozen=True)
class LiveConfig:
    """Guardrails for live order placement. Disabled by default."""

    enabled: bool
    kill_switch_file: Path
    max_daily_loss: float
    max_weekly_loss: float
    max_contracts_per_order: int


@dataclass(frozen=True)
class WithdrawConfig:
    """Rules for the (advisory-only) withdrawal advisor.

    The advisor NEVER moves money; it only recommends when to take cash off the
    table. All amounts are in dollars and computed from *realized* P/L (you can
    only withdraw cash you've actually banked). A rule is inactive when its key
    knob is 0. ``min_trading_balance`` is a floor the profit rules never dip
    below so the account can keep trading; the defensive drawdown rule may go
    below it because its whole point is to stop trading.
    """

    enabled: bool
    starting_capital: float
    min_trading_balance: float
    skim_pct: float
    tax_reserve_pct: float
    milestones: tuple[tuple[float, float], ...]
    drawdown_trigger_pct: float
    drawdown_defense_pct: float


@dataclass(frozen=True)
class Config:
    account_value: float
    trading_mode: str
    watchlist: list[str]
    risk: RiskConfig
    filters: ContractFilters
    signal: SignalConfig
    exits: ExitConfig
    live: LiveConfig
    db_path: Path
    database_url: str | None
    tradier: TradierConfig
    strategies: tuple["StrategyConfig", ...] = ()
    costs: "CostConfig" = field(default_factory=CostConfig)
    sizing: "SizingConfig" = field(default_factory=SizingConfig)
    withdraw: "WithdrawConfig" = field(
        default_factory=lambda: WithdrawConfig(
            enabled=False,
            starting_capital=0.0,
            min_trading_balance=0.0,
            skim_pct=0.0,
            tax_reserve_pct=0.0,
            milestones=(),
            drawdown_trigger_pct=0.0,
            drawdown_defense_pct=0.0,
        )
    )

    @property
    def active_strategies(self) -> tuple["StrategyConfig", ...]:
        """Strategies to run. Falls back to a single default profile built from
        the base filters/exits so code paths that don't configure strategies
        (and direct ``Config`` construction in tests) keep working unchanged."""
        if self.strategies:
            return self.strategies
        return (
            StrategyConfig(
                name="default",
                signal="momentum",
                filters=self.filters,
                exits=self.exits,
            ),
        )

    def cost_model(self):
        """Build the ``CostModel`` for paper fills from ``costs`` config.

        Returns ``CostModel.free()`` (mid fills, no commission) when costs are
        disabled, so tests and the legacy behaviour can opt out.
        """
        from killer_options_bot.models import CostModel

        if not self.costs.enabled:
            return CostModel.free()
        return CostModel(
            commission_per_contract=self.costs.commission_per_contract,
            slippage_frac=self.costs.slippage_frac,
        )

    def contracts_for(self, cost_per_contract: float) -> int:
        """How many contracts to buy for a signal given a per-contract debit.

        Returns 1 when sizing is disabled (legacy behaviour). When enabled,
        spends the risk budget on as many contracts as fit, capped by
        ``max_contracts`` and floored at 1 so a signal always takes at least a
        single contract.
        """
        if not self.sizing.enabled:
            return 1
        if cost_per_contract <= 0:
            return 1
        pct = self.sizing.risk_per_trade_pct
        if pct is None:
            pct = self.risk.max_trade_risk_pct
        budget = self.account_value * pct
        n = int(budget // cost_per_contract)
        n = min(n, self.sizing.max_contracts)
        return max(1, n)


def _require(mapping: dict, key: str, section: str):
    if key not in mapping:
        raise ValueError(f"Missing '{key}' in config section '{section}'")
    return mapping[key]


def _select_tier(tiers: list, account_value: float) -> dict:
    """Return the account-size tier whose ``min_value`` best fits the account.

    Tiers let the strategy "bend" with account size: each tier is a dict with a
    ``min_value`` threshold plus any config sections to overlay (e.g. ``risk``,
    ``contract_filters``, ``exits``). The matching tier is the one with the
    greatest ``min_value`` that is <= ``account_value``. Returns an empty dict
    when no tiers are defined or none apply.
    """
    if not tiers:
        return {}
    applicable = [
        t for t in tiers if float(t.get("min_value", 0)) <= account_value
    ]
    if not applicable:
        return {}
    return max(applicable, key=lambda t: float(t.get("min_value", 0)))


def _overlay(base: dict, override: dict) -> dict:
    """Shallow-merge ``override`` onto a copy of ``base`` (override wins)."""
    merged = dict(base)
    merged.update(override or {})
    return merged


def _build_filters(d: dict) -> ContractFilters:
    cfg = ContractFilters(
        min_dte=int(d.get("min_dte", 30)),
        max_dte=int(d.get("max_dte", 60)),
        min_delta=float(d.get("min_delta", 0.30)),
        max_delta=float(d.get("max_delta", 0.45)),
        max_spread_pct=float(d.get("max_spread_pct", 0.12)),
        min_volume=int(d.get("min_volume", 100)),
        min_open_interest=int(d.get("min_open_interest", 500)),
    )
    if cfg.min_dte > cfg.max_dte:
        raise ValueError("min_dte must be <= max_dte")
    if cfg.min_delta > cfg.max_delta:
        raise ValueError("min_delta must be <= max_delta")
    return cfg


def _build_trims(raw_trims) -> tuple[TrimRule, ...]:
    """Parse and validate the scale-out ladder for an exits block."""
    if not raw_trims:
        return ()
    rules: list[TrimRule] = []
    for i, t in enumerate(raw_trims):
        at_pct = float(t.get("at_pct", 0.0))
        fraction = float(t.get("fraction", 0.0))
        if at_pct <= 0:
            raise ValueError("exits.trims[].at_pct must be positive")
        if not 0 < fraction < 1:
            raise ValueError(
                "exits.trims[].fraction must be between 0 and 1 (exclusive)"
            )
        rules.append(TrimRule(at_pct=at_pct, fraction=fraction))
    rules.sort(key=lambda r: r.at_pct)
    total = sum(r.fraction for r in rules)
    if total >= 1.0:
        raise ValueError(
            "exits.trims fractions must sum to less than 1 "
            "(leave a runner for the terminal exit)"
        )
    return tuple(rules)


def _build_exits(d: dict) -> ExitConfig:
    # Trim ladder: the UI stores trims as flat keys ``trim_N_at_pct`` /
    # ``trim_N_fraction`` in the exits dict (so they flow through the normal
    # override mechanism). When any such keys exist they REPLACE the YAML
    # ``trims`` list so an edit in /config always wins over the file.
    flat_at: dict[int, float] = {}
    flat_frac: dict[int, float] = {}
    for k, v in d.items():
        if k.startswith("trim_") and k.endswith("_at_pct"):
            try:
                flat_at[int(k[5:-7])] = float(v)
            except (ValueError, IndexError):
                pass
        elif k.startswith("trim_") and k.endswith("_fraction"):
            try:
                flat_frac[int(k[5:-9])] = float(v)
            except (ValueError, IndexError):
                pass
    if flat_at or flat_frac:
        override_trims = [
            {"at_pct": flat_at[i], "fraction": flat_frac[i]}
            for i in sorted(set(flat_at) | set(flat_frac))
            if flat_at.get(i, 0) > 0 and flat_frac.get(i, 0) > 0
        ]
        trims = _build_trims(override_trims)
    else:
        trims = _build_trims(d.get("trims", []))
    cfg = ExitConfig(
        profit_target_pct=float(d.get("profit_target_pct", 0.35)),
        stop_loss_pct=float(d.get("stop_loss_pct", 0.45)),
        max_holding_days=int(d.get("max_holding_days", 21)),
        min_dte_exit=int(d.get("min_dte_exit", 21)),
        trims=trims,
        trail_pct=float(d.get("trail_pct", 0.0)),
        trail_activate_pct=float(d.get("trail_activate_pct", 0.0)),
    )
    if cfg.profit_target_pct <= 0:
        raise ValueError("exits.profit_target_pct must be positive")
    if not 0 < cfg.stop_loss_pct <= 1:
        raise ValueError("exits.stop_loss_pct must be between 0 and 1")
    if not 0 <= cfg.trail_pct < 1:
        raise ValueError("exits.trail_pct must be between 0 and 1 (0 disables)")
    if cfg.trail_activate_pct < 0:
        raise ValueError("exits.trail_activate_pct must be >= 0")
    return cfg


_VALID_SIGNALS = {"momentum", "intraday_momentum", "strat_breakout", "intraday_reversal", "daily_reversal"}



def load_config(
    path: str | Path,
    overrides: dict[tuple[str, str], str] | None = None,
    strategy_overrides: dict[tuple[str, str, str], str] | None = None,
) -> Config:
    """Load and validate configuration from YAML + environment.

    ``overrides`` is an optional ``{(section, key): str_value}`` mapping for
    base config fields (account, risk, contract_filters, exits).
    ``strategy_overrides`` is an optional ``{(strategy, section, key): str_value}``
    mapping for per-strategy fields. Both are produced by Storage methods and
    let the /config web UI persist edits to Supabase so they survive redeploys.
    """
    load_dotenv()

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # Layer any DB-persisted overrides on top of the YAML baseline.
    if overrides:
        for (section, key), value_str in overrides.items():
            try:
                raw.setdefault(section, {})[key] = float(value_str)
            except (ValueError, TypeError):
                pass

    # Layer per-strategy overrides into raw['strategies'][name][section][key].
    if strategy_overrides:
        for (strat_name, section, key), value_str in strategy_overrides.items():
            strat_raw = raw.setdefault("strategies", {}).setdefault(strat_name, {})
            if key == "skip_midday":
                strat_raw.setdefault(section, {})[key] = (
                    value_str.lower() in ("true", "1", "yes")
                )
            else:
                try:
                    strat_raw.setdefault(section, {})[key] = float(value_str)
                except (ValueError, TypeError):
                    pass

    account = raw.get("account", {})
    mode = raw.get("mode", {})
    risk = raw.get("risk", {})
    filters = raw.get("contract_filters", {})
    signal = raw.get("signal", {})
    exits = raw.get("exits", {})
    storage = raw.get("storage", {})

    account_value = float(_require(account, "value", "account"))
    if account_value <= 0:
        raise ValueError("account.value must be positive")

    # Account-size tiers: overlay the matching tier's sections so the strategy
    # bends automatically with account value. No-op when no tiers are defined.
    tier = _select_tier(raw.get("account_tiers", []), account_value)
    if tier:
        risk = _overlay(risk, tier.get("risk", {}))
        filters = _overlay(filters, tier.get("contract_filters", {}))
        exits = _overlay(exits, tier.get("exits", {}))

    trading_mode = str(mode.get("trading_mode", "scan")).lower()
    if trading_mode not in {"scan", "paper", "live"}:
        raise ValueError(
            f"Unsupported trading_mode '{trading_mode}'. "
            "Use 'scan', 'paper', or 'live'."
        )

    watchlist = list(raw.get("watchlist", []))
    # A tier may override the watchlist entirely (e.g. keep a small account on
    # SPY/QQQ only, and open up more tickers as the account grows). When a tier
    # has no 'watchlist' key, the base watchlist is used unchanged.
    if tier and tier.get("watchlist") is not None:
        watchlist = list(tier.get("watchlist"))
    if not watchlist:
        raise ValueError("watchlist must contain at least one symbol")

    risk_cfg = RiskConfig(
        max_trade_risk_pct=float(risk.get("max_trade_risk_pct", 0.05)),
        max_open_positions=int(risk.get("max_open_positions", 1)),
        max_trades_per_week=int(risk.get("max_trades_per_week", 2)),
    )
    if not 0 < risk_cfg.max_trade_risk_pct <= 1:
        raise ValueError("risk.max_trade_risk_pct must be between 0 and 1")

    filters_cfg = _build_filters(filters)

    signal_cfg = SignalConfig(
        sma_period=int(signal.get("sma_period", 20)),
        rsi_period=int(signal.get("rsi_period", 14)),
        rsi_min=float(signal.get("rsi_min", 45)),
        rsi_max=float(signal.get("rsi_max", 70)),
        trend_buffer_pct=float(signal.get("trend_buffer_pct", 0.0)),
        slope_lookback=int(signal.get("slope_lookback", 0)),
        intraday_interval=str(signal.get("intraday_interval", "5min")),
        intraday_sma_period=int(signal.get("intraday_sma_period", 9)),
        intraday_rsi_period=int(signal.get("intraday_rsi_period", 9)),
        strat_interval=str(signal.get("strat_interval", "15min")),
        strat_displacement_mult=float(
            signal.get("strat_displacement_mult", 1.5)
        ),
        strat_atr_period=int(signal.get("strat_atr_period", 10)),
        weekly_bar_days=int(signal.get("weekly_bar_days", 0)),
        weekly_sma_period=int(signal.get("weekly_sma_period", 8)),
        sr_lookback=int(signal.get("sr_lookback", 0)),
        sr_buffer_pct=float(signal.get("sr_buffer_pct", 0.01)),
    )
    if signal_cfg.intraday_interval not in {"1min", "5min", "15min"}:
        raise ValueError(
            "signal.intraday_interval must be one of 1min, 5min, 15min"
        )
    if signal_cfg.intraday_sma_period <= 0 or signal_cfg.intraday_rsi_period <= 0:
        raise ValueError("signal.intraday_*_period must be positive")
    if not 0 <= signal_cfg.trend_buffer_pct < 1:
        raise ValueError("signal.trend_buffer_pct must be in [0, 1)")
    if signal_cfg.slope_lookback < 0:
        raise ValueError("signal.slope_lookback must be >= 0")
    if signal_cfg.strat_interval not in {"1min", "5min", "15min"}:
        raise ValueError(
            "signal.strat_interval must be one of 1min, 5min, 15min"
        )
    if signal_cfg.strat_displacement_mult < 0:
        raise ValueError("signal.strat_displacement_mult must be >= 0")
    if signal_cfg.strat_atr_period <= 0:
        raise ValueError("signal.strat_atr_period must be positive")

    exits_cfg = _build_exits(exits)

    # --- Strategy profiles ------------------------------------------------
    # Each named strategy overlays its own contract_filters/exits on the base
    # (tier-adjusted) sections. "default" is always available and mirrors the
    # base weekly trade. A tier's optional 'strategies' list selects which
    # profiles are active for that account size (higher tiers unlock more).
    default_prof = (raw.get("strategies", {}) or {}).get("default") or {}
    registry: dict[str, StrategyConfig] = {
        "default": StrategyConfig(
            name="default",
            signal="momentum",
            filters=filters_cfg,
            exits=exits_cfg,
            scan_interval_minutes=int(
                default_prof.get("scan_interval_minutes", 5)
            ),
            max_trades_per_day=int(
                (default_prof.get("limits") or {}).get("max_trades_per_day", 0)
            ),
            conflict_group=default_prof.get("conflict_group"),
        )
    }
    for name, prof in (raw.get("strategies", {}) or {}).items():
        prof = prof or {}
        if name == "default":
            continue
        sig = str(prof.get("signal", "momentum"))
        if sig not in _VALID_SIGNALS:
            raise ValueError(
                f"strategy '{name}' has unknown signal '{sig}'. "
                f"Valid signals: {sorted(_VALID_SIGNALS)}"
            )
        interval = int(prof.get("scan_interval_minutes", 5))
        if interval <= 0:
            raise ValueError(
                f"strategy '{name}' scan_interval_minutes must be positive"
            )
        max_trades_per_day = int(
            (prof.get("limits") or {}).get("max_trades_per_day", 0)
        )
        skip_midday = bool((prof.get("limits") or {}).get("skip_midday", False))
        registry[name] = StrategyConfig(
            name=name,
            signal=sig,
            filters=_build_filters(
                _overlay(filters, prof.get("contract_filters", {}))
            ),
            exits=_build_exits(_overlay(exits, prof.get("exits", {}))),
            scan_interval_minutes=interval,
            max_trades_per_day=max_trades_per_day,
            skip_midday=skip_midday,
            conflict_group=prof.get("conflict_group"),
        )

    if tier and tier.get("strategies") is not None:
        active_names = list(tier.get("strategies"))
    else:
        active_names = ["default"]
    if not active_names:
        raise ValueError("a tier's 'strategies' list must not be empty")
    active_strategies: list[StrategyConfig] = []
    for nm in active_names:
        if nm not in registry:
            raise ValueError(
                f"unknown strategy '{nm}'. Define it under 'strategies:'. "
                f"Known strategies: {sorted(registry)}"
            )
        active_strategies.append(registry[nm])

    db_path = Path(storage.get("db_path", "data/killer_options_bot.db"))
    # Hosted DB (e.g. Supabase) takes precedence when provided.
    database_url = os.getenv("DATABASE_URL") or storage.get("database_url")

    live_raw = raw.get("live", {})
    live_cfg = LiveConfig(
        enabled=bool(live_raw.get("enabled", False)),
        kill_switch_file=Path(
            live_raw.get("kill_switch_file", "KILL_SWITCH")
        ),
        max_daily_loss=float(live_raw.get("max_daily_loss", 0.0)) or (
            account_value * 0.02
        ),
        max_weekly_loss=float(live_raw.get("max_weekly_loss", 0.0)) or (
            account_value * 0.05
        ),
        max_contracts_per_order=int(
            live_raw.get("max_contracts_per_order", 1)
        ),
    )

    tradier = TradierConfig(
        api_token=os.getenv("TRADIER_API_TOKEN"),
        base_url=os.getenv("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1"),
    )

    costs_raw = raw.get("costs", {}) or {}
    costs_cfg = CostConfig(
        commission_per_contract=float(
            costs_raw.get("commission_per_contract", 0.65)
        ),
        slippage_frac=float(costs_raw.get("slippage_frac", 1.0)),
        enabled=bool(costs_raw.get("enabled", True)),
    )
    if costs_cfg.commission_per_contract < 0:
        raise ValueError("costs.commission_per_contract must be >= 0")
    if not 0 <= costs_cfg.slippage_frac <= 1:
        raise ValueError("costs.slippage_frac must be between 0 and 1")

    sizing_raw = raw.get("sizing", {}) or {}
    sizing_risk = sizing_raw.get("risk_per_trade_pct")
    sizing_cfg = SizingConfig(
        enabled=bool(sizing_raw.get("enabled", False)),
        max_contracts=int(sizing_raw.get("max_contracts", 1)),
        risk_per_trade_pct=(
            float(sizing_risk) if sizing_risk is not None else None
        ),
    )
    if sizing_cfg.max_contracts < 1:
        raise ValueError("sizing.max_contracts must be >= 1")
    if (
        sizing_cfg.risk_per_trade_pct is not None
        and not 0 < sizing_cfg.risk_per_trade_pct <= 1
    ):
        raise ValueError("sizing.risk_per_trade_pct must be between 0 and 1")

    wd = raw.get("withdraw", {}) or {}
    starting_capital = float(wd.get("starting_capital", 0.0)) or account_value
    milestones_raw = wd.get("milestones", []) or []
    milestones: list[tuple[float, float]] = []
    for m in milestones_raw:
        at = float(_require(m, "at", "withdraw.milestones"))
        amount = float(_require(m, "withdraw", "withdraw.milestones"))
        milestones.append((at, amount))
    milestones.sort(key=lambda pair: pair[0])
    skim_pct = float(wd.get("skim_pct", 0.0))
    tax_reserve_pct = float(wd.get("tax_reserve_pct", 0.0))
    drawdown_trigger_pct = float(wd.get("drawdown_trigger_pct", 0.0))
    drawdown_defense_pct = float(wd.get("drawdown_defense_pct", 0.0))
    for name, val in (
        ("skim_pct", skim_pct),
        ("tax_reserve_pct", tax_reserve_pct),
        ("drawdown_trigger_pct", drawdown_trigger_pct),
        ("drawdown_defense_pct", drawdown_defense_pct),
    ):
        if not 0 <= val <= 1:
            raise ValueError(f"withdraw.{name} must be between 0 and 1")
    withdraw_cfg = WithdrawConfig(
        enabled=bool(wd.get("enabled", False)),
        starting_capital=starting_capital,
        min_trading_balance=float(
            wd.get("min_trading_balance", 0.0)
        )
        or starting_capital,
        skim_pct=skim_pct,
        tax_reserve_pct=tax_reserve_pct,
        milestones=tuple(milestones),
        drawdown_trigger_pct=drawdown_trigger_pct,
        drawdown_defense_pct=drawdown_defense_pct,
    )

    return Config(
        account_value=account_value,
        trading_mode=trading_mode,
        watchlist=watchlist,
        risk=risk_cfg,
        filters=filters_cfg,
        signal=signal_cfg,
        exits=exits_cfg,
        live=live_cfg,
        db_path=db_path,
        database_url=database_url,
        tradier=tradier,
        strategies=tuple(active_strategies),
        costs=costs_cfg,
        sizing=sizing_cfg,
        withdraw=withdraw_cfg,
    )