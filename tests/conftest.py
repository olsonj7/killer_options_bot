"""Shared test fixtures and config builders."""

from __future__ import annotations

import pytest

from killer_options_bot.config import (
    Config,
    ContractFilters,
    ExitConfig,
    LiveConfig,
    RiskConfig,
    SignalConfig,
    TradierConfig,
)


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch):
    """Never let the test suite touch a hosted database.

    ``load_config`` calls ``load_dotenv()`` and reads ``DATABASE_URL`` from the
    environment (which takes precedence over the YAML ``db_path``). Without this
    guard, tests would connect to whatever ``.env`` points at (e.g. Supabase).
    Setting the var to an empty string forces the local SQLite path: it is
    already present so ``load_dotenv(override=False)`` won't repopulate it, and
    an empty string is falsy so ``load_config`` falls back to ``db_path``.
    """
    monkeypatch.setenv("DATABASE_URL", "")


def make_config(tmp_path, **overrides) -> Config:
    defaults = dict(
        account_value=1000.0,
        trading_mode="scan",
        watchlist=["AAPL"],
        risk=RiskConfig(
            max_trade_risk_pct=0.05,
            max_open_positions=1,
            max_trades_per_week=2,
        ),
        filters=ContractFilters(
            min_dte=30,
            max_dte=60,
            min_delta=0.30,
            max_delta=0.45,
            max_spread_pct=0.12,
            min_volume=100,
            min_open_interest=500,
        ),
        signal=SignalConfig(
            sma_period=20, rsi_period=14, rsi_min=45, rsi_max=70
        ),
        exits=ExitConfig(
            profit_target_pct=0.35,
            stop_loss_pct=0.45,
            max_holding_days=21,
            min_dte_exit=21,
        ),
        live=LiveConfig(
            enabled=False,
            kill_switch_file=tmp_path / "KILL_SWITCH",
            max_daily_loss=20.0,
            max_weekly_loss=50.0,
            max_contracts_per_order=1,
        ),
        db_path=tmp_path / "test.db",
        database_url=None,
        tradier=TradierConfig(api_token=None, base_url="https://example.test"),
    )
    defaults.update(overrides)
    return Config(**defaults)
