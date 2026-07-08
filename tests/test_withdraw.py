"""Tests for the withdrawal advisor."""

from __future__ import annotations

from killer_options_bot.config import WithdrawConfig
from killer_options_bot.withdraw import advise, compute_equity


def _cfg(**overrides) -> WithdrawConfig:
    base = dict(
        enabled=True,
        starting_capital=1000.0,
        min_trading_balance=1000.0,
        skim_pct=0.0,
        tax_reserve_pct=0.0,
        milestones=(),
        drawdown_trigger_pct=0.0,
        drawdown_defense_pct=0.0,
    )
    base.update(overrides)
    return WithdrawConfig(**base)


def _kinds(advice):
    return {r.kind for r in advice.recommendations}


def _by_kind(advice, kind):
    return next(r for r in advice.recommendations if r.kind == kind)


# --- compute_equity --------------------------------------------------------


def test_compute_equity_tracks_peak_and_current():
    equity, peak = compute_equity(1000.0, [200.0, -100.0, 50.0])
    assert equity == 1150.0
    assert peak == 1200.0  # high-water mark after the first +200


def test_compute_equity_empty_series():
    equity, peak = compute_equity(1000.0, [])
    assert equity == 1000.0
    assert peak == 1000.0


# --- disabled / no-op ------------------------------------------------------


def test_disabled_returns_no_recommendations():
    advice = advise(_cfg(enabled=False, skim_pct=0.25), 2000.0, 2000.0)
    assert advice.enabled is False
    assert advice.recommendations == []


def test_no_rules_configured_gives_no_actions():
    advice = advise(_cfg(), 2000.0, 2000.0)
    assert advice.recommendations == []
    assert advice.gain == 1000.0
    assert advice.has_action is False


# --- profit skim -----------------------------------------------------------


def test_profit_skim_fires_on_new_high():
    advice = advise(_cfg(skim_pct=0.25), 2000.0, 2000.0)
    rec = _by_kind(advice, "profit_skim")
    assert rec.amount == 250.0  # 25% of the $1000 gain


def test_profit_skim_silent_below_peak():
    # Equity below the high-water mark -> don't nag.
    advice = advise(_cfg(skim_pct=0.25), 1500.0, 2000.0)
    assert "profit_skim" not in _kinds(advice)


def test_profit_skim_silent_when_no_gain():
    advice = advise(_cfg(skim_pct=0.25), 1000.0, 1000.0)
    assert "profit_skim" not in _kinds(advice)


def test_profit_skim_respects_min_trading_balance():
    # Big skim would drop below the floor -> capped so equity - amount == floor.
    advice = advise(
        _cfg(skim_pct=0.90, min_trading_balance=1500.0), 2000.0, 2000.0
    )
    rec = _by_kind(advice, "profit_skim")
    assert rec.amount == 500.0  # capped at equity - floor, not 0.9 * 1000


# --- milestones ------------------------------------------------------------


def test_milestone_reports_highest_reached():
    cfg = _cfg(milestones=((2000.0, 1000.0), (5000.0, 1500.0)))
    advice = advise(cfg, 5200.0, 5200.0)
    rec = _by_kind(advice, "milestone")
    assert rec.amount == 1500.0


def test_milestone_not_reached():
    cfg = _cfg(milestones=((2000.0, 1000.0),))
    advice = advise(cfg, 1500.0, 1800.0)
    assert "milestone" not in _kinds(advice)


# --- tax reserve -----------------------------------------------------------


def test_tax_reserve_on_realized_gain():
    advice = advise(_cfg(tax_reserve_pct=0.30), 2000.0, 2000.0)
    rec = _by_kind(advice, "tax_reserve")
    assert rec.amount == 300.0  # 30% of the $1000 gain


def test_tax_reserve_silent_without_gain():
    advice = advise(_cfg(tax_reserve_pct=0.30), 900.0, 1000.0)
    assert "tax_reserve" not in _kinds(advice)


# --- drawdown defense ------------------------------------------------------


def test_drawdown_defense_fires_past_trigger():
    cfg = _cfg(drawdown_trigger_pct=0.20, drawdown_defense_pct=0.50)
    # 25% below the $2000 peak -> triggers.
    advice = advise(cfg, 1500.0, 2000.0)
    rec = _by_kind(advice, "drawdown_defense")
    assert rec.amount == 750.0  # 50% of current equity


def test_drawdown_defense_silent_within_trigger():
    cfg = _cfg(drawdown_trigger_pct=0.20, drawdown_defense_pct=0.50)
    # Only 10% below peak -> no defensive action.
    advice = advise(cfg, 1800.0, 2000.0)
    assert "drawdown_defense" not in _kinds(advice)


def test_drawdown_defense_may_go_below_trading_floor():
    # Defensive rule ignores min_trading_balance on purpose.
    cfg = _cfg(
        drawdown_trigger_pct=0.20,
        drawdown_defense_pct=0.90,
        min_trading_balance=1000.0,
    )
    advice = advise(cfg, 1200.0, 2000.0)
    rec = _by_kind(advice, "drawdown_defense")
    assert rec.amount == 1080.0  # 90% of 1200, below the 1000 floor -> allowed


# --- advice metadata -------------------------------------------------------


def test_advice_drawdown_pct():
    advice = advise(_cfg(), 1600.0, 2000.0)
    assert advice.drawdown_pct == 0.20


def test_multiple_rules_can_fire_together():
    cfg = _cfg(skim_pct=0.25, tax_reserve_pct=0.30)
    advice = advise(cfg, 2000.0, 2000.0)
    assert _kinds(advice) == {"profit_skim", "tax_reserve"}
