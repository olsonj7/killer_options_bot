"""Tests for the guarded live-execution scaffolding.

These never touch a real broker: they use MockBroker and MockMarketData.
The point is to prove the guardrails block orders as designed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import make_config

from killer_options_bot.brokers.mock import MockBroker, MockMarketData
from killer_options_bot.config import LiveConfig
from killer_options_bot.live import KillSwitchError, LiveEngine, LiveGuardError
from killer_options_bot.models import (
    Candidate,
    OptionContract,
    PaperPosition,
    PositionStatus,
    RiskDecision,
    Side,
)
from killer_options_bot.storage import SQLiteStorage


def _live_config(tmp_path, **overrides):
    live = LiveConfig(
        enabled=overrides.pop("enabled", True),
        kill_switch_file=overrides.pop("kill_switch_file", tmp_path / "KILL"),
        max_daily_loss=overrides.pop("max_daily_loss", 100.0),
        max_weekly_loss=overrides.pop("max_weekly_loss", 250.0),
        max_contracts_per_order=overrides.pop("max_contracts_per_order", 1),
    )
    return make_config(
        tmp_path,
        account_value=10000.0,
        trading_mode=overrides.pop("trading_mode", "live"),
        live=live,
        **overrides,
    )


def _candidate(as_of: date) -> Candidate:
    contract = OptionContract(
        symbol="AAPL260315C00150000",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        bid=1.00,
        ask=1.10,
        last=1.05,
        delta=0.40,
        implied_volatility=0.30,
        volume=500,
        open_interest=2000,
    )
    return Candidate(
        contract=contract,
        side=Side.CALL,
        signal_note="test",
        decision=RiskDecision.accept(),
        max_loss=contract.cost,
    )


def _engine(config, tmp_path, as_of):
    storage = SQLiteStorage(config.db_path)
    data = MockMarketData(as_of=as_of)
    broker = MockBroker()
    return LiveEngine(config, data, storage, broker, as_of=as_of), storage, broker


def test_dry_run_does_not_transmit(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path)
    engine, storage, broker = _engine(config, tmp_path, as_of)
    result = engine.open_from_candidate(_candidate(as_of), confirm_live=False)
    assert result.accepted
    assert result.position is None  # nothing persisted on a dry run
    assert storage.count_open_positions() == 0
    assert broker.orders[0]["dry_run"] is True


def test_confirm_live_opens_and_records_order(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path)
    engine, storage, broker = _engine(config, tmp_path, as_of)
    result = engine.open_from_candidate(_candidate(as_of), confirm_live=True)
    assert result.accepted
    assert result.position is not None
    assert storage.count_open_positions() == 1
    assert broker.orders[0]["dry_run"] is False
    # Limit at the ask, never a market order.
    assert broker.orders[0]["limit_price"] == 1.10


def test_kill_switch_blocks(tmp_path):
    as_of = date(2026, 1, 1)
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    config = _live_config(tmp_path, kill_switch_file=kill)
    engine, _storage, _broker = _engine(config, tmp_path, as_of)
    with pytest.raises(KillSwitchError):
        engine.open_from_candidate(_candidate(as_of), confirm_live=True)


def test_disabled_blocks(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path, enabled=False)
    engine, _storage, _broker = _engine(config, tmp_path, as_of)
    with pytest.raises(LiveGuardError):
        engine.open_from_candidate(_candidate(as_of), confirm_live=True)


def test_non_live_mode_blocks(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path, trading_mode="paper")
    engine, _storage, _broker = _engine(config, tmp_path, as_of)
    with pytest.raises(LiveGuardError):
        engine.open_from_candidate(_candidate(as_of), confirm_live=True)


def test_quantity_over_limit_blocks(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path, max_contracts_per_order=1)
    engine, _storage, _broker = _engine(config, tmp_path, as_of)
    with pytest.raises(LiveGuardError):
        engine.open_from_candidate(
            _candidate(as_of), quantity=2, confirm_live=True
        )


def test_daily_loss_lockout(tmp_path):
    as_of = date(2026, 1, 1)
    config = _live_config(tmp_path, max_daily_loss=40.0)
    engine, storage, _broker = _engine(config, tmp_path, as_of)
    # Seed a realized live loss of -50 today.
    losing = PaperPosition(
        option_symbol="OLD",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=1,
        entry_price=1.00,
        entry_date=as_of,
        status=PositionStatus.OPEN,
    )
    storage.open_position(losing, mode="live")
    storage.close_position(losing.id, 0.50, as_of, "stop loss")
    with pytest.raises(LiveGuardError):
        engine.open_from_candidate(_candidate(as_of), confirm_live=True)
