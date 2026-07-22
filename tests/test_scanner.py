"""End-to-end scanner test using the mock data source."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import make_config

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import StrategyConfig
from killer_options_bot.models import OptionContract, Quote, Side
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


def test_scan_not_skipped_for_other_strategy(tmp_path):
    # The held-underlying skip is per (strategy, underlying): a position opened
    # by ANOTHER strategy must not hide an opportunity for this one (e.g. a
    # weekly swing hold should not suppress a 0DTE scalp on the same name).
    from datetime import timedelta

    from killer_options_bot.models import PaperPosition, PositionStatus, Side

    config = make_config(tmp_path, watchlist=["SPY"])
    as_of = date(2026, 1, 1)
    data = MockMarketData(as_of=as_of)
    storage = Storage(config.db_path)
    scanner = Scanner(config, data, storage, as_of=as_of)
    strategy = config.active_strategies[0]  # "default"

    # SPY held by a DIFFERENT strategy.
    storage.open_position(
        PaperPosition(
            option_symbol="SPY260206C00370000",
            underlying="SPY",
            side=Side.CALL,
            strike=370.0,
            expiration=as_of + timedelta(days=1),
            quantity=1,
            entry_price=1.0,
            entry_date=as_of,
            status=PositionStatus.OPEN,
            strategy="zerodte",
        )
    )

    # The default strategy still scans SPY and produces its candidate.
    candidate = scanner.scan_symbol_strategy("SPY", strategy)
    assert candidate is not None
    assert candidate.contract.underlying == "SPY"


# --- conflict_group: cadence-mates must not hold opposite sides -----------


class _StubPutData:
    """Deterministically triggers a PUT via momentum_signal on daily closes."""

    def get_quote(self, symbol: str) -> Quote:
        closes = [float(v) for v in range(124, 99, -1)]  # steady decline
        return Quote(symbol=symbol, last=closes[-1], closes=closes)

    def get_option_chain(self, symbol: str, side: Side):
        return [
            OptionContract(
                symbol="X",
                underlying=symbol,
                side=side,
                strike=100.0,
                expiration=date(2026, 1, 1) + timedelta(days=45),
                bid=1.0,
                ask=1.0,
                last=1.0,
                delta=-0.35 if side == Side.PUT else 0.35,
                implied_volatility=0.30,
                volume=500,
                open_interest=2000,
            )
        ]


def test_conflict_group_blocks_opposite_side_across_strategies(tmp_path):
    from datetime import timedelta

    from killer_options_bot.models import PaperPosition, PositionStatus

    config = make_config(
        tmp_path,
        watchlist=["AAPL"],
        strategies=(
            StrategyConfig(
                name="default",
                signal="momentum",
                filters=make_config(tmp_path).filters,
                exits=make_config(tmp_path).exits,
                conflict_group="weekly",
            ),
            StrategyConfig(
                name="weekly_reversion",
                signal="momentum",
                filters=make_config(tmp_path).filters,
                exits=make_config(tmp_path).exits,
                conflict_group="weekly",
            ),
        ),
    )
    as_of = date(2026, 1, 1)
    data = _StubPutData()
    storage = Storage(config.db_path)
    scanner = Scanner(config, data, storage, as_of=as_of)

    # weekly_reversion already holds a CALL on AAPL.
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
            strategy="weekly_reversion",
        )
    )

    default_strategy = next(
        s for s in config.active_strategies if s.name == "default"
    )
    # The daily momentum signal wants a PUT, but "default" shares a
    # conflict_group with weekly_reversion, which already holds the opposite
    # side (a CALL) -- the entry must be blocked.
    assert scanner.scan_symbol_strategy("AAPL", default_strategy) is None


def test_no_conflict_group_allows_opposite_side(tmp_path):
    from datetime import timedelta

    from killer_options_bot.models import PaperPosition, PositionStatus

    base = make_config(tmp_path, watchlist=["AAPL"])
    config = make_config(
        tmp_path,
        watchlist=["AAPL"],
        strategies=(
            StrategyConfig(
                name="default",
                signal="momentum",
                filters=base.filters,
                exits=base.exits,
                # No conflict_group set -- unaffected by other strategies'
                # positions.
            ),
        ),
    )
    as_of = date(2026, 1, 1)
    data = _StubPutData()
    storage = Storage(config.db_path)
    scanner = Scanner(config, data, storage, as_of=as_of)

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
            strategy="weekly_reversion",
        )
    )

    default_strategy = config.active_strategies[0]
    candidate = scanner.scan_symbol_strategy("AAPL", default_strategy)
    assert candidate is not None
    assert candidate.side is Side.PUT
