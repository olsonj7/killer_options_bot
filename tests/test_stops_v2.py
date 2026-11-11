"""Profit-lock ratchet, structure (underlying) stop, time stop, and the
position state they need (MAE, entry underlying, stop level)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from conftest import make_config
from test_scaleout import StubData
from test_strat_combo import TODAY, _daily, _two_one_two

from killer_options_bot.config import (
    ContractFilters,
    ExitConfig,
    RatchetRule,
    StrategyConfig,
    _build_exits,
)
from killer_options_bot.market import EASTERN
from killer_options_bot.models import (
    Bar,
    OptionContract,
    PaperPosition,
    PositionStatus,
    Quote,
    Side,
)
from killer_options_bot.paper import PaperEngine
from killer_options_bot.scanner import Scanner, _combo_stop_level
from killer_options_bot.storage import Storage

AS_OF = date(2026, 1, 1)


def _exits(**kw) -> ExitConfig:
    base = dict(
        profit_target_pct=2.0, stop_loss_pct=0.30, max_holding_days=21,
        min_dte_exit=0,
    )
    base.update(kw)
    return ExitConfig(**base)


def _position(**kw) -> PaperPosition:
    base = dict(
        option_symbol="X", underlying="AAPL", side=Side.CALL, strike=150.0,
        expiration=AS_OF + timedelta(days=45), quantity=1, entry_price=1.00,
        entry_date=AS_OF, status=PositionStatus.OPEN,
    )
    base.update(kw)
    return PaperPosition(**base)


def _engine(tmp_path, exits, mid=1.0, now_fn=None):
    config = make_config(tmp_path, exits=exits)
    storage = Storage(config.db_path)
    data = StubData(AS_OF, mid=mid)
    return PaperEngine(config, data, storage, as_of=AS_OF, now_fn=now_fn), data, storage


# --- ratchet config --------------------------------------------------------


def test_ratchet_parses_sorted():
    e = _build_exits({"ratchet": [
        {"peak_pct": 0.2, "floor_pct": 0.0}, {"peak_pct": 0.1, "floor_pct": -0.1},
    ]})
    assert e.ratchet == (RatchetRule(0.1, -0.1), RatchetRule(0.2, 0.0))


@pytest.mark.parametrize("tiers", [
    [{"peak_pct": 0.1, "floor_pct": 0.1}],                                 # floor == peak
    [{"peak_pct": 0.1, "floor_pct": 0.0}, {"peak_pct": 0.2, "floor_pct": -0.1}],  # loosens
    [{"peak_pct": 0.0, "floor_pct": -0.1}],                                # zero peak
])
def test_ratchet_rejects_bad_tiers(tiers):
    with pytest.raises(ValueError):
        _build_exits({"ratchet": tiers})


# --- ratchet behaviour -----------------------------------------------------

TIERS = (RatchetRule(0.10, -0.10), RatchetRule(0.20, 0.0))


def test_ratchet_floor_rises_with_peak(tmp_path):
    engine, _, _ = _engine(tmp_path, _exits(ratchet=TIERS))
    # Never reached +10%: the normal -30% stop still governs, so -15% holds.
    assert engine.exit_reason(_position(high_water_mark=1.05), 0.85) is None
    # Peaked +12% -> floor -10%: a fall to -12% exits well before the -30% stop.
    reason = engine.exit_reason(_position(high_water_mark=1.12), 0.88)
    assert reason and reason.startswith("profit-lock stop hit")
    # Peaked +25% -> floor breakeven.
    assert engine.exit_reason(_position(high_water_mark=1.25), 1.02) is None
    assert engine.exit_reason(_position(high_water_mark=1.25), 1.00)


def test_ratchet_never_fires_on_a_trade_that_never_peaked(tmp_path):
    engine, _, _ = _engine(tmp_path, _exits(ratchet=TIERS))
    assert engine.exit_reason(_position(), 0.80) is None  # -20%, stop is -30%


# --- structure stop --------------------------------------------------------


def test_structure_stop_call_with_buffer(tmp_path):
    exits = _exits(underlying_stop_enabled=True, stop_loss_pct=0.9)
    engine, data, storage = _engine(tmp_path, exits)
    pos = _position(underlying_stop=150.0)
    storage.open_position(pos)

    data.underlying_last = 152.0
    assert not engine.manage_position(pos).closed
    data.underlying_last = 149.95  # through 150 but inside the 5 bp buffer
    assert not engine.manage_position(pos).closed
    data.underlying_last = 149.90
    result = engine.manage_position(pos)
    assert result.closed and "structure stop hit" in result.reason


def test_structure_stop_put_mirror(tmp_path):
    engine, _, _ = _engine(tmp_path, _exits(underlying_stop_enabled=True))
    pos = _position(side=Side.PUT, underlying_stop=100.0)
    assert engine.exit_reason(pos, 1.0, underlying_price=99.0) is None
    assert "structure stop" in engine.exit_reason(pos, 1.0, underlying_price=100.2)


def test_structure_stop_ignored_when_disabled_or_missing(tmp_path):
    engine, _, _ = _engine(tmp_path, _exits(underlying_stop_enabled=False))
    assert engine.exit_reason(
        _position(underlying_stop=150.0), 1.0, underlying_price=1.0
    ) is None
    engine2, _, _ = _engine(tmp_path, _exits(underlying_stop_enabled=True))
    assert engine2.exit_reason(_position(), 1.0, underlying_price=1.0) is None


def test_premium_backstop_still_fires_first_when_deeper(tmp_path):
    engine, _, _ = _engine(
        tmp_path, _exits(underlying_stop_enabled=True, stop_loss_pct=0.30)
    )
    pos = _position(underlying_stop=100.0)
    reason = engine.exit_reason(pos, 0.65, underlying_price=155.0)
    assert reason and reason.startswith("stop loss hit")


# --- time stop -------------------------------------------------------------


def _clock(hh: int, mm: int):
    return lambda: datetime(2026, 1, 1, hh, mm, tzinfo=EASTERN)


def _timed(tmp_path, now, **exit_kw):
    exits = _exits(time_stop_minutes=45, **exit_kw)
    engine, _, _ = _engine(tmp_path, exits, now_fn=now)
    return engine


def test_time_stop_fires_on_stalled_red_trade(tmp_path):
    pos = _position(entry_time="10:00:00")
    assert _timed(tmp_path, _clock(10, 44)).exit_reason(pos, 0.95) is None
    reason = _timed(tmp_path, _clock(10, 46)).exit_reason(pos, 0.95)
    assert reason and reason.startswith("time stop")


def test_time_stop_spares_green_or_previously_green_trades(tmp_path):
    engine = _timed(tmp_path, _clock(11, 30))
    assert engine.exit_reason(_position(entry_time="10:00:00"), 1.02) is None
    assert engine.exit_reason(
        _position(entry_time="10:00:00", high_water_mark=1.08), 0.97
    ) is None


def test_time_stop_is_same_day_only(tmp_path):
    engine = _timed(tmp_path, _clock(11, 30))
    overnight = _position(entry_time="10:00:00", entry_date=AS_OF - timedelta(days=1))
    assert engine.exit_reason(overnight, 0.95) is None
    assert engine.exit_reason(_position(), 0.95) is None  # no entry_time


# --- position state: MAE, entry underlying, stop level ---------------------


def test_low_water_mark_tracks_adverse_excursion(tmp_path):
    engine, data, storage = _engine(tmp_path, _exits(stop_loss_pct=0.9))
    pos = _position()
    storage.open_position(pos)
    for mid in (0.90, 0.70, 0.95):
        data.set_mid(mid)
        engine.manage_position(pos)
    assert storage.open_positions()[0].low_water_mark == pytest.approx(0.70)


def test_stop_state_round_trips_and_survives_archive(tmp_path):
    _, _, storage = _engine(tmp_path, _exits())
    pos = _position(entry_underlying=151.5, underlying_stop=150.0)
    storage.open_position(pos)
    stored = storage.open_positions()[0]
    assert (stored.entry_underlying, stored.underlying_stop) == (151.5, 150.0)

    storage.close_position(stored.id, 1.1, AS_OF, "test")
    storage.archive_and_clear_positions("t")
    archived = storage.archived_positions()[0]
    assert (archived.entry_underlying, archived.underlying_stop) == (151.5, 150.0)
    assert archived.low_water_mark == pytest.approx(1.0)


# --- STRAT combo stop level ------------------------------------------------


def _bar(high, low):
    return Bar(open=low, high=high, low=low, close=high)


def test_combo_stop_level_modes():
    setup, inside = _bar(102.0, 100.0), _bar(101.5, 100.4)  # setup mid 101.0
    assert _combo_stop_level(Side.CALL, inside, setup, 102.0, "inside_extreme") == 100.4
    assert _combo_stop_level(Side.CALL, inside, setup, 102.0, "setup_mid") == 101.0
    # Setup mid not below price -> fall back to the inside-bar extreme.
    assert _combo_stop_level(Side.CALL, inside, setup, 100.9, "setup_mid") == 100.4
    assert _combo_stop_level(Side.PUT, inside, setup, 99.0, "inside_extreme") == 101.5
    assert _combo_stop_level(Side.PUT, inside, setup, 99.0, "setup_mid") == 101.0


# --- scanner: stop level on candidates + too-wide rejection ----------------


class _ComboData:
    def get_quote(self, symbol):
        return Quote(symbol=symbol, last=101.6, closes=[100.0] * 30)

    def get_intraday_bars(self, symbol, interval="15min"):
        return _two_one_two()

    def get_daily_bars(self, symbol, lookback=5):
        return _daily()

    def get_option_chain(self, symbol, side):
        return [OptionContract(
            symbol="SPY261102C00101000", underlying=symbol, side=side, strike=101.0,
            expiration=date(2026, 11, 5) + timedelta(days=2), bid=2.95, ask=3.05,
            last=3.0, delta=0.50, implied_volatility=0.30, volume=500,
            open_interest=2000,
        )]


def _combo_candidate(tmp_path, stop_loss_pct, structure=True):
    cfg = make_config(tmp_path, watchlist=["SPY"], account_value=100000.0)
    strat = StrategyConfig(
        name="strat", signal="strat_combo", scan_interval_minutes=5,
        filters=ContractFilters(
            min_dte=1, max_dte=5, min_delta=0.3, max_delta=0.7,
            max_spread_pct=0.2, min_volume=0, min_open_interest=0,
        ),
        exits=_exits(stop_loss_pct=stop_loss_pct, underlying_stop_enabled=structure),
    )
    scanner = Scanner(cfg, _ComboData(), Storage(cfg.db_path), as_of=date(2026, 11, 5))
    return cfg, scanner.scan_symbol_strategy("SPY", strat)


def test_combo_candidate_carries_stop_level(tmp_path):
    # Stop = inside low 100.3, price 101.6: est. loss 1.3*0.5*1.2/3.0 = 26%.
    _, cand = _combo_candidate(tmp_path, stop_loss_pct=0.35)
    assert cand.decision.allowed
    assert cand.stop_level == pytest.approx(100.3)
    assert cand.underlying_price == pytest.approx(101.6)


def test_combo_rejected_when_structure_stop_too_far(tmp_path):
    _, cand = _combo_candidate(tmp_path, stop_loss_pct=0.20)
    assert not cand.decision.allowed
    assert any("structure stop" in r for r in cand.decision.reasons)


def test_no_stop_level_when_structure_stop_disabled(tmp_path):
    _, cand = _combo_candidate(tmp_path, stop_loss_pct=0.20, structure=False)
    assert cand.stop_level is None and cand.decision.allowed


def test_opened_position_stores_stop_state(tmp_path):
    cfg, cand = _combo_candidate(tmp_path, stop_loss_pct=0.35)
    storage = Storage(cfg.db_path)
    engine = PaperEngine(cfg, _ComboData(), storage, as_of=date(2026, 11, 5))
    pos = engine.open_from_candidate(cand)
    stored = storage.open_positions()[0]
    assert pos.underlying_stop == stored.underlying_stop == pytest.approx(100.3)
    assert stored.entry_underlying == pytest.approx(101.6)
