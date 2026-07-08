"""Tests for CSV export of positions."""

from __future__ import annotations

from datetime import date, timedelta

from killer_options_bot.export import CSV_COLUMNS, positions_to_csv
from killer_options_bot.models import PaperPosition, PositionStatus, Side


def _open(as_of: date) -> PaperPosition:
    return PaperPosition(
        id=1,
        option_symbol="AAPL260315C00150000",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=1,
        entry_price=1.00,
        entry_date=as_of,
        status=PositionStatus.OPEN,
    )


def _closed(as_of: date) -> PaperPosition:
    return PaperPosition(
        id=2,
        option_symbol="MSFT260315C00300000",
        underlying="MSFT",
        side=Side.CALL,
        strike=300.0,
        expiration=as_of + timedelta(days=45),
        quantity=2,
        entry_price=2.00,
        entry_date=as_of,
        status=PositionStatus.CLOSED,
        exit_price=3.00,
        exit_date=as_of + timedelta(days=10),
        exit_reason="profit target",
    )


def test_csv_has_header_and_rows():
    as_of = date(2026, 1, 1)
    csv_text = positions_to_csv([_open(as_of), _closed(as_of)])
    lines = csv_text.strip().splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS)
    # header + 2 rows
    assert len(lines) == 3


def test_csv_closed_before_open_and_pl():
    as_of = date(2026, 1, 1)
    csv_text = positions_to_csv([_open(as_of), _closed(as_of)])
    rows = csv_text.strip().splitlines()[1:]
    # Closed position (id=2) sorts before the open one.
    assert rows[0].startswith("2,closed")
    assert rows[1].startswith("1,open")
    # realized P/L for closed = (3-2)*100*2 = 200.00
    assert "200.00" in rows[0]


def test_csv_empty():
    csv_text = positions_to_csv([])
    assert csv_text.strip() == ",".join(CSV_COLUMNS)
