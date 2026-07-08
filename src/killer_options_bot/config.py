"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
class LiveConfig:
    """Guardrails for live order placement. Disabled by default."""

    enabled: bool
    kill_switch_file: Path
    max_daily_loss: float
    max_weekly_loss: float
    max_contracts_per_order: int


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


def _require(mapping: dict, key: str, section: str):
    if key not in mapping:
        raise ValueError(f"Missing '{key}' in config section '{section}'")
    return mapping[key]


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

    trading_mode = str(mode.get("trading_mode", "scan")).lower()
    if trading_mode not in {"scan", "paper", "live"}:
        raise ValueError(
            f"Unsupported trading_mode '{trading_mode}'. "
            "Use 'scan', 'paper', or 'live'."
        )

    watchlist = list(raw.get("watchlist", []))
    if not watchlist:
        raise ValueError("watchlist must contain at least one symbol")

    risk_cfg = RiskConfig(
        max_trade_risk_pct=float(risk.get("max_trade_risk_pct", 0.05)),
        max_open_positions=int(risk.get("max_open_positions", 1)),
        max_trades_per_week=int(risk.get("max_trades_per_week", 2)),
    )
    if not 0 < risk_cfg.max_trade_risk_pct <= 1:
        raise ValueError("risk.max_trade_risk_pct must be between 0 and 1")

    filters_cfg = ContractFilters(
        min_dte=int(filters.get("min_dte", 30)),
        max_dte=int(filters.get("max_dte", 60)),
        min_delta=float(filters.get("min_delta", 0.30)),
        max_delta=float(filters.get("max_delta", 0.45)),
        max_spread_pct=float(filters.get("max_spread_pct", 0.12)),
        min_volume=int(filters.get("min_volume", 100)),
        min_open_interest=int(filters.get("min_open_interest", 500)),
    )
    if filters_cfg.min_dte > filters_cfg.max_dte:
        raise ValueError("min_dte must be <= max_dte")
    if filters_cfg.min_delta > filters_cfg.max_delta:
        raise ValueError("min_delta must be <= max_delta")

    signal_cfg = SignalConfig(
        sma_period=int(signal.get("sma_period", 20)),
        rsi_period=int(signal.get("rsi_period", 14)),
        rsi_min=float(signal.get("rsi_min", 45)),
        rsi_max=float(signal.get("rsi_max", 70)),
    )

    exits_cfg = ExitConfig(
        profit_target_pct=float(exits.get("profit_target_pct", 0.35)),
        stop_loss_pct=float(exits.get("stop_loss_pct", 0.45)),
        max_holding_days=int(exits.get("max_holding_days", 21)),
        min_dte_exit=int(exits.get("min_dte_exit", 21)),
    )
    if exits_cfg.profit_target_pct <= 0:
        raise ValueError("exits.profit_target_pct must be positive")
    if not 0 < exits_cfg.stop_loss_pct <= 1:
        raise ValueError("exits.stop_loss_pct must be between 0 and 1")

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
    )
