"""Tests for realistic paper fills (CostModel from config) and the
Black-Scholes mock chain (py_vollib pricing + Greeks)."""

from __future__ import annotations

import math
from datetime import date

import yaml

from killer_options_bot.brokers.mock import RISK_FREE_RATE, MockMarketData
from killer_options_bot.config import load_config
from killer_options_bot.models import CostModel, Side


def _write_config(tmp_path, costs=None):
    cfg = {
        "account": {"value": 50000.0},
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
    if costs is not None:
        cfg["costs"] = costs
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


# --- Config cost model --------------------------------------------------


def test_costs_default_to_realistic_fills(tmp_path):
    # No 'costs' block -> realistic fills (bid/ask + commission), not free.
    config = load_config(_write_config(tmp_path))
    cm = config.cost_model()
    assert cm.commission_per_contract == 0.65
    assert cm.slippage_frac == 1.0


def test_costs_can_be_disabled(tmp_path):
    config = load_config(_write_config(tmp_path, costs={"enabled": False}))
    cm = config.cost_model()
    # Disabled -> equivalent to CostModel.free().
    assert cm.commission_per_contract == 0.0
    assert cm.slippage_frac == 0.0


def test_costs_custom_values(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            costs={"commission_per_contract": 1.0, "slippage_frac": 0.5},
        )
    )
    cm = config.cost_model()
    assert cm.commission_per_contract == 1.0
    assert cm.slippage_frac == 0.5


def test_costs_reject_bad_slippage(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, costs={"slippage_frac": 1.5}))


def test_costs_reject_negative_commission(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        load_config(
            _write_config(tmp_path, costs={"commission_per_contract": -1.0})
        )


# --- Black-Scholes mock chain -------------------------------------------


def test_mock_prices_match_black_scholes():
    from py_vollib.black_scholes import black_scholes

    as_of = date(2026, 2, 1)
    data = MockMarketData(as_of=as_of)
    last = data.get_quote("SPY").last
    chain = data.get_option_chain("SPY", Side.CALL)

    # Pick a mid-dated, near-the-money contract and reprice it independently.
    near = min(
        (c for c in chain if c.dte(as_of) >= 20),
        key=lambda c: abs(c.strike - last),
    )
    t = near.dte(as_of) / 365.0
    expected = black_scholes(
        "c", last, near.strike, t, RISK_FREE_RATE, near.implied_volatility
    )
    assert abs(near.mid - round(expected, 2)) <= 0.02


def test_mock_atm_delta_near_half():
    as_of = date(2026, 2, 1)
    data = MockMarketData(as_of=as_of)
    last = data.get_quote("SPY").last
    chain = data.get_option_chain("SPY", Side.CALL)
    atm = min(chain, key=lambda c: abs(c.strike - last))
    # An at-the-money call has a delta close to 0.5.
    assert 0.45 <= atm.delta <= 0.60


def test_mock_delta_signs_and_range():
    as_of = date(2026, 2, 1)
    data = MockMarketData(as_of=as_of)
    calls = data.get_option_chain("SPY", Side.CALL)
    puts = data.get_option_chain("SPY", Side.PUT)
    # Calls: 0..1, puts: -1..0.
    assert all(0.0 <= c.delta <= 1.0 for c in calls)
    assert all(-1.0 <= p.delta <= 0.0 for p in puts)
    # The chain spans a wide delta range so in-band strikes always exist.
    assert min(c.delta for c in calls) < 0.15
    assert max(c.delta for c in calls) > 0.85


def test_mock_put_call_parity_roughly_holds():
    # C - P ~= S - K*e^{-rt} for the same strike/expiry.
    as_of = date(2026, 2, 1)
    data = MockMarketData(as_of=as_of)
    last = data.get_quote("SPY").last
    calls = {(c.strike, c.expiration): c for c in data.get_option_chain("SPY", Side.CALL)}
    puts = {(p.strike, p.expiration): p for p in data.get_option_chain("SPY", Side.PUT)}
    key = min(
        (k for k in calls if k in puts and calls[k].dte(as_of) >= 20),
        key=lambda k: abs(k[0] - last),
    )
    call = calls[key]
    put = puts[key]
    t = call.dte(as_of) / 365.0
    lhs = call.mid - put.mid
    rhs = last - key[0] * math.exp(-RISK_FREE_RATE * t)
    # Same IV both sides -> parity holds up to rounding/spread noise.
    assert abs(lhs - rhs) <= 0.15
