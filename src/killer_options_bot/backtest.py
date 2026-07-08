"""Backtest loop: step the clock, scan, open, and manage paper positions.

This drives the existing Scanner and PaperEngine across a date range so you can
generate paper-trade statistics over many trades in a single run. It uses an
isolated in-memory database by default so it never touches your real trade log.

Backtests are only meaningful with a data source that supports historical dates
(the mock source does). It intentionally reuses the exact same risk engine,
signal, and exit rules as live scanning so results reflect the real rules.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import Config
from killer_options_bot.models import PaperPosition
from killer_options_bot.paper import PaperEngine
from killer_options_bot.scanner import Scanner
from killer_options_bot.storage import Storage


@dataclass
class TradeRecord:
    """A completed round-trip trade in the backtest."""

    option_symbol: str
    underlying: str
    side: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    pl: float
    pl_pct: float
    reason: str
    holding_days: int


@dataclass
class BacktestStats:
    start: date
    end: date
    trades: list[TradeRecord] = field(default_factory=list)
    ending_open: int = 0

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.pl > 0]

    @property
    def losses(self) -> list[TradeRecord]:
        return [t for t in self.trades if t.pl <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / self.num_trades if self.num_trades else 0.0

    @property
    def total_pl(self) -> float:
        return round(sum(t.pl for t in self.trades), 2)

    @property
    def avg_win(self) -> float:
        return (
            round(sum(t.pl for t in self.wins) / len(self.wins), 2)
            if self.wins
            else 0.0
        )

    @property
    def avg_loss(self) -> float:
        return (
            round(sum(t.pl for t in self.losses) / len(self.losses), 2)
            if self.losses
            else 0.0
        )

    @property
    def expectancy(self) -> float:
        """Average P/L per trade in dollars."""
        return (
            round(self.total_pl / self.num_trades, 2)
            if self.num_trades
            else 0.0
        )

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pl for t in self.wins)
        gross_loss = -sum(t.pl for t in self.losses)
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return round(gross_win / gross_loss, 2)

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough drop of the cumulative realized-P/L curve (dollars)."""
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted(self.trades, key=lambda r: r.exit_date):
            equity += t.pl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return round(max_dd, 2)


class Backtester:
    def __init__(
        self,
        config: Config,
        start: date,
        end: date,
        step_days: int = 1,
        db_path: str | None = None,
    ):
        self.config = config
        self.start = start
        self.end = end
        self.step_days = max(1, step_days)
        # Isolated store so the real trade log is never touched. The storage
        # layer opens a fresh connection per call, so an in-memory DB would not
        # persist across calls; use a throwaway temp file instead.
        if db_path is None:
            self._tmp = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False
            )
            self._tmp.close()
            db_path = self._tmp.name
        else:
            self._tmp = None
        self._db_path = Path(db_path)
        self.storage = Storage(db_path)

    def run(self) -> BacktestStats:
        current = self.start
        while current <= self.end:
            data = MockMarketData(as_of=current)

            # 1) Manage existing positions first (exits before new entries).
            paper = PaperEngine(self.config, data, self.storage, as_of=current)
            paper.manage_all()

            # 2) Scan and open new positions for allowed candidates.
            scanner = Scanner(self.config, data, self.storage, as_of=current)
            for candidate in scanner.scan():
                if candidate.decision.allowed:
                    paper.open_from_candidate(candidate)

            current += timedelta(days=self.step_days)

        # Force-close anything still open at the final date for clean stats.
        final = self.end
        paper = PaperEngine(
            self.config, MockMarketData(as_of=final), self.storage, as_of=final
        )
        for position in self.storage.open_positions():
            price = paper.mark_to_market(position)
            if price is not None:
                self.storage.close_position(
                    position.id, price, final, "backtest end (forced close)"
                )

        return self._collect_stats()

    def _collect_stats(self) -> BacktestStats:
        stats = BacktestStats(start=self.start, end=self.end)
        for p in self.storage.closed_positions():
            record = self._to_record(p)
            if record is not None:
                stats.trades.append(record)
        self._cleanup()
        return stats

    def _cleanup(self) -> None:
        if self._tmp is not None:
            try:
                self._db_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _to_record(p: PaperPosition) -> TradeRecord | None:
        if p.exit_price is None or p.exit_date is None:
            return None
        pl = p.realized_pl() or 0.0
        pl_pct = (
            (p.exit_price - p.entry_price) / p.entry_price
            if p.entry_price
            else 0.0
        )
        return TradeRecord(
            option_symbol=p.option_symbol,
            underlying=p.underlying,
            side=p.side.value,
            entry_date=p.entry_date,
            exit_date=p.exit_date,
            entry_price=p.entry_price,
            exit_price=p.exit_price,
            pl=pl,
            pl_pct=round(pl_pct, 4),
            reason=p.exit_reason or "",
            holding_days=(p.exit_date - p.entry_date).days,
        )
