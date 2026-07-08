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


@dataclass(frozen=True)
class TradierConfig:
    api_token: str | None
    base_url: str


@dataclass(frozen=True)
class ExitConfig:
    profit_target_pct: float
    stop_loss_pct: float
    max_holding_days: int
    min_dte_exit: int


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


def _build_exits(d: dict) -> ExitConfig:
    cfg = ExitConfig(
        profit_target_pct=float(d.get("profit_target_pct", 0.35)),
        stop_loss_pct=float(d.get("stop_loss_pct", 0.45)),
        max_holding_days=int(d.get("max_holding_days", 21)),
        min_dte_exit=int(d.get("min_dte_exit", 21)),
    )
    if cfg.profit_target_pct <= 0:
        raise ValueError("exits.profit_target_pct must be positive")
    if not 0 < cfg.stop_loss_pct <= 1:
        raise ValueError("exits.stop_loss_pct must be between 0 and 1")
    return cfg


_VALID_SIGNALS = {"momentum"}



def load_config(path: str | Path = "config.yaml") -> Config:
    """Load and validate configuration from YAML + environment."""
    load_dotenv()

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

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
    )

    exits_cfg = _build_exits(exits)

    # --- Strategy profiles ------------------------------------------------
    # Each named strategy overlays its own contract_filters/exits on the base
    # (tier-adjusted) sections. "default" is always available and mirrors the
    # base weekly trade. A tier's optional 'strategies' list selects which
    # profiles are active for that account size (higher tiers unlock more).
    registry: dict[str, StrategyConfig] = {
        "default": StrategyConfig(
            name="default",
            signal="momentum",
            filters=filters_cfg,
            exits=exits_cfg,
            scan_interval_minutes=int(
                (raw.get("strategies", {}) or {}).get("default", {}).get(
                    "scan_interval_minutes", 5
                )
            ),
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
        registry[name] = StrategyConfig(
            name=name,
            signal=sig,
            filters=_build_filters(
                _overlay(filters, prof.get("contract_filters", {}))
            ),
            exits=_build_exits(_overlay(exits, prof.get("exits", {}))),
            scan_interval_minutes=interval,
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
        withdraw=withdraw_cfg,
    )
