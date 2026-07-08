"""CSV export helpers for paper/live positions."""

from __future__ import annotations

import csv
import io

from killer_options_bot.models import PaperPosition, PositionStatus

CSV_COLUMNS = [
    "id",
    "status",
    "underlying",
    "side",
    "strike",
    "expiration",
    "quantity",
    "entry_date",
    "entry_price",
    "entry_cost",
    "exit_date",
    "exit_price",
    "exit_reason",
    "realized_pl",
]


def _row(position: PaperPosition) -> dict:
    realized = position.realized_pl()
    return {
        "id": position.id,
        "status": position.status.value,
        "underlying": position.underlying,
        "side": position.side.value,
        "strike": f"{position.strike:g}",
        "expiration": position.expiration.isoformat(),
        "quantity": position.quantity,
        "entry_date": position.entry_date.isoformat(),
        "entry_price": f"{position.entry_price:.2f}",
        "entry_cost": f"{position.entry_cost:.2f}",
        "exit_date": position.exit_date.isoformat() if position.exit_date else "",
        "exit_price": (
            f"{position.exit_price:.2f}"
            if position.exit_price is not None
            else ""
        ),
        "exit_reason": position.exit_reason or "",
        "realized_pl": f"{realized:.2f}" if realized is not None else "",
    }


def positions_to_csv(positions: list[PaperPosition]) -> str:
    """Render positions as a CSV string (closed first, then open)."""
    ordered = sorted(
        positions,
        key=lambda p: (p.status is PositionStatus.OPEN, p.id or 0),
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for position in ordered:
        writer.writerow(_row(position))
    return buf.getvalue()
