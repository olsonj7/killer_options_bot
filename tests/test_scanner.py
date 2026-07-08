"""End-to-end scanner test using the mock data source."""

from __future__ import annotations

from datetime import date

from conftest import make_config

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.scanner import Scanner
from killer_options_bot.storage import Storage


def test_scanner_runs_and_logs(tmp_path):
    config = make_config(
        tmp_path,
        watchlist=["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
    )
    as_of = date(2026, 1, 1)
    data = MockMarketData(as_of=as_of)
    storage = Storage(config.db_path)
    scanner = Scanner(config, data, storage, as_of=as_of)

    candidates = scanner.scan()

    # The scanner should evaluate deterministically and log what it evaluated.
    logged = storage.recent_candidates(limit=100)
    assert len(logged) == len(candidates)
    # Every candidate carries a decision with reasons list.
    for c in candidates:
        assert c.decision.allowed or c.decision.reasons


def test_storage_weekly_count(tmp_path):
    config = make_config(tmp_path)
    storage = Storage(config.db_path)
    assert storage.trades_this_week() == 0
