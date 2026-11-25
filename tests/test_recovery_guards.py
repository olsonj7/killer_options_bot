"""Recovery entry gates: broad index regime + strategy allowed sides."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import make_config

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.cli import _rank_candidates
from killer_options_bot.config import SignalConfig, StrategyConfig
from killer_options_bot.models import Candidate, OptionContract, Quote, RiskDecision, Side
from killer_options_bot.scanner import Scanner, _candidate_quality
from killer_options_bot.storage import Storage

AS_OF = date(2026, 11, 24)


def _signal_config(**changes) -> SignalConfig:
    values = dict(
        sma_period=20,
        rsi_period=14,
        rsi_min=45,
        rsi_max=100,
        slope_lookback=3,
        rsi_sanity_only=True,
        require_index_confirmation=True,
    )
    values.update(changes)
    return SignalConfig(**values)


def _up(symbol: str) -> list[float]:
    start = 100 + len(symbol)
    return [start + i for i in range(40)]


def _down(symbol: str) -> list[float]:
    start = 200 + len(symbol)
    return [start - i for i in range(40)]


class _RecoveryData(MarketData):
    def __init__(self, directions: dict[str, str]):
        self.directions = directions
        self.quote_calls: dict[str, int] = {}

    def get_quote(self, symbol: str) -> Quote:
        self.quote_calls[symbol] = self.quote_calls.get(symbol, 0) + 1
        closes = _up(symbol) if self.directions[symbol] == "up" else _down(symbol)
        return Quote(symbol=symbol, last=closes[-1], closes=closes)

    def get_intraday_closes(self, symbol: str, interval: str = "5min"):
        closes = _up(symbol) if self.directions[symbol] == "up" else _down(symbol)
        return closes

    def get_option_chain(self, symbol: str, side: Side):
        delta = 0.35 if side is Side.CALL else -0.35
        return [
            OptionContract(
                symbol=f"{symbol}RECOVERY{'C' if side is Side.CALL else 'P'}00350000",
                underlying=symbol,
                side=side,
                strike=135.0,
                expiration=AS_OF + timedelta(days=45),
                bid=0.99,
                ask=1.01,
                last=1.0,
                delta=delta,
                implied_volatility=0.30,
                volume=1000,
                open_interest=2000,
            )
        ]


def _scanner(tmp_path, directions: dict[str, str]):
    config = make_config(
        tmp_path,
        account_value=100000.0,
        watchlist=["SPY", "QQQ", "AAPL"],
        signal=_signal_config(),
    )
    data = _RecoveryData(directions)
    scanner = Scanner(config, data, Storage(config.db_path), as_of=AS_OF)
    strategy = config.active_strategies[0]
    return config, data, scanner, strategy


def test_index_regime_rejects_call_while_spy_and_qqq_are_bearish(tmp_path):
    _cfg, _data, scanner, strategy = _scanner(
        tmp_path, {"SPY": "down", "QQQ": "down", "AAPL": "up"}
    )
    candidate = scanner.scan_symbol_strategy("AAPL", strategy)
    assert candidate is not None and candidate.side is Side.CALL
    assert not candidate.decision.allowed
    assert any("opposes index regime" in reason for reason in candidate.decision.reasons)


def test_index_regime_allows_put_with_bearish_indexes(tmp_path):
    _cfg, _data, scanner, strategy = _scanner(
        tmp_path, {"SPY": "down", "QQQ": "down", "AAPL": "down"}
    )
    candidate = scanner.scan_symbol_strategy("AAPL", strategy)
    assert candidate is not None and candidate.side is Side.PUT
    assert candidate.decision.allowed
    assert "index regime SPY=put, QQQ=put" in candidate.signal_note


def test_mixed_index_regime_rejects_candidate(tmp_path):
    _cfg, _data, scanner, strategy = _scanner(
        tmp_path, {"SPY": "down", "QQQ": "up", "AAPL": "down"}
    )
    candidate = scanner.scan_symbol_strategy("AAPL", strategy)
    assert candidate is not None
    assert not candidate.decision.allowed
    assert any("index regime unconfirmed" in reason for reason in candidate.decision.reasons)


def test_intraday_regime_checks_put_against_bearish_indices(tmp_path):
    config, _data, scanner, base = _scanner(
        tmp_path, {"SPY": "down", "QQQ": "down", "AAPL": "down"}
    )
    strategy = StrategyConfig(
        name="zerodte",
        signal="intraday_momentum",
        filters=base.filters,
        exits=base.exits,
        scan_interval_minutes=2,
        allowed_sides=(Side.PUT,),
    )
    candidate = scanner.scan_symbol_strategy("AAPL", strategy)
    assert candidate is not None and candidate.decision.allowed
    assert "index regime SPY=put, QQQ=put" in candidate.signal_note

    bullish_data = _RecoveryData({"SPY": "up", "QQQ": "up", "AAPL": "down"})
    opposite_scanner = Scanner(
        config, bullish_data, Storage(config.db_path), as_of=AS_OF
    )
    candidate = opposite_scanner.scan_symbol_strategy("AAPL", strategy)
    assert candidate is not None and not candidate.decision.allowed
    assert any("opposes index regime" in reason for reason in candidate.decision.reasons)


def test_regime_is_cached_per_scan(tmp_path):
    _cfg, data, scanner, strategy = _scanner(
        tmp_path, {"SPY": "down", "QQQ": "down", "AAPL": "down"}
    )
    scanner.scan_symbol_strategy("AAPL", strategy)
    assert data.quote_calls["SPY"] == 1
    assert data.quote_calls["QQQ"] == 1
    scanner.scan_symbol_strategy("SPY", strategy)
    assert data.quote_calls["SPY"] == 2  # the symbol's own quote still refreshes
    assert data.quote_calls["QQQ"] == 1  # regime quote remains cached


def test_strategy_allowed_sides_filters_before_option_chain(tmp_path):
    config, data, scanner, strategy = _scanner(
        tmp_path, {"SPY": "up", "QQQ": "up", "AAPL": "up"}
    )
    put_only = StrategyConfig(
        name=strategy.name,
        signal=strategy.signal,
        filters=strategy.filters,
        exits=strategy.exits,
        scan_interval_minutes=strategy.scan_interval_minutes,
        max_trades_per_day=strategy.max_trades_per_day,
        skip_midday=strategy.skip_midday,
        conflict_group=strategy.conflict_group,
        allowed_sides=(Side.PUT,),
    )
    assert scanner.scan_symbol_strategy("AAPL", put_only) is None


def test_candidate_quality_prefers_tight_liquid_contract(tmp_path):
    from dataclasses import replace

    config = make_config(tmp_path, account_value=100000.0)
    base = _RecoveryData({"SPY": "down"}).get_option_chain("SPY", Side.PUT)[0]
    tight = replace(base, bid=1.99, ask=2.01, last=2.0, volume=5000, open_interest=5000)
    loose = replace(base, bid=1.80, ask=2.20, last=2.0, volume=1, open_interest=100)
    assert _candidate_quality(tight, config) > _candidate_quality(loose, config)
    assert 0 <= _candidate_quality(tight, config) <= 1


def test_global_ranker_prefers_quality_over_strategy_order():
    contract = _RecoveryData({"SPY": "down"}).get_option_chain("SPY", Side.PUT)[0]
    first = Candidate(
        contract=contract,
        side=Side.PUT,
        signal_note="default",
        decision=RiskDecision.accept(),
        max_loss=100,
        strategy="default",
        quality_score=0.20,
    )
    better = Candidate(
        contract=contract,
        side=Side.PUT,
        signal_note="zerodte",
        decision=RiskDecision.accept(),
        max_loss=100,
        strategy="zerodte",
        quality_score=0.90,
    )
    assert [c.strategy for c in _rank_candidates([first, better])] == [
        "zerodte",
        "default",
    ]


def test_recovery_yaml_is_put_only_and_ratchet_off():
    from killer_options_bot.config import load_config

    config = load_config("config.yaml")
    assert config.risk.max_open_positions == 2
    assert config.risk.max_trades_per_day == 2
    assert config.signal.require_index_confirmation
    by_name = {strategy.name: strategy for strategy in config.active_strategies}
    assert by_name["default"].allowed_sides == (Side.PUT,)
    assert by_name["zerodte"].allowed_sides == (Side.PUT,)
    assert (by_name["default"].filters.min_dte, by_name["default"].filters.max_dte) == (7, 14)
    assert (by_name["zerodte"].filters.min_dte, by_name["zerodte"].filters.max_dte) == (0, 1)
    assert (by_name["strat"].filters.min_dte, by_name["strat"].filters.max_dte) == (1, 2)
    assert (by_name["swing"].filters.min_dte, by_name["swing"].filters.max_dte) == (7, 21)
    assert all(not strategy.exits.ratchet for strategy in by_name.values())

    import yaml

    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert raw["contract_filters"]["min_dte"] == 7
    assert raw["contract_filters"]["max_dte"] == 14
    assert raw["contract_filters"]["min_delta"] == 0.20
    assert raw["contract_filters"]["max_delta"] == 0.40


def test_disallowed_sides_are_validated():
    from killer_options_bot.config import _build_sides

    assert _build_sides(["put"]) == (Side.PUT,)
    assert _build_sides(None) == (Side.CALL, Side.PUT)
