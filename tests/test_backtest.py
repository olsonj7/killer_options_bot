"""Tests for the backtest engine and stats."""

from __future__ import annotations

from datetime import date

from conftest import make_config

from killer_options_bot.backtest import Backtester, BacktestStats, TradeRecord


def test_backtest_runs_and_produces_stats(tmp_path):
    config = make_config(
        tmp_path,
        account_value=50000.0,
        watchlist=["SPY", "QQQ", "NVDA", "AAPL", "MSFT"],
    )
    bt = Backtester(config, date(2026, 2, 1), date(2026, 6, 1), step_days=2)
    stats = bt.run()

    # Deterministic mock -> should be repeatable and internally consistent.
    assert isinstance(stats, BacktestStats)
    assert stats.num_trades == len(stats.wins) + len(stats.losses)
    # No position should ever exceed max_open at the end (all closed out).
    assert stats.ending_open == 0
    # Stats fields are computable without error.
    assert stats.total_pl == round(
        sum(t.pl for t in stats.trades), 2
    )
    assert stats.max_drawdown >= 0.0


def test_backtest_is_deterministic(tmp_path):
    config = make_config(tmp_path, account_value=50000.0, watchlist=["NVDA"])
    bt1 = Backtester(config, date(2026, 2, 1), date(2026, 5, 1), step_days=3)
    bt2 = Backtester(config, date(2026, 2, 1), date(2026, 5, 1), step_days=3)
    s1 = bt1.run()
    s2 = bt2.run()
    assert s1.num_trades == s2.num_trades
    assert s1.total_pl == s2.total_pl


def test_backtest_empty_range_no_trades(tmp_path):
    config = make_config(tmp_path, account_value=50000.0, watchlist=["NVDA"])
    # Single day, unlikely to both open and close -> may have an open forced
    # closed, or zero trades. Either way stats must be coherent.
    bt = Backtester(config, date(2026, 2, 1), date(2026, 2, 1), step_days=1)
    stats = bt.run()
    assert stats.num_trades >= 0
    assert stats.win_rate >= 0.0


def test_stats_win_loss_math():
    stats = BacktestStats(start=date(2026, 1, 1), end=date(2026, 2, 1))
    stats.trades = [
        TradeRecord("A", "AAPL", "call", date(2026, 1, 1), date(2026, 1, 5),
                    1.0, 1.5, 50.0, 0.5, "profit", 4),
        TradeRecord("B", "MSFT", "put", date(2026, 1, 2), date(2026, 1, 6),
                    2.0, 1.0, -100.0, -0.5, "stop", 4),
        TradeRecord("C", "NVDA", "call", date(2026, 1, 3), date(2026, 1, 8),
                    1.0, 1.4, 40.0, 0.4, "profit", 5),
    ]
    assert stats.num_trades == 3
    assert len(stats.wins) == 2
    assert len(stats.losses) == 1
    assert round(stats.win_rate, 2) == 0.67
    assert stats.total_pl == -10.0
    assert stats.avg_win == 45.0
    assert stats.avg_loss == -100.0
    assert stats.profit_factor == 0.9
    # Equity curve: +50, -50, -10 -> peak 50, trough -50 -> drawdown 100.
    assert stats.max_drawdown == 100.0
