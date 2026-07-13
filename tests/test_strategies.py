"""Tests for named strategy profiles and their tier-based activation."""

from __future__ import annotations

from datetime import date

import yaml
from conftest import make_config

from killer_options_bot.config import StrategyConfig, load_config
from killer_options_bot.models import Candidate, OptionContract, RiskDecision, Side
from killer_options_bot.paper import PaperEngine
from killer_options_bot.storage import SQLiteStorage


def _write(tmp_path, account_value, tiers=None, strategies=None):
    cfg = {
        "account": {"value": account_value},
        "mode": {"trading_mode": "paper"},
        "watchlist": ["SPY"],
        "risk": {
            "max_trade_risk_pct": 0.30,
            "max_open_positions": 5,
            "max_trades_per_week": 50,
        },
        "contract_filters": {
            "min_dte": 2,
            "max_dte": 7,
            "min_delta": 0.20,
            "max_delta": 0.40,
            "max_spread_pct": 0.15,
            "min_volume": 1,
            "min_open_interest": 1,
        },
        "signal": {"sma_period": 3, "rsi_period": 3, "rsi_min": 40, "rsi_max": 75},
        "exits": {
            "profit_target_pct": 0.30,
            "stop_loss_pct": 0.25,
            "max_holding_days": 3,
            "min_dte_exit": 0,
        },
        "storage": {"db_path": str(tmp_path / "t.db")},
    }
    if strategies is not None:
        cfg["strategies"] = strategies
    if tiers is not None:
        cfg["account_tiers"] = tiers
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


_ZERODTE = {
    "zerodte": {
        "signal": "momentum",
        "contract_filters": {"min_dte": 0, "max_dte": 1, "min_delta": 0.35, "max_delta": 0.55},
        "exits": {"profit_target_pct": 0.40, "stop_loss_pct": 0.30, "max_holding_days": 0, "min_dte_exit": 0},
    }
}


# --- default / backward compatibility --------------------------------------


def test_no_strategies_block_yields_single_default(tmp_path):
    cfg = load_config(_write(tmp_path, 1000.0))
    active = cfg.active_strategies
    assert len(active) == 1
    assert active[0].name == "default"
    # Default mirrors the base filters/exits.
    assert active[0].filters.min_dte == 2
    assert active[0].exits.max_holding_days == 3


def test_make_config_active_strategies_fallback(tmp_path):
    # Direct Config construction (no strategies) still resolves a default.
    cfg = make_config(tmp_path)
    assert [s.name for s in cfg.active_strategies] == ["default"]
    assert cfg.active_strategies[0].filters is cfg.filters


# --- profile parsing + overlay ---------------------------------------------


def test_strategy_overlays_base_and_keeps_unset_keys(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            1000.0,
            tiers=[{"min_value": 0, "strategies": ["default", "zerodte"]}],
            strategies=_ZERODTE,
        )
    )
    zdte = next(s for s in cfg.active_strategies if s.name == "zerodte")
    # Overridden keys:
    assert zdte.filters.min_dte == 0
    assert zdte.filters.max_dte == 1
    assert zdte.exits.max_holding_days == 0
    # Unset key falls through to the base:
    assert zdte.filters.max_spread_pct == 0.15


def test_unknown_strategy_name_raises(tmp_path):
    path = _write(
        tmp_path,
        1000.0,
        tiers=[{"min_value": 0, "strategies": ["default", "ghost"]}],
        strategies=_ZERODTE,
    )
    try:
        load_config(path)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "ghost" in str(exc)


def test_unknown_signal_raises(tmp_path):
    path = _write(
        tmp_path,
        1000.0,
        strategies={"weird": {"signal": "telepathy"}},
        tiers=[{"min_value": 0, "strategies": ["weird"]}],
    )
    try:
        load_config(path)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "telepathy" in str(exc)


# --- tier activation --------------------------------------------------------


def test_small_tier_excludes_zerodte(tmp_path):
    tiers = [
        {"min_value": 0, "strategies": ["default"]},
        {"min_value": 5000, "strategies": ["default", "zerodte"]},
    ]
    small = load_config(_write(tmp_path, 1000.0, tiers=tiers, strategies=_ZERODTE))
    assert [s.name for s in small.active_strategies] == ["default"]


def test_higher_tier_unlocks_zerodte(tmp_path):
    tiers = [
        {"min_value": 0, "strategies": ["default"]},
        {"min_value": 5000, "strategies": ["default", "zerodte"]},
    ]
    big = load_config(_write(tmp_path, 10000.0, tiers=tiers, strategies=_ZERODTE))
    assert [s.name for s in big.active_strategies] == ["default", "zerodte"]


def test_tier_without_strategies_key_uses_default(tmp_path):
    tiers = [{"min_value": 0}]  # no strategies key
    cfg = load_config(_write(tmp_path, 1000.0, tiers=tiers, strategies=_ZERODTE))
    assert [s.name for s in cfg.active_strategies] == ["default"]


# --- per-strategy exit management ------------------------------------------


def _make_candidate(strategy: str, underlying: str = "SPY") -> Candidate:
    contract = OptionContract(
        symbol=f"OPT-{strategy}",
        underlying=underlying,
        side=Side.CALL,
        strike=100.0,
        expiration=date(2026, 3, 20),
        bid=1.00,
        ask=1.04,
        last=1.02,
        delta=0.40,
        implied_volatility=0.3,
        volume=1000,
        open_interest=1000,
    )
    return Candidate(
        contract=contract,
        side=Side.CALL,
        signal_note="test",
        decision=RiskDecision.accept(),
        max_loss=contract.cost,
        strategy=strategy,
    )


def test_position_remembers_strategy_and_uses_its_exits(tmp_path):
    # Two strategies with different profit targets; positions must be managed
    # under the strategy that opened them.
    from killer_options_bot.config import RiskConfig

    base_filters = make_config(tmp_path).filters
    cfg = make_config(
        tmp_path,
        account_value=100000.0,
        risk=RiskConfig(
            max_trade_risk_pct=0.90,
            max_open_positions=5,
            max_trades_per_week=50,
        ),
        strategies=(
            StrategyConfig(
                name="fast",
                signal="momentum",
                filters=base_filters,
                exits=_exit(profit=0.10),
            ),
            StrategyConfig(
                name="slow",
                signal="momentum",
                filters=base_filters,
                exits=_exit(profit=0.90),
            ),
        ),
    )
    storage = SQLiteStorage(str(tmp_path / "pos.db"))
    from killer_options_bot.brokers.mock import MockMarketData

    engine = PaperEngine(
        cfg, MockMarketData(as_of=date(2026, 2, 2)), storage, as_of=date(2026, 2, 2)
    )
    # Distinct underlyings so the one-position-per-underlying rule doesn't block
    # the second open; this test is about per-strategy exits, not stacking.
    fast = engine.open_from_candidate(_make_candidate("fast", underlying="SPY"))
    slow = engine.open_from_candidate(_make_candidate("slow", underlying="QQQ"))
    assert fast.strategy == "fast"
    assert slow.strategy == "slow"

    # At +15%, the fast strategy (10% target) should exit, the slow (90%) hold.
    price = fast.entry_price * 1.15
    assert engine.exit_reason(fast, price) is not None
    assert engine.exit_reason(slow, price) is None


def _exit(profit=0.30):
    from killer_options_bot.config import ExitConfig

    return ExitConfig(
        profit_target_pct=profit,
        stop_loss_pct=0.25,
        max_holding_days=99,
        min_dte_exit=0,
    )
