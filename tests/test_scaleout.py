"""Tests for position sizing and partial exits (0DTE scale-out playbook)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import make_config

from killer_options_bot.config import (
    ExitConfig,
    SizingConfig,
    TrimRule,
    _build_exits,
    _build_trims,
)
from killer_options_bot.models import (
    OptionContract,
    PaperPosition,
    PositionStatus,
    Quote,
    Side,
)
from killer_options_bot.paper import PaperEngine
from killer_options_bot.storage import Storage
from killer_options_bot.web import _r_multiple


# --- A stub data source with a controllable option mid --------------------


class StubData:
    """Returns one CALL contract whose mid we can set to drive pl_pct."""

    def __init__(self, as_of: date, mid: float, strike: float = 150.0):
        self.as_of = as_of
        self.mid = mid
        self.strike = strike
        self.expiration = as_of + timedelta(days=45)

    def set_mid(self, mid: float) -> None:
        self.mid = mid

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last=self.strike + 5, closes=[])

    def get_option_chain(self, symbol: str, side: Side):
        # Zero-width spread so mid == bid == ask == our target.
        return [
            OptionContract(
                symbol="X",
                underlying=symbol,
                side=Side.CALL,
                strike=self.strike,
                expiration=self.expiration,
                bid=self.mid,
                ask=self.mid,
                last=self.mid,
                delta=0.40,
                implied_volatility=0.30,
                volume=500,
                open_interest=2000,
            )
        ]


def _open_position(
    storage: Storage, as_of: date, entry: float, qty: int
) -> PaperPosition:
    pos = PaperPosition(
        option_symbol="X",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=qty,
        entry_price=entry,
        entry_date=as_of,
        status=PositionStatus.OPEN,
        original_quantity=qty,
    )
    storage.open_position(pos)
    return pos


# Trim ladder that never triggers the terminal exit at the test price points:
# high profit target, generous holding window.
def _trim_exits(trims: tuple[TrimRule, ...]) -> ExitConfig:
    return ExitConfig(
        profit_target_pct=0.80,
        stop_loss_pct=0.30,
        max_holding_days=21,
        min_dte_exit=0,
        trims=trims,
    )


# --- Position sizing -------------------------------------------------------


def test_contracts_for_disabled_returns_one(tmp_path):
    config = make_config(tmp_path, account_value=100000.0)
    # sizing defaults to disabled.
    assert config.contracts_for(100.0) == 1


def test_contracts_for_sizes_by_risk_budget(tmp_path):
    config = make_config(
        tmp_path,
        account_value=10000.0,
        sizing=SizingConfig(
            enabled=True, max_contracts=100, risk_per_trade_pct=0.30
        ),
    )
    # budget = 10000 * 0.30 = 3000; 3000 // 100 = 30 contracts.
    assert config.contracts_for(100.0) == 30


def test_contracts_for_capped_by_max(tmp_path):
    config = make_config(
        tmp_path,
        account_value=10000.0,
        sizing=SizingConfig(
            enabled=True, max_contracts=10, risk_per_trade_pct=0.30
        ),
    )
    # Raw would be 30, capped at 10.
    assert config.contracts_for(100.0) == 10


def test_contracts_for_falls_back_to_risk_pct(tmp_path):
    config = make_config(
        tmp_path,
        account_value=10000.0,
        sizing=SizingConfig(enabled=True, max_contracts=100),
    )
    # No risk_per_trade_pct -> use risk.max_trade_risk_pct (0.05 in make_config).
    # budget = 10000 * 0.05 = 500; 500 // 100 = 5.
    assert config.contracts_for(100.0) == 5


def test_contracts_for_floors_at_one_when_expensive(tmp_path):
    config = make_config(
        tmp_path,
        account_value=1000.0,
        sizing=SizingConfig(
            enabled=True, max_contracts=10, risk_per_trade_pct=0.30
        ),
    )
    # budget = 300, but one contract costs 500 -> floor at 1.
    assert config.contracts_for(500.0) == 1


def test_open_from_candidate_auto_sizes(tmp_path):
    from killer_options_bot.models import Candidate, RiskDecision

    config = make_config(
        tmp_path,
        account_value=10000.0,
        risk=make_config(tmp_path).risk,  # keep max_open_positions=1
        sizing=SizingConfig(
            enabled=True, max_contracts=10, risk_per_trade_pct=0.30
        ),
    )
    as_of = date(2026, 1, 1)
    data = StubData(as_of, mid=1.00)
    storage = Storage(config.db_path)
    engine = PaperEngine(config, data, storage, as_of=as_of)

    contract = data.get_option_chain("AAPL", Side.CALL)[0]
    candidate = Candidate(
        contract=contract,
        side=Side.CALL,
        signal_note="t",
        decision=RiskDecision.accept(),
        max_loss=contract.cost,
    )
    pos = engine.open_from_candidate(candidate)
    assert pos is not None
    # entry cost 100/contract, budget 3000 -> 10 (capped).
    assert pos.quantity == 10
    assert pos.original_quantity == 10


# --- Trim config parsing & validation -------------------------------------


def test_build_trims_sorts_by_threshold():
    trims = _build_trims(
        [{"at_pct": 0.5, "fraction": 0.25}, {"at_pct": 0.25, "fraction": 0.5}]
    )
    assert [t.at_pct for t in trims] == [0.25, 0.5]
    assert [t.fraction for t in trims] == [0.5, 0.25]


def test_build_trims_empty_is_empty_tuple():
    assert _build_trims([]) == ()
    assert _build_trims(None) == ()


def test_build_trims_rejects_bad_threshold():
    with pytest.raises(ValueError, match="at_pct"):
        _build_trims([{"at_pct": 0.0, "fraction": 0.5}])


def test_build_trims_rejects_bad_fraction():
    with pytest.raises(ValueError, match="fraction"):
        _build_trims([{"at_pct": 0.25, "fraction": 1.0}])


def test_build_trims_rejects_fractions_summing_to_one():
    with pytest.raises(ValueError, match="sum to less than 1"):
        _build_trims(
            [
                {"at_pct": 0.25, "fraction": 0.5},
                {"at_pct": 0.5, "fraction": 0.5},
            ]
        )


def test_build_exits_carries_trims():
    cfg = _build_exits(
        {
            "profit_target_pct": 0.80,
            "stop_loss_pct": 0.30,
            "trims": [{"at_pct": 0.25, "fraction": 0.5}],
        }
    )
    assert len(cfg.trims) == 1
    assert cfg.trims[0].at_pct == 0.25


# --- Trim engine behaviour -------------------------------------------------


def test_trim_scales_out_and_banks(tmp_path):
    exits = _trim_exits((TrimRule(0.25, 0.5), TrimRule(0.50, 0.25)))
    config = make_config(tmp_path, exits=exits)
    as_of = date(2026, 1, 1)
    storage = Storage(config.db_path)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)

    data = StubData(as_of, mid=1.30)  # +30% -> only first trim fires
    engine = PaperEngine(config, data, storage, as_of=as_of)
    result = engine.manage_position(pos)

    assert not result.closed
    assert result.trimmed == 2  # sold half of 4
    assert pos.quantity == 2
    assert pos.trims_done == 1
    # banked = (1.30 - 1.00) * 100 * 2 = 60
    assert pos.realized_pl_banked == pytest.approx(60.0)

    # Persisted?
    reloaded = storage.open_positions()[0]
    assert reloaded.quantity == 2
    assert reloaded.trims_done == 1
    assert reloaded.realized_pl_banked == pytest.approx(60.0)
    assert reloaded.original_quantity == 4


def test_trim_ladder_fires_multiple_levels(tmp_path):
    exits = _trim_exits((TrimRule(0.25, 0.5), TrimRule(0.50, 0.25)))
    config = make_config(tmp_path, exits=exits)
    as_of = date(2026, 1, 1)
    storage = Storage(config.db_path)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)

    data = StubData(as_of, mid=1.60)  # +60% -> both trims fire in one tick
    engine = PaperEngine(config, data, storage, as_of=as_of)
    result = engine.manage_position(pos)

    assert not result.closed
    # level0 sells int(4*0.5)=2, level1 sells int(4*0.25)=1 -> 3 total.
    assert result.trimmed == 3
    assert pos.quantity == 1
    assert pos.trims_done == 2
    # banked = (1.60-1.00)*100*2 + (1.60-1.00)*100*1 = 120 + 60 = 180
    assert pos.realized_pl_banked == pytest.approx(180.0)


def test_final_trim_closes_the_runner(tmp_path):
    # Two equal 50% trims: level0 sells 2 of 4 (qty->2), level1 would sell
    # int(4*0.5)=2 which equals the remainder -> becomes a full close.
    exits = ExitConfig(
        profit_target_pct=0.80,
        stop_loss_pct=0.30,
        max_holding_days=21,
        min_dte_exit=0,
        trims=(TrimRule(0.25, 0.5), TrimRule(0.50, 0.5)),
    )
    config = make_config(tmp_path, exits=exits)
    as_of = date(2026, 1, 1)
    storage = Storage(config.db_path)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)

    data = StubData(as_of, mid=1.60)  # +60% -> both levels fire
    engine = PaperEngine(config, data, storage, as_of=as_of)
    result = engine.manage_position(pos)

    assert result.closed
    assert pos.status is PositionStatus.CLOSED
    closed = storage.closed_positions()
    assert len(closed) == 1
    # realized = banked from first trim (2 @ +0.60 = 120) + final leg
    # (2 @ +0.60 = 120) = 240.
    assert closed[0].realized_pl() == pytest.approx(240.0)


def test_no_trim_for_single_contract(tmp_path):
    exits = _trim_exits((TrimRule(0.25, 0.5),))
    config = make_config(tmp_path, exits=exits)
    as_of = date(2026, 1, 1)
    storage = Storage(config.db_path)
    pos = _open_position(storage, as_of, entry=1.00, qty=1)

    data = StubData(as_of, mid=1.50)  # +50%, but only one contract
    engine = PaperEngine(config, data, storage, as_of=as_of)
    result = engine.manage_position(pos)

    assert result.trimmed == 0
    assert not result.closed
    assert pos.quantity == 1
    assert pos.trims_done == 0


def test_trim_then_terminal_exit_on_runner(tmp_path):
    # First trim banks, then price hits the profit target and closes the rest.
    exits = ExitConfig(
        profit_target_pct=0.80,
        stop_loss_pct=0.30,
        max_holding_days=21,
        min_dte_exit=0,
        trims=(TrimRule(0.25, 0.5),),
    )
    config = make_config(tmp_path, exits=exits)
    as_of = date(2026, 1, 1)
    storage = Storage(config.db_path)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)

    data = StubData(as_of, mid=1.90)  # +90%: trim fires AND target hit
    engine = PaperEngine(config, data, storage, as_of=as_of)
    result = engine.manage_position(pos)

    assert result.closed
    assert "profit target" in result.reason
    closed = storage.closed_positions()[0]
    # banked (2 @ +0.90 = 180) + final leg (2 @ +0.90 = 180) = 360.
    assert closed.realized_pl() == pytest.approx(360.0)


# --- Storage & accounting --------------------------------------------------


def test_reduce_position_round_trips(tmp_path):
    config = make_config(tmp_path)
    storage = Storage(config.db_path)
    as_of = date(2026, 1, 1)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)

    storage.reduce_position(pos.id, new_quantity=2, realized_pl_banked=60.0,
                            trims_done=1)
    reloaded = storage.open_positions()[0]
    assert reloaded.quantity == 2
    assert reloaded.realized_pl_banked == pytest.approx(60.0)
    assert reloaded.trims_done == 1
    assert reloaded.original_quantity == 4


def test_realized_pl_total_includes_open_banked(tmp_path):
    config = make_config(tmp_path)
    storage = Storage(config.db_path)
    as_of = date(2026, 1, 1)
    pos = _open_position(storage, as_of, entry=1.00, qty=4)
    storage.reduce_position(pos.id, new_quantity=2, realized_pl_banked=75.0,
                            trims_done=1)

    engine = PaperEngine(config, StubData(as_of, 1.0), storage, as_of=as_of)
    # Nothing closed, but banked profit on the open runner counts as realized.
    assert engine.realized_pl_total() == pytest.approx(75.0)


def test_r_multiple_uses_initial_cost(tmp_path):
    as_of = date(2026, 1, 1)
    # Original 4 contracts @ $1.00 -> initial cost $400 (the risk basis).
    # Trimmed to a 1-contract runner, closed with total realized P/L of $200.
    pos = PaperPosition(
        option_symbol="X",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=1,
        entry_price=1.00,
        entry_date=as_of,
        status=PositionStatus.CLOSED,
        original_quantity=4,
        realized_pl_banked=150.0,
        exit_price=1.50,  # final leg: (1.50-1.00)*100*1 = 50
        exit_date=as_of,
    )
    # realized = 150 + 50 = 200; initial cost = 1.00*100*4 = 400 -> 0.5R.
    assert pos.realized_pl() == pytest.approx(200.0)
    assert _r_multiple(pos) == pytest.approx(0.5)
