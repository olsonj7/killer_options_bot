"""Tests for the paper-trading engine: fills, exits, and P&L."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import make_config

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.models import (
    Candidate,
    OptionContract,
    PaperPosition,
    PositionStatus,
    RiskDecision,
    Side,
)
from killer_options_bot.paper import PaperEngine
from killer_options_bot.storage import Storage


def make_candidate(as_of: date) -> Candidate:
    contract = OptionContract(
        symbol="AAPL260315C00150000",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        bid=1.00,
        ask=1.04,
        last=1.02,
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


def test_open_from_candidate(tmp_path):
    config = make_config(tmp_path, account_value=10000.0)
    as_of = date(2026, 1, 1)
    data = MockMarketData(as_of=as_of)
    storage = Storage(config.db_path)
    engine = PaperEngine(config, data, storage, as_of=as_of)

    position = engine.open_from_candidate(make_candidate(as_of))
    assert position is not None
    assert position.id is not None
    assert storage.count_open_positions() == 1
    assert position.entry_price == 1.02
    assert position.entry_cost == 102.0


def test_rejected_candidate_does_not_open(tmp_path):
    config = make_config(tmp_path, account_value=10000.0)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    candidate = make_candidate(as_of)
    blocked = Candidate(
        contract=candidate.contract,
        side=candidate.side,
        signal_note="x",
        decision=RiskDecision.reject("nope"),
        max_loss=0.0,
    )
    assert engine.open_from_candidate(blocked) is None


def test_max_open_positions_enforced(tmp_path):
    config = make_config(tmp_path, account_value=10000.0)  # max_open_positions=1
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    assert engine.open_from_candidate(make_candidate(as_of)) is not None
    # Second open is blocked because only 1 position is allowed.
    assert engine.open_from_candidate(make_candidate(as_of)) is None


def _position(as_of: date, entry_price: float) -> PaperPosition:
    return PaperPosition(
        option_symbol="X",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=1,
        entry_price=entry_price,
        entry_date=as_of,
    )


def test_profit_target_exit(tmp_path):
    config = make_config(tmp_path)  # profit_target 0.35
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    # +40% -> profit target.
    assert engine.exit_reason(pos, 1.40) is not None
    assert "profit target" in engine.exit_reason(pos, 1.40)


def test_stop_loss_exit(tmp_path):
    config = make_config(tmp_path)  # stop_loss 0.45
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    assert "stop loss" in engine.exit_reason(pos, 0.50)


def test_max_holding_days_exit(tmp_path):
    config = make_config(tmp_path)  # max_holding_days 21
    entry = date(2026, 1, 1)
    later = entry + timedelta(days=21)
    engine = PaperEngine(config, MockMarketData(as_of=later),
                         Storage(config.db_path), as_of=later)
    pos = _position(entry, 1.00)
    assert "max holding" in engine.exit_reason(pos, 1.00)


def _trailing_config(tmp_path):
    """Config whose base exits use a trailing stop instead of a fixed target."""
    from killer_options_bot.config import ExitConfig

    return make_config(
        tmp_path,
        exits=ExitConfig(
            profit_target_pct=0.35,
            stop_loss_pct=0.45,
            max_holding_days=21,
            min_dte_exit=0,
            trail_pct=0.20,
            trail_activate_pct=0.30,
        ),
    )


def test_trailing_stop_not_armed_below_activation(tmp_path):
    config = _trailing_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    pos.high_water_mark = 1.25  # peak only +25%, below +30% activation
    # Even a pullback should NOT exit while the trail is unarmed.
    assert engine.exit_reason(pos, 1.05) is None


def test_trailing_stop_does_not_fire_above_trigger(tmp_path):
    config = _trailing_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    pos.high_water_mark = 1.50  # armed (+50%), trigger at 1.20
    assert engine.exit_reason(pos, 1.50) is None  # at the peak
    assert engine.exit_reason(pos, 1.30) is None  # pullback above trigger


def test_trailing_stop_fires_on_giveback(tmp_path):
    config = _trailing_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    pos.high_water_mark = 1.50  # armed (+50%), trigger at 1.20
    reason = engine.exit_reason(pos, 1.20)
    assert reason is not None and "trailing stop" in reason


def test_trailing_stop_lets_winner_run_past_fixed_target(tmp_path):
    config = _trailing_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    pos.high_water_mark = 2.00
    # +100% would have closed under the fixed 0.35 target; with trailing the
    # runner keeps going and only the trail (or stop) exits.
    assert engine.exit_reason(pos, 2.00) is None


def test_trailing_stop_loss_still_applies(tmp_path):
    config = _trailing_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)  # never armed the trail
    assert "stop loss" in engine.exit_reason(pos, 0.50)


def test_high_water_mark_persists_and_reloads(tmp_path):
    config = make_config(tmp_path)
    as_of = date(2026, 1, 1)
    data = MockMarketData(as_of=as_of)
    storage = Storage(config.db_path)
    engine = PaperEngine(config, data, storage, as_of=as_of)
    pos = engine.open_from_candidate(make_candidate(as_of))
    assert pos is not None
    # Fresh position: high-water mark defaults to entry price.
    assert storage.open_positions()[0].high_water_mark == pos.entry_price
    storage.update_high_water_mark(pos.id, 3.33)
    assert storage.open_positions()[0].high_water_mark == 3.33


def test_min_dte_exit(tmp_path):
    config = make_config(tmp_path)  # min_dte_exit 21
    entry = date(2026, 1, 1)
    # Short-dated position: 28 DTE at entry, held only 10 days -> 18 DTE.
    # Max-holding (21d) has NOT triggered, so DTE is the reason.
    pos = PaperPosition(
        option_symbol="X",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=entry + timedelta(days=28),
        quantity=1,
        entry_price=1.00,
        entry_date=entry,
    )
    later = entry + timedelta(days=10)
    engine = PaperEngine(config, MockMarketData(as_of=later),
                         Storage(config.db_path), as_of=later)
    assert "DTE" in engine.exit_reason(pos, 1.05)


def test_hold_when_no_rule_triggers(tmp_path):
    config = make_config(tmp_path)
    as_of = date(2026, 1, 1)
    engine = PaperEngine(config, MockMarketData(as_of=as_of),
                         Storage(config.db_path), as_of=as_of)
    pos = _position(as_of, 1.00)
    # +10%, day 0, 45 DTE -> nothing triggers.
    assert engine.exit_reason(pos, 1.10) is None


def test_manage_and_pnl_end_to_end(tmp_path):
    """Open via mock, advance time, manage, and verify P/L is recorded."""
    config = make_config(
        tmp_path, account_value=100000.0, watchlist=["NVDA"]
    )
    open_day = date(2026, 2, 1)
    data = MockMarketData(as_of=open_day)

    # Build a candidate from a real mock contract so symbols match on re-price.
    chain = data.get_option_chain("NVDA", Side.CALL)
    contract = chain[len(chain) // 2]
    candidate = Candidate(
        contract=contract,
        side=Side.CALL,
        signal_note="test",
        decision=RiskDecision.accept(),
        max_loss=contract.cost,
    )

    storage = Storage(config.db_path)
    engine = PaperEngine(config, data, storage, as_of=open_day)
    position = engine.open_from_candidate(candidate)
    assert position is not None

    # Advance far enough that max holding days forces an exit.
    later = open_day + timedelta(days=30)
    engine_later = PaperEngine(
        config, MockMarketData(as_of=later), storage, as_of=later
    )
    results = engine_later.manage_all()
    assert len(results) == 1
    assert results[0].closed

    closed = storage.closed_positions()
    assert len(closed) == 1
    assert closed[0].status is PositionStatus.CLOSED
    assert closed[0].realized_pl() is not None
