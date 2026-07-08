"""Tests for account-size tier overlays in load_config."""

from __future__ import annotations

import yaml

from killer_options_bot.config import _select_tier, load_config


def _write_config(tmp_path, account_value, tiers=None):
    cfg = {
        "account": {"value": account_value},
        "mode": {"trading_mode": "paper"},
        "watchlist": ["SPY"],
        "risk": {
            "max_trade_risk_pct": 0.05,
            "max_open_positions": 1,
            "max_trades_per_week": 10,
        },
        "contract_filters": {
            "min_dte": 2,
            "max_dte": 7,
            "min_delta": 0.30,
            "max_delta": 0.45,
            "max_spread_pct": 0.12,
            "min_volume": 100,
            "min_open_interest": 500,
        },
        "signal": {"sma_period": 20, "rsi_period": 14, "rsi_min": 45, "rsi_max": 70},
        "exits": {
            "profit_target_pct": 0.30,
            "stop_loss_pct": 0.25,
            "max_holding_days": 3,
            "min_dte_exit": 0,
        },
        "storage": {"db_path": str(tmp_path / "t.db")},
    }
    if tiers is not None:
        cfg["account_tiers"] = tiers
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


_TIERS = [
    {
        "min_value": 0,
        "risk": {"max_trade_risk_pct": 0.30, "max_open_positions": 2},
        "contract_filters": {"min_delta": 0.20, "max_delta": 0.35},
    },
    {
        "min_value": 5000,
        "risk": {"max_trade_risk_pct": 0.15, "max_open_positions": 3},
        "contract_filters": {"min_delta": 0.30, "max_delta": 0.45},
    },
    {
        "min_value": 25000,
        "risk": {"max_trade_risk_pct": 0.10, "max_open_positions": 5},
        "contract_filters": {"min_delta": 0.35, "max_delta": 0.50},
    },
]


def test_select_tier_picks_greatest_min_value_at_or_below():
    assert _select_tier(_TIERS, 500)["min_value"] == 0
    assert _select_tier(_TIERS, 4999)["min_value"] == 0
    assert _select_tier(_TIERS, 5000)["min_value"] == 5000
    assert _select_tier(_TIERS, 24999)["min_value"] == 5000
    assert _select_tier(_TIERS, 25000)["min_value"] == 25000
    assert _select_tier(_TIERS, 1_000_000)["min_value"] == 25000


def test_select_tier_empty_when_none_defined():
    assert _select_tier([], 1000) == {}


def test_select_tier_empty_when_account_below_all():
    tiers = [{"min_value": 5000}]
    assert _select_tier(tiers, 1000) == {}


def test_no_tiers_leaves_base_config_unchanged(tmp_path):
    path = _write_config(tmp_path, 1000.0, tiers=None)
    cfg = load_config(path)
    assert cfg.risk.max_trade_risk_pct == 0.05
    assert cfg.risk.max_open_positions == 1
    assert cfg.filters.min_delta == 0.30
    assert cfg.filters.max_delta == 0.45


def test_low_tier_overlay_applied(tmp_path):
    path = _write_config(tmp_path, 1000.0, tiers=_TIERS)
    cfg = load_config(path)
    assert cfg.risk.max_trade_risk_pct == 0.30
    assert cfg.risk.max_open_positions == 2
    assert cfg.filters.min_delta == 0.20
    assert cfg.filters.max_delta == 0.35


def test_high_tier_overlay_applied(tmp_path):
    path = _write_config(tmp_path, 30000.0, tiers=_TIERS)
    cfg = load_config(path)
    assert cfg.risk.max_trade_risk_pct == 0.10
    assert cfg.risk.max_open_positions == 5
    assert cfg.filters.min_delta == 0.35
    assert cfg.filters.max_delta == 0.50


def test_unlisted_keys_fall_through_to_base(tmp_path):
    # max_trades_per_week and the DTE filters are not in any tier, so they must
    # keep their base values even when a tier is applied.
    path = _write_config(tmp_path, 1000.0, tiers=_TIERS)
    cfg = load_config(path)
    assert cfg.risk.max_trades_per_week == 10
    assert cfg.filters.min_dte == 2
    assert cfg.filters.max_dte == 7


_WATCHLIST_TIERS = [
    {"min_value": 0, "watchlist": ["SPY", "QQQ"]},
    {"min_value": 5000, "watchlist": ["SPY", "QQQ", "IWM", "AAPL"]},
    {"min_value": 25000},  # no watchlist key -> base watchlist used
]


def test_small_account_watchlist_locked_to_spy_qqq(tmp_path):
    path = _write_config(tmp_path, 1000.0, tiers=_WATCHLIST_TIERS)
    cfg = load_config(path)
    assert cfg.watchlist == ["SPY", "QQQ"]


def test_mid_account_opens_up_watchlist(tmp_path):
    path = _write_config(tmp_path, 10000.0, tiers=_WATCHLIST_TIERS)
    cfg = load_config(path)
    assert cfg.watchlist == ["SPY", "QQQ", "IWM", "AAPL"]


def test_tier_without_watchlist_key_keeps_base(tmp_path):
    # $25k tier defines no watchlist -> base watchlist (from _write_config) wins.
    path = _write_config(tmp_path, 30000.0, tiers=_WATCHLIST_TIERS)
    cfg = load_config(path)
    assert cfg.watchlist == ["SPY"]

