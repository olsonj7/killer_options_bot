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


def test_scan_skips_held_underlying(tmp_path):
    # Once a position is open on a name, scanning it should return nothing and
    # log no candidate -- no "already holding" noise, no wasted chain fetch.
    from datetime import timedelta

    from killer_options_bot.models import PaperPosition, PositionStatus, Side

    config = make_config(tmp_path, watchlist=["AAPL"])
    as_of = date(2026, 1, 1)
    data = MockMarketData(as_of=as_of)
    storage = Storage(config.db_path)
    scanner = Scanner(config, data, storage, as_of=as_of)
    strategy = config.active_strategies[0]

    # Open an AAPL position directly.
    storage.open_position(
        PaperPosition(
            option_symbol="AAPL260315C00150000",
            underlying="AAPL",
            side=Side.CALL,
            strike=150.0,
            expiration=as_of + timedelta(days=45),
            quantity=1,
            entry_price=1.0,
            entry_date=as_of,
            status=PositionStatus.OPEN,
        )
    )

    # Scanning the held name is skipped entirely: no candidate, nothing logged.
    assert scanner.scan_symbol_strategy("AAPL", strategy) is None
    assert storage.recent_candidates(limit=100) == []
