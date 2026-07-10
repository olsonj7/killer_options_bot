"""Persistence for scanned candidates and (paper/live) trades.

Two backends implement the same interface:

- ``SQLiteStorage`` (default): a local file DB, great for dev and paper trading.
- ``PostgresStorage``: for hosted deployments (e.g. Supabase). Selected when a
  ``DATABASE_URL`` is configured. Requires the optional ``psycopg`` dependency.

All high-level query logic lives in ``BaseStorage`` and is written with ``?``
placeholders; each backend translates placeholders and dialect-specific SQL.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from killer_options_bot.models import (
    Candidate,
    PaperPosition,
    PositionStatus,
    Side,
)


@dataclass(frozen=True)
class Dialect:
    placeholder: str
    autoincrement: str
    real: str


SQLITE = Dialect(
    placeholder="?",
    autoincrement="INTEGER PRIMARY KEY AUTOINCREMENT",
    real="REAL",
)
POSTGRES = Dialect(
    placeholder="%s",
    autoincrement="BIGSERIAL PRIMARY KEY",
    real="DOUBLE PRECISION",
)


def _schema(d: Dialect) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS candidates (
    id {d.autoincrement},
    created_at TEXT NOT NULL,
    underlying TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strike {d.real} NOT NULL,
    expiration TEXT NOT NULL,
    dte INTEGER NOT NULL,
    mid {d.real} NOT NULL,
    spread_pct {d.real} NOT NULL,
    delta {d.real} NOT NULL,
    implied_volatility {d.real} NOT NULL,
    volume INTEGER NOT NULL,
    open_interest INTEGER NOT NULL,
    cost {d.real} NOT NULL,
    max_loss {d.real} NOT NULL,
    allowed INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    signal_note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id {d.autoincrement},
    option_symbol TEXT NOT NULL,
    underlying TEXT NOT NULL,
    side TEXT NOT NULL,
    strike {d.real} NOT NULL,
    expiration TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price {d.real} NOT NULL,
    entry_date TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    strategy TEXT NOT NULL DEFAULT 'default',
    original_quantity INTEGER,
    realized_pl_banked {d.real} NOT NULL DEFAULT 0,
    trims_done INTEGER NOT NULL DEFAULT 0,
    broker_order_id TEXT,
    exit_price {d.real},
    exit_date TEXT,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BaseStorage:
    """Backend-agnostic query logic. Subclasses implement connection I/O."""

    dialect: Dialect

    # --- Low-level ops -----------------------------------------------------

    @contextmanager
    def _connect(self):  # pragma: no cover - overridden
        raise NotImplementedError
        yield  # noqa

    def _translate(self, sql: str) -> str:
        if self.dialect.placeholder == "?":
            return sql
        return sql.replace("?", self.dialect.placeholder)

    def _insert(self, sql: str, params: tuple) -> int:  # pragma: no cover
        raise NotImplementedError

    def _query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(self._translate(sql), params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._query_all(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._connect() as conn:
            conn.execute(self._translate(sql), params)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            for statement in _schema(self.dialect).split(";"):
                stmt = statement.strip()
                if stmt:
                    conn.execute(stmt)
        self._migrate()

    def _existing_columns(self, table: str) -> set[str]:
        """Return the set of column names on ``table`` for the active backend."""
        if self.dialect.placeholder == "?":  # SQLite
            rows = self._query_all(f"PRAGMA table_info({table})")
            return {r["name"] for r in rows}
        rows = self._query_all(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name = ?",
            (table,),
        )
        return {r["name"] for r in rows}

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created.

        Uses plain ``ALTER TABLE ... ADD COLUMN`` guarded by an existence check
        so it is safe on both SQLite (no ADD COLUMN IF NOT EXISTS) and Postgres,
        and is a no-op on already-current databases.
        """
        cols = self._existing_columns("positions")
        if cols and "strategy" not in cols:
            with self._connect() as conn:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN strategy TEXT "
                    "NOT NULL DEFAULT 'default'"
                )
        # Scale-out (partial exit) columns, added after strategy support.
        if cols and "original_quantity" not in cols:
            with self._connect() as conn:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN original_quantity INTEGER"
                )
        if cols and "realized_pl_banked" not in cols:
            with self._connect() as conn:
                conn.execute(
                    f"ALTER TABLE positions ADD COLUMN realized_pl_banked "
                    f"{self.dialect.real} NOT NULL DEFAULT 0"
                )
        if cols and "trims_done" not in cols:
            with self._connect() as conn:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN trims_done INTEGER "
                    "NOT NULL DEFAULT 0"
                )

    # --- Runtime state (cross-process key/value) ---------------------------

    def set_state(self, key: str, value: str) -> None:
        """Upsert a runtime_state row, stamping updated_at with UTC now.

        Used for cross-process signals between the run loop and the dashboard
        (which are often separate processes sharing one DB): a scan heartbeat
        and per-strategy enable/disable toggles.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO runtime_state (key, value, updated_at) "
            "VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now),
        )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._query_one(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        )
        return row["value"] if row else default

    def get_state_row(self, key: str) -> tuple[str, str] | None:
        """Return (value, updated_at ISO) for ``key``, or None if unset."""
        row = self._query_one(
            "SELECT value, updated_at FROM runtime_state WHERE key = ?", (key,)
        )
        if not row:
            return None
        return row["value"], row["updated_at"]

    # --- Strategy enable/disable toggles -----------------------------------

    @staticmethod
    def _strategy_key(name: str) -> str:
        return f"strategy_enabled:{name}"

    def strategy_enabled(self, name: str) -> bool:
        """Whether ``name`` is enabled for new entries. Defaults to True when
        no toggle has been set, so existing behaviour is unchanged."""
        value = self.get_state(self._strategy_key(name))
        if value is None:
            return True
        return value == "1"

    def set_strategy_enabled(self, name: str, enabled: bool) -> None:
        self.set_state(self._strategy_key(name), "1" if enabled else "0")

    # --- Candidates --------------------------------------------------------

    def record_candidate(self, candidate: Candidate) -> int:
        c = candidate.contract
        return self._insert(
            """
            INSERT INTO candidates (
                created_at, underlying, option_symbol, side, strike,
                expiration, dte, mid, spread_pct, delta, implied_volatility,
                volume, open_interest, cost, max_loss, allowed, reasons,
                signal_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.created_at.isoformat(),
                c.underlying,
                c.symbol,
                candidate.side.value,
                c.strike,
                c.expiration.isoformat(),
                c.dte(),
                c.mid,
                c.spread_pct,
                c.delta,
                c.implied_volatility,
                c.volume,
                c.open_interest,
                c.cost,
                candidate.max_loss,
                1 if candidate.decision.allowed else 0,
                "; ".join(candidate.decision.reasons),
                candidate.signal_note,
            ),
        )

    def count_allowed_since(self, since: datetime) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM candidates "
            "WHERE allowed = 1 AND created_at >= ?",
            (since.isoformat(),),
        )
        return int(row["n"]) if row else 0

    def trades_this_week(self) -> int:
        since = datetime.utcnow() - timedelta(days=7)
        return self.count_allowed_since(since)

    def positions_opened_since(self, since: date) -> int:
        """Count positions whose entry_date is on/after ``since``.

        Basing the weekly-cadence guardrail on entry dates (not wall-clock
        candidate timestamps) keeps it correct during backtests too.
        """
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM positions WHERE entry_date >= ?",
            (since.isoformat(),),
        )
        return int(row["n"]) if row else 0

    def trades_in_trailing_week(self, as_of: date) -> int:
        return self.positions_opened_since(as_of - timedelta(days=7))

    def recent_candidates(self, limit: int = 20) -> list[dict]:
        return self._query_all(
            "SELECT * FROM candidates ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    # --- Positions ---------------------------------------------------------

    @staticmethod
    def _row_to_position(row: dict) -> PaperPosition:
        return PaperPosition(
            id=int(row["id"]),
            option_symbol=row["option_symbol"],
            underlying=row["underlying"],
            side=Side(row["side"]),
            strike=float(row["strike"]),
            expiration=date.fromisoformat(row["expiration"]),
            quantity=int(row["quantity"]),
            entry_price=float(row["entry_price"]),
            entry_date=date.fromisoformat(row["entry_date"]),
            status=PositionStatus(row["status"]),
            exit_price=(
                float(row["exit_price"])
                if row.get("exit_price") is not None
                else None
            ),
            exit_date=(
                date.fromisoformat(row["exit_date"])
                if row.get("exit_date")
                else None
            ),
            exit_reason=row.get("exit_reason"),
            strategy=row.get("strategy") or "default",
            original_quantity=(
                int(row["original_quantity"])
                if row.get("original_quantity") is not None
                else int(row["quantity"])
            ),
            realized_pl_banked=float(row.get("realized_pl_banked") or 0.0),
            trims_done=int(row.get("trims_done") or 0),
        )

    def open_position(
        self,
        position: PaperPosition,
        mode: str = "paper",
        broker_order_id: str | None = None,
    ) -> int:
        position.id = self._insert(
            """
            INSERT INTO positions (
                option_symbol, underlying, side, strike, expiration,
                quantity, entry_price, entry_date, status, mode,
                broker_order_id, exit_price, exit_date, exit_reason, strategy,
                original_quantity, realized_pl_banked, trims_done
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                position.option_symbol,
                position.underlying,
                position.side.value,
                position.strike,
                position.expiration.isoformat(),
                position.quantity,
                position.entry_price,
                position.entry_date.isoformat(),
                position.status.value,
                mode,
                broker_order_id,
                position.exit_price,
                position.exit_date.isoformat() if position.exit_date else None,
                position.exit_reason,
                position.strategy,
                position.original_quantity or position.quantity,
                position.realized_pl_banked,
                position.trims_done,
            ),
        )
        return position.id

    def reduce_position(
        self,
        position_id: int,
        new_quantity: int,
        realized_pl_banked: float,
        trims_done: int,
    ) -> None:
        """Apply a partial exit (trim): shrink the held quantity and record the
        cumulative banked P/L and the number of trim levels that have fired.
        The position stays open; the terminal exit closes the remainder."""
        self._execute(
            """
            UPDATE positions
            SET quantity = ?, realized_pl_banked = ?, trims_done = ?
            WHERE id = ?
            """,
            (new_quantity, realized_pl_banked, trims_done, position_id),
        )

    def close_position(
        self,
        position_id: int,
        exit_price: float,
        exit_date: date,
        exit_reason: str,
    ) -> None:
        self._execute(
            """
            UPDATE positions
            SET status = ?, exit_price = ?, exit_date = ?, exit_reason = ?
            WHERE id = ?
            """,
            (
                PositionStatus.CLOSED.value,
                exit_price,
                exit_date.isoformat(),
                exit_reason,
                position_id,
            ),
        )

    def open_positions(self) -> list[PaperPosition]:
        rows = self._query_all(
            "SELECT * FROM positions WHERE status = ? ORDER BY id",
            (PositionStatus.OPEN.value,),
        )
        return [self._row_to_position(r) for r in rows]

    def count_open_positions(self) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM positions WHERE status = ?",
            (PositionStatus.OPEN.value,),
        )
        return int(row["n"]) if row else 0

    def has_open_position(self, option_symbol: str) -> bool:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM positions "
            "WHERE status = ? AND option_symbol = ?",
            (PositionStatus.OPEN.value, option_symbol),
        )
        return bool(row and int(row["n"]) > 0)

    def all_positions(self, limit: int = 100) -> list[PaperPosition]:
        rows = self._query_all(
            "SELECT * FROM positions ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_position(r) for r in rows]

    def closed_positions(self) -> list[PaperPosition]:
        rows = self._query_all(
            "SELECT * FROM positions WHERE status = ? ORDER BY id",
            (PositionStatus.CLOSED.value,),
        )
        return [self._row_to_position(r) for r in rows]

    def realized_pl_since(self, since: date, mode: str = "live") -> float:
        """Sum realized P/L (dollars) for closed positions of ``mode`` whose
        exit_date is on/after ``since``. Used by live loss-lockout guards.
        Includes any P/L banked from partial exits (trims)."""
        rows = self._query_all(
            "SELECT entry_price, exit_price, quantity, realized_pl_banked "
            "FROM positions "
            "WHERE status = ? AND mode = ? AND exit_date >= ? "
            "AND exit_price IS NOT NULL",
            (PositionStatus.CLOSED.value, mode, since.isoformat()),
        )
        total = 0.0
        for r in rows:
            total += (
                (float(r["exit_price"]) - float(r["entry_price"]))
                * 100
                * int(r["quantity"])
            ) + float(r.get("realized_pl_banked") or 0.0)
        return round(total, 2)


class SQLiteStorage(BaseStorage):
    """Local file-based SQLite backend."""

    dialect = SQLITE

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        with self._connect() as conn:
            cur = conn.execute(self._translate(sql), params)
            return int(cur.lastrowid)


class PostgresStorage(BaseStorage):
    """Postgres backend (e.g. Supabase). Requires the ``psycopg`` package."""

    dialect = POSTGRES

    def __init__(self, dsn: str):
        try:
            import psycopg  # noqa: F401
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "Postgres backend requires 'psycopg'. Install with: "
                "pip install 'psycopg[binary]'"
            ) from exc
        self._dsn = dsn
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = self._psycopg.connect(self._dsn, row_factory=self._dict_row)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        sql = self._translate(sql) + " RETURNING id"
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return int(row["id"])


# Backwards-compatible default alias.
Storage = SQLiteStorage


def get_storage(config: Any) -> BaseStorage:
    """Pick a backend based on config.

    Uses Postgres when ``config.database_url`` is set (from the DATABASE_URL
    environment variable), otherwise the local SQLite file.
    """
    dsn = getattr(config, "database_url", None)
    if dsn:
        return PostgresStorage(dsn)
    return SQLiteStorage(config.db_path)
