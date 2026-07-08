"""Tests for the risk engine guardrails."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import make_config

from killer_options_bot.models import OptionContract, Side
from killer_options_bot.risk import RiskEngine


def make_contract(**overrides) -> OptionContract:
    as_of = date(2026, 1, 1)
    defaults = dict(
        symbol="AAPL260215C00150000",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        bid=0.40,
        ask=0.44,
        last=0.42,
        delta=0.38,
        implied_volatility=0.30,
        volume=500,
        open_interest=2000,
    )
    defaults.update(overrides)
    return OptionContract(**defaults)


AS_OF = date(2026, 1, 1)


def test_clean_contract_is_allowed(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    decision = engine.evaluate(
        make_contract(), trades_this_week=0, as_of=AS_OF
    )
    assert decision.allowed, decision.reasons


def test_contract_too_expensive_is_rejected(tmp_path):
    # Account 1000 * 5% = $50 max risk. A $0.60 mid = $60 cost > $50.
    engine = RiskEngine(make_config(tmp_path))
    contract = make_contract(bid=0.58, ask=0.62, last=0.60)
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert any("exceeds max trade risk" in r for r in decision.reasons)


def test_dte_out_of_range_is_rejected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    contract = make_contract(expiration=AS_OF + timedelta(days=10))
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert any("DTE" in r for r in decision.reasons)


def test_delta_out_of_band_is_rejected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    contract = make_contract(delta=0.70)
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert any("Delta" in r for r in decision.reasons)


def test_wide_spread_is_rejected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    # mid ~0.42, spread of 0.20 -> ~48% spread.
    contract = make_contract(bid=0.32, ask=0.52, last=0.42)
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert any("spread" in r for r in decision.reasons)


def test_low_liquidity_is_rejected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    contract = make_contract(volume=10, open_interest=50)
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert any("Volume" in r for r in decision.reasons)
    assert any("Open interest" in r for r in decision.reasons)


def test_weekly_limit_is_rejected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    decision = engine.evaluate(
        make_contract(), trades_this_week=2, as_of=AS_OF
    )
    assert not decision.allowed
    assert any("Weekly trade limit" in r for r in decision.reasons)


def test_multiple_reasons_are_collected(tmp_path):
    engine = RiskEngine(make_config(tmp_path))
    contract = make_contract(
        delta=0.9,
        volume=1,
        open_interest=1,
        expiration=AS_OF + timedelta(days=5),
    )
    decision = engine.evaluate(contract, trades_this_week=0, as_of=AS_OF)
    assert not decision.allowed
    assert len(decision.reasons) >= 3
