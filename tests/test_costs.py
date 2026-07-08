"""Tests for the CostModel and its integration into the backtester."""

from __future__ import annotations

from datetime import date

from conftest import make_config

from killer_options_bot.backtest import Backtester
from killer_options_bot.models import CostModel, OptionContract, Side


def _contract(bid: float, ask: float) -> OptionContract:
    return OptionContract(
        symbol="TEST",
        underlying="SPY",
        side=Side.CALL,
        strike=100.0,
        expiration=date(2026, 3, 20),
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        delta=0.30,
        implied_volatility=0.3,
        volume=1000,
        open_interest=1000,
    )


def test_free_model_fills_at_mid():
    cm = CostModel.free()
    c = _contract(1.00, 1.20)  # mid 1.10
    assert cm.entry_fill(c) == 1.10
    assert cm.exit_fill(c) == 1.10


def test_full_spread_crossing_hits_ask_and_bid():
    # slippage 1.0, no commission -> buy at ask, sell at bid.
    cm = CostModel(commission_per_contract=0.0, slippage_frac=1.0)
    c = _contract(1.00, 1.20)  # mid 1.10, half-spread 0.10
    assert cm.entry_fill(c) == 1.20
    assert cm.exit_fill(c) == 1.00


def test_commission_is_folded_per_share():
    # $0.65/contract = $0.0065/share on top of the half-spread.
    cm = CostModel(commission_per_contract=0.65, slippage_frac=1.0)
    c = _contract(1.00, 1.20)  # mid 1.10, half-spread 0.10
    assert cm.entry_fill(c) == round(1.10 + 0.10 + 0.0065, 4)
    assert cm.exit_fill(c) == round(1.10 - 0.10 - 0.0065, 4)


def test_half_slippage_crosses_half_the_spread():
    cm = CostModel(commission_per_contract=0.0, slippage_frac=0.5)
    c = _contract(1.00, 1.20)  # half-spread 0.10
    assert cm.entry_fill(c) == 1.15
    assert cm.exit_fill(c) == 1.05


def test_exit_fill_floors_at_zero():
    cm = CostModel(commission_per_contract=0.0, slippage_frac=1.0)
    c = _contract(0.00, 0.05)  # mid 0.025, half-spread 0.025
    assert cm.exit_fill(c) == 0.0


def test_settle_fill_charges_commission_only():
    cm = CostModel(commission_per_contract=0.65, slippage_frac=1.0)
    assert cm.settle_fill(2.00) == round(2.00 - 0.0065, 4)
    assert cm.settle_fill(0.0) == 0.0


def test_costs_reduce_backtest_pl(tmp_path):
    # With identical inputs, a realistic cost model should never produce more
    # total P/L than the zero-cost (mid-fill) baseline.
    config = make_config(
        tmp_path, account_value=50000.0, watchlist=["SPY", "QQQ", "NVDA"]
    )
    free = Backtester(
        config,
        date(2026, 2, 1),
        date(2026, 6, 1),
        step_days=2,
        cost_model=CostModel.free(),
    ).run()
    costed = Backtester(
        config,
        date(2026, 2, 1),
        date(2026, 6, 1),
        step_days=2,
        cost_model=CostModel(),
    ).run()
    assert costed.total_pl <= free.total_pl


def test_default_backtester_applies_costs(tmp_path):
    # No cost_model arg -> realistic costs by default (not free).
    config = make_config(tmp_path, account_value=50000.0, watchlist=["SPY"])
    default = Backtester(
        config, date(2026, 2, 1), date(2026, 5, 1), step_days=3
    ).run()
    free = Backtester(
        config,
        date(2026, 2, 1),
        date(2026, 5, 1),
        step_days=3,
        cost_model=CostModel.free(),
    ).run()
    assert default.total_pl <= free.total_pl


def test_data_factory_is_pluggable(tmp_path):
    # A custom factory is used instead of the default mock builder.
    from killer_options_bot.brokers.mock import MockMarketData

    calls: list[date] = []

    def factory(as_of):
        calls.append(as_of)
        return MockMarketData(as_of=as_of)

    Backtester(
        config=make_config(tmp_path, watchlist=["SPY"]),
        start=date(2026, 2, 1),
        end=date(2026, 2, 5),
        step_days=1,
        data_factory=factory,
    ).run()
    assert calls  # factory was invoked at least once per stepped day


def test_t_stat_zero_for_tiny_sample(tmp_path):
    from killer_options_bot.backtest import BacktestStats, TradeRecord

    stats = BacktestStats(start=date(2026, 1, 1), end=date(2026, 2, 1))
    stats.trades = [
        TradeRecord("A", "SPY", "call", date(2026, 1, 1), date(2026, 1, 2),
                    1.0, 1.5, 50.0, 0.5, "profit", 1),
    ]
    assert stats.pl_std == 0.0
    assert stats.t_stat == 0.0


def test_t_stat_computed_for_sample():
    from killer_options_bot.backtest import BacktestStats, TradeRecord

    stats = BacktestStats(start=date(2026, 1, 1), end=date(2026, 2, 1))
    stats.trades = [
        TradeRecord("A", "SPY", "call", date(2026, 1, 1), date(2026, 1, 2),
                    1.0, 1.5, 50.0, 0.5, "profit", 1),
        TradeRecord("B", "SPY", "put", date(2026, 1, 1), date(2026, 1, 2),
                    1.0, 1.4, 40.0, 0.4, "profit", 1),
        TradeRecord("C", "SPY", "call", date(2026, 1, 1), date(2026, 1, 2),
                    1.0, 1.6, 60.0, 0.6, "profit", 1),
    ]
    # Mean 50, all positive -> strongly positive t-stat.
    assert stats.pl_std > 0
    assert stats.t_stat > 2
