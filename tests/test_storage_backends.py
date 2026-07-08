"""Tests for storage backend abstraction (placeholder translation, schema,
mode-aware queries, and the get_storage factory)."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import make_config

from killer_options_bot.models import PaperPosition, PositionStatus, Side
from killer_options_bot.storage import (
    POSTGRES,
    SQLITE,
    PostgresStorage,
    SQLiteStorage,
    Storage,
    _schema,
    get_storage,
)


def test_storage_alias_is_sqlite():
    assert Storage is SQLiteStorage


def test_translate_sqlite_is_noop():
    s = SQLiteStorage(":memory:")
    assert s._translate("SELECT ? , ?") == "SELECT ? , ?"


def test_translate_postgres_uses_percent_s():
    # Build without connecting (bypass __init__ which needs psycopg + a DB).
    inst = PostgresStorage.__new__(PostgresStorage)
    inst.dialect = POSTGRES
    assert inst._translate("SELECT ? , ?") == "SELECT %s , %s"


def test_schema_dialect_specifics():
    sqlite_sql = _schema(SQLITE)
    pg_sql = _schema(POSTGRES)
    assert "AUTOINCREMENT" in sqlite_sql
    assert "BIGSERIAL" in pg_sql
    assert "DOUBLE PRECISION" in pg_sql
    # Both must include the new live-tracking columns.
    for sql in (sqlite_sql, pg_sql):
        assert "mode" in sql
        assert "broker_order_id" in sql


def test_get_storage_defaults_to_sqlite(tmp_path):
    config = make_config(tmp_path, database_url=None)
    assert isinstance(get_storage(config), SQLiteStorage)


def _position(as_of: date, entry: float, exit_: float) -> PaperPosition:
    return PaperPosition(
        option_symbol="AAPL260315C00150000",
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=as_of + timedelta(days=45),
        quantity=1,
        entry_price=entry,
        entry_date=as_of,
        status=PositionStatus.OPEN,
    )


def test_open_position_records_mode(tmp_path):
    config = make_config(tmp_path)
    storage = SQLiteStorage(config.db_path)
    as_of = date(2026, 1, 1)
    pos = _position(as_of, 1.00, 0.0)
    storage.open_position(pos, mode="live", broker_order_id="ABC123")
    # realized_pl_since only counts live-mode closed positions.
    storage.close_position(pos.id, 0.50, as_of, "stop loss")
    loss = storage.realized_pl_since(as_of, mode="live")
    # (0.50 - 1.00) * 100 * 1 = -50.00
    assert loss == -50.00


def test_realized_pl_since_ignores_paper(tmp_path):
    config = make_config(tmp_path)
    storage = SQLiteStorage(config.db_path)
    as_of = date(2026, 1, 1)
    pos = _position(as_of, 1.00, 0.0)
    storage.open_position(pos, mode="paper")
    storage.close_position(pos.id, 0.50, as_of, "stop loss")
    assert storage.realized_pl_since(as_of, mode="live") == 0.0
