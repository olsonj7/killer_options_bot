"""A tiny local web dashboard for the Killer Options Bot.

Uses only the Python standard library (``http.server``) so it adds no
dependencies. It binds to 127.0.0.1 by default and is intended for local,
single-user use during paper trading. Optional HTTP Basic Auth can be enabled
so it is safe to reach over a trusted LAN (e.g. from your phone); without auth
it must not be exposed beyond localhost.

Routes:
  GET  /           Dashboard: P&L summary, equity curve, positions, candidates
  POST /scan       Run a scan (log candidates only)
  POST /scan-paper Run a scan and open paper positions for allowed candidates
  POST /manage     Re-price open positions and apply exit rules
  GET  /config     View/edit safe numeric config fields
  POST /config     Save edited config fields back to config.yaml
"""

from __future__ import annotations

import base64
import hmac
import html
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

import yaml

from killer_options_bot.brokers.base import MarketData
from killer_options_bot.brokers.mock import MockMarketData
from killer_options_bot.config import Config, load_config
from killer_options_bot.models import PaperPosition
from killer_options_bot.paper import PaperEngine
from killer_options_bot.scanner import Scanner
from killer_options_bot.storage import BaseStorage, get_storage
from killer_options_bot.withdraw import advise_from_storage


def _build_data_source(source: str, config: Config) -> MarketData:
    if source == "mock":
        return MockMarketData()
    if source == "tradier":
        from killer_options_bot.brokers.tradier import TradierMarketData

        return TradierMarketData(
            api_token=config.tradier.api_token or "",
            base_url=config.tradier.base_url,
        )
    raise ValueError(f"Unknown source: {source}")


# Editable config fields: (yaml section, key, label, type, min, max).
# Only these safe numeric fields are exposed in the UI. Everything else in
# config.yaml (watchlist, storage paths, etc.) is left untouched on save.
EDITABLE_FIELDS: list[tuple[str, str, str, str, float, float]] = [
    ("account", "value", "Account value ($)", "float", 1.0, 1_000_000.0),
    ("risk", "max_trade_risk_pct", "Max risk / trade (fraction)", "float", 0.001, 1.0),
    ("risk", "max_open_positions", "Max open positions", "int", 1, 50),
    ("risk", "max_trades_per_week", "Max trades / week", "int", 1, 100),
    ("contract_filters", "min_dte", "Min DTE", "int", 0, 365),
    ("contract_filters", "max_dte", "Max DTE", "int", 1, 730),
    ("contract_filters", "min_delta", "Min delta", "float", 0.0, 1.0),
    ("contract_filters", "max_delta", "Max delta", "float", 0.0, 1.0),
    ("contract_filters", "max_spread_pct", "Max spread (fraction)", "float", 0.0, 1.0),
    ("contract_filters", "min_volume", "Min volume", "int", 0, 1_000_000),
    ("contract_filters", "min_open_interest", "Min open interest", "int", 0, 10_000_000),
    ("exits", "profit_target_pct", "Profit target (fraction)", "float", 0.01, 10.0),
    ("exits", "stop_loss_pct", "Stop loss (fraction)", "float", 0.01, 1.0),
    ("exits", "max_holding_days", "Max holding days", "int", 1, 365),
    ("exits", "min_dte_exit", "Min DTE exit", "int", 0, 365),
    ("exits", "trim_0_at_pct", "Trim 1 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_0_fraction", "Trim 1 — sell this fraction", "float", 0.0, 0.99),
    ("exits", "trim_1_at_pct", "Trim 2 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_1_fraction", "Trim 2 — sell this fraction", "float", 0.0, 0.99),
    ("exits", "trim_2_at_pct", "Trim 3 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_2_fraction", "Trim 3 — sell this fraction", "float", 0.0, 0.99),
]

# Per-strategy editable fields (contract_filters + exits + limits).
# Form field names are prefixed: "strategy.<name>.<section>.<key>".
STRATEGY_EDITABLE_FIELDS: list[tuple[str, str, str, str, float, float]] = [
    ("limits", "max_trades_per_day", "Max trades / day (0 = unlimited)", "int", 0, 50),
    ("limits", "skip_midday", "Skip midday 12pm\u20132pm ET", "bool", 0, 1),
    ("contract_filters", "min_delta", "Min delta", "float", 0.0, 1.0),
    ("contract_filters", "max_delta", "Max delta", "float", 0.0, 1.0),
    ("contract_filters", "max_spread_pct", "Max spread (fraction)", "float", 0.0, 1.0),
    ("exits", "profit_target_pct", "Profit target (fraction)", "float", 0.01, 10.0),
    ("exits", "stop_loss_pct", "Stop loss (fraction)", "float", 0.01, 1.0),
    ("exits", "trim_0_at_pct", "Trim 1 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_0_fraction", "Trim 1 — sell this fraction", "float", 0.0, 0.99),
    ("exits", "trim_1_at_pct", "Trim 2 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_1_fraction", "Trim 2 — sell this fraction", "float", 0.0, 0.99),
    ("exits", "trim_2_at_pct", "Trim 3 — trigger (fraction, 0=off)", "float", 0.0, 10.0),
    ("exits", "trim_2_fraction", "Trim 3 — sell this fraction", "float", 0.0, 0.99),
]


class Dashboard:
    """Gathers data and runs actions for the web UI."""

    def __init__(self, config_path: str, source: str):
        self.config_path = config_path
        self.source = source

    def _context(self):
        base_config = load_config(self.config_path)
        storage = get_storage(base_config)
        overrides = storage.get_config_overrides()
        strat_overrides = storage.get_strategy_config_overrides()
        config = load_config(self.config_path, overrides or None, strat_overrides or None)
        data = _build_data_source(self.source, config)
        engine = PaperEngine(config, data, storage, cost_model=config.cost_model())
        return config, storage, data, engine

    # --- Actions -----------------------------------------------------------

    def run_scan(self, paper: bool) -> str:
        config, storage, data, engine = self._context()
        scanner = Scanner(config, data, storage)
        candidates = scanner.scan()
        allowed = [c for c in candidates if c.decision.allowed]
        opened = 0
        if paper:
            for c in allowed:
                if engine.open_from_candidate(c) is not None:
                    opened += 1
        if not candidates:
            return "Scan complete: no signals fired."
        if paper:
            return (
                f"Scan complete: {len(allowed)} allowed / {len(candidates)} "
                f"evaluated, opened {opened} paper position(s)."
            )
        return (
            f"Scan complete: {len(allowed)} allowed / {len(candidates)} "
            f"evaluated (scan only, nothing opened)."
        )

    def run_manage(self) -> str:
        _config, storage, _data, engine = self._context()
        if not storage.open_positions():
            return "Manage: no open positions."
        results = engine.manage_all()
        closed = sum(1 for r in results if r.closed)
        return f"Manage: processed {len(results)} position(s), closed {closed}."

    def export_csv(self) -> str:
        from killer_options_bot.export import positions_to_csv

        _config, storage, _data, _engine = self._context()
        return positions_to_csv(storage.all_positions(limit=100000))

    # --- Config editing ----------------------------------------------------

    def _load_raw_config(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def save_config(self, form: dict[str, list[str]]) -> str:
        """Validate and persist edited fields to the DB.

        Only whitelisted fields are written. Values are range-checked. Overrides
        are stored in Supabase (runtime_state) so they survive container redeploys
        and take effect on the next dashboard render without any code push.
        """
        base_config = load_config(self.config_path)
        storage = get_storage(base_config)
        existing = storage.get_config_overrides()

        # Build effective raw dict: YAML + existing DB overrides + new form values
        raw = self._load_raw_config()
        for (sec, k), v_str in existing.items():
            try:
                raw.setdefault(sec, {})[k] = float(v_str)
            except (ValueError, TypeError):
                pass

        to_save: list[tuple[str, str, int | float]] = []
        strat_to_save: list[tuple[str, str, str, int | float]] = []
        errors: list[str] = []

        for section, key, label, kind, lo, hi in EDITABLE_FIELDS:
            field_name = f"{section}.{key}"
            values = form.get(field_name)
            if not values:
                continue
            text = values[0].strip()
            if text == "":
                continue
            try:
                value: int | float = int(text) if kind == "int" else float(text)
            except ValueError:
                errors.append(f"{label}: not a number")
                continue
            if not (lo <= value <= hi):
                errors.append(f"{label}: must be between {lo:g} and {hi:g}")
                continue
            raw.setdefault(section, {})[key] = value
            to_save.append((section, key, value))

        # Per-strategy fields: form key = "strategy.<name>.<section>.<key>"
        existing_strat = storage.get_strategy_config_overrides()
        non_default = [s for s in base_config.active_strategies if s.name != "default"]
        for strat in non_default:
            for section, key, label, kind, lo, hi in STRATEGY_EDITABLE_FIELDS:
                field_name = f"strategy.{strat.name}.{section}.{key}"
                if kind == "bool":
                    # Checkboxes: present with value="true" when checked, absent when unchecked.
                    # We must always write a value so an uncheck is preserved.
                    sval_bool: bool = bool(form.get(field_name))
                    sval_str = "true" if sval_bool else "false"
                    raw.setdefault("strategies", {}).setdefault(strat.name, {}).setdefault(section, {})[key] = sval_bool
                    strat_to_save.append((strat.name, section, key, sval_str))
                    continue
                values = form.get(field_name)
                if not values:
                    continue
                text = values[0].strip()
                if text == "":
                    continue
                try:
                    sval: int | float = int(text) if kind == "int" else float(text)
                except ValueError:
                    errors.append(f"{strat.name} {label}: not a number")
                    continue
                if not (lo <= sval <= hi):
                    errors.append(f"{strat.name} {label}: must be between {lo:g} and {hi:g}")
                    continue
                raw.setdefault("strategies", {}).setdefault(strat.name, {}).setdefault(section, {})[key] = sval
                strat_to_save.append((strat.name, section, key, sval))

        # Cross-field sanity checks on base.
        cf = raw.get("contract_filters", {})
        if cf.get("min_dte", 0) > cf.get("max_dte", 0):
            errors.append("Min DTE must be <= Max DTE")
        if cf.get("min_delta", 0) > cf.get("max_delta", 0):
            errors.append("Min delta must be <= Max delta")
        # Trim ladder sanity: fractions must sum to < 1.
        for ctx, exits_dict in [("default", raw.get("exits", {}))] + [
            (n, (raw.get("strategies", {}) or {}).get(n, {}).get("exits", {}))
            for n in {s for s, _, _, _ in strat_to_save}
        ]:
            total_frac = sum(
                float(exits_dict.get(f"trim_{i}_fraction", 0) or 0)
                for i in range(3)
            )
            if total_frac >= 1.0:
                errors.append(
                    f"{ctx} trims: sell fractions sum to {total_frac:.2f} — "
                    "must be < 1 so a runner stays"
                )

        if errors:
            return "Config not saved. " + "; ".join(errors)

        # Validate the full merged config before committing any writes.
        new_overrides = dict(existing)
        for section, key, value in to_save:
            new_overrides[(section, key)] = str(value)
        new_strat_overrides = dict(existing_strat)
        for strat_name, section, key, value in strat_to_save:
            new_strat_overrides[(strat_name, section, key)] = str(value)
        try:
            load_config(self.config_path, new_overrides, new_strat_overrides)
        except Exception as exc:
            return f"Config rejected (validation failed): {exc}"

        for section, key, value in to_save:
            storage.set_config_override(section, key, value)
        for strat_name, section, key, value in strat_to_save:
            storage.set_strategy_config_override(strat_name, section, key, value)

        total = len(to_save) + len(strat_to_save)
        return f"Config saved: {total} field(s) updated."

    def set_strategies(self, form: dict[str, list[str]]) -> str:
        """Persist which strategies are enabled from the checkbox form.

        Checked boxes arrive as ``strategy=<name>`` entries; unchecked ones are
        simply absent. Every active strategy is set explicitly so toggles are
        deterministic regardless of prior state.
        """
        config, storage, _data, _engine = self._context()
        checked = set(form.get("strategy", []))
        on, off = [], []
        for s in config.active_strategies:
            enabled = s.name in checked
            storage.set_strategy_enabled(s.name, enabled)
            (on if enabled else off).append(s.name)
        if not on:
            return "All strategies disabled \u2014 no new entries will open."
        summary = f"Strategies enabled: {', '.join(on)}"
        if off:
            summary += f"; disabled: {', '.join(off)}"
        return summary + "."

    def render_config(self, flash: str = "") -> str:
        # _context() applies all DB overrides; use it as the single source of
        # truth for what the bot is currently running with.
        config, _storage, _data, _engine = self._context()
        raw = self._load_raw_config()
        # Overlay effective base values so the form shows what the bot uses.
        raw.setdefault("account", {})["value"] = config.account_value
        raw.setdefault("risk", {}).update({
            "max_trade_risk_pct": config.risk.max_trade_risk_pct,
            "max_open_positions": config.risk.max_open_positions,
            "max_trades_per_week": config.risk.max_trades_per_week,
        })
        raw.setdefault("contract_filters", {}).update({
            "min_dte": config.filters.min_dte,
            "max_dte": config.filters.max_dte,
            "min_delta": config.filters.min_delta,
            "max_delta": config.filters.max_delta,
            "max_spread_pct": config.filters.max_spread_pct,
            "min_volume": config.filters.min_volume,
            "min_open_interest": config.filters.min_open_interest,
        })
        exits_raw = raw.setdefault("exits", {})
        exits_raw.update({
            "profit_target_pct": config.exits.profit_target_pct,
            "stop_loss_pct": config.exits.stop_loss_pct,
            "max_holding_days": config.exits.max_holding_days,
            "min_dte_exit": config.exits.min_dte_exit,
        })
        # Expand the default strategy's trim ladder into flat keys.
        for i, trim in enumerate(config.exits.trims):
            exits_raw[f"trim_{i}_at_pct"] = trim.at_pct
            exits_raw[f"trim_{i}_fraction"] = trim.fraction
        return _render_config_page(raw, self.source, flash, config.active_strategies)

    # --- Rendering ---------------------------------------------------------

    def render(self, flash: str = "") -> str:
        config, storage, _data, engine = self._context()
        return _render_page(config, storage, engine, self.source, flash)


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_scan_time(raw: str | None) -> str:
    """Format a candidate's ``created_at`` (UTC ISO string) as market time.

    Candidates are stamped with ``datetime.utcnow()`` at scan time. Convert to
    US/Eastern (the market clock) and show a compact ``MM-DD HH:MM`` so it's
    obvious when a scan ran. Returns ``-`` if the value is missing/unparseable.
    """
    if not raw:
        return "-"
    from killer_options_bot.market import EASTERN

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).strftime("%m-%d %H:%M ET")

def _equity_curve_svg(
    closed: list[PaperPosition], unrealized: float | None = None
) -> str:
    """Render the cumulative realized-P/L curve as an inline SVG.

    Points are ordered by exit date. When ``unrealized`` is provided (the
    current mark-to-market P/L of open positions), a dashed projected segment
    is appended so the curve reflects live open exposure. Returns a small
    placeholder message when there is not yet enough data to draw a line.
    """
    trades = sorted(
        (p for p in closed if p.exit_date is not None),
        key=lambda p: p.exit_date,
    )

    equity = 0.0
    points = [0.0]
    for p in trades:
        equity += p.realized_pl() or 0.0
        points.append(round(equity, 2))

    has_projection = unrealized is not None and abs(unrealized) > 1e-9
    proj_index: int | None = None
    if has_projection:
        proj_index = len(points)
        points.append(round(equity + unrealized, 2))

    if len(points) < 2:
        return (
            "<div class='muted' style='padding:24px;'>"
            "Not enough closed trades yet to chart an equity curve.</div>"
        )

    w, h, pad = 1040, 220, 28
    lo = min(points)
    hi = max(points)
    span = (hi - lo) or 1.0
    n = len(points) - 1

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / n)

    def y(v: float) -> float:
        return pad + (h - 2 * pad) * (1 - (v - lo) / span)

    # Solid line covers realized points; a dashed segment shows the projection.
    solid_end = proj_index if proj_index is not None else len(points)
    solid_pts = points[:solid_end]
    solid_coords = " ".join(
        f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(solid_pts)
    )
    final = points[-1]
    line_color = "#3fb950" if final >= 0 else "#f85149"
    zero_y = y(0.0)
    zero_line = ""
    if lo <= 0 <= hi:
        zero_line = (
            f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{w - pad}' "
            f"y2='{zero_y:.1f}' stroke='#30363d' stroke-dasharray='4 4'/>"
        )
    # Area under the realized line for a subtle fill.
    area = f"{pad},{h - pad} {solid_coords} {x(solid_end - 1):.1f},{h - pad}"

    proj_svg = ""
    if proj_index is not None:
        px1, py1 = x(proj_index - 1), y(points[proj_index - 1])
        px2, py2 = x(proj_index), y(points[proj_index])
        proj_color = "#d29922"
        proj_svg = (
            f"<line x1='{px1:.1f}' y1='{py1:.1f}' x2='{px2:.1f}' "
            f"y2='{py2:.1f}' stroke='{proj_color}' stroke-width='2' "
            f"stroke-dasharray='5 4'/>"
            f"<circle cx='{px2:.1f}' cy='{py2:.1f}' r='3' fill='{proj_color}'/>"
        )

    label = "realized"
    if proj_index is not None:
        label = "realized + open marks (dashed)"

    return f"""<svg viewBox="0 0 {w} {h}" width="100%" height="{h}"
  preserveAspectRatio="none" role="img" aria-label="Equity curve">
  <polygon points="{area}" fill="{line_color}22"/>
  {zero_line}
  <polyline points="{solid_coords}" fill="none" stroke="{line_color}"
    stroke-width="2" stroke-linejoin="round"/>
  {proj_svg}
  <text x="{pad}" y="16" fill="#8b949e" font-size="11">
    {label}: {_fmt_money(hi)} peak / {_fmt_money(lo)} trough</text>
  <text x="{w - pad}" y="16" fill="{line_color}" font-size="11"
    text-anchor="end">ending {_fmt_money(final)}</text>
</svg>"""

def _render_withdraw_section(config: Config, storage: "BaseStorage") -> str:
    """Render the withdrawal advisor block, or nothing when disabled."""
    if not config.withdraw.enabled:
        return ""
    advice = advise_from_storage(config.withdraw, storage)

    cards = (
        f"<div class='card'><div class='label'>Equity (banked)</div>"
        f"<div class='value'>{_fmt_money(advice.equity)}</div></div>"
        f"<div class='card'><div class='label'>Peak</div>"
        f"<div class='value'>{_fmt_money(advice.peak_equity)}</div></div>"
        f"<div class='card'><div class='label'>Gain</div>"
        f"<div class='value'>{_fmt_money(advice.gain)}</div></div>"
        f"<div class='card'><div class='label'>Drawdown</div>"
        f"<div class='value'>{advice.drawdown_pct:.0%}</div></div>"
    )

    if advice.recommendations:
        items = "".join(
            f"<li><strong>{html.escape(_WD_LABELS.get(r.kind, r.kind))}: "
            f"{_fmt_money(r.amount)}</strong> &mdash; "
            f"{html.escape(r.reason)}</li>"
            for r in advice.recommendations
        )
        body = f"<ul class='wd-list'>{items}</ul>"
    else:
        body = (
            "<div class='muted' style='text-align:left;'>No action suggested "
            "right now &mdash; keep it all working.</div>"
        )

    return f"""
  <h2 style="font-size:15px;">Withdrawal advisor
    <span class="warn" style="font-weight:400;">(advisory only &mdash; the bot never moves money)</span>
  </h2>
  <div class="cards">{cards}</div>
  <div class="wd-box">{body}</div>
"""


_WD_LABELS = {
    "profit_skim": "Skim profits",
    "milestone": "Milestone reached",
    "tax_reserve": "Tax reserve",
    "drawdown_defense": "De-risk (drawdown)",
}


def _r_multiple(position: PaperPosition) -> float | None:
    """Result expressed in R (multiple of risk).

    Risk on a long option is the debit paid (the max loss), so R is the
    realized P/L divided by the *initial* cost (the original number of
    contracts). Using initial cost keeps R correct after partial exits, where
    the remaining ``entry_cost`` no longer reflects the size originally risked.
    Mirrors the "Percent Result" column of the trade-tracker template, where a
    full stop-out is -1R.
    """
    pl = position.realized_pl()
    if pl is None or position.initial_cost <= 0:
        return None
    return pl / position.initial_cost


def _trade_stats(closed: list[PaperPosition]) -> list[dict]:
    """Compute tracker-style stats grouped by strategy, plus a Total row.

    Each row carries: type, wins, losses, total, win_rate, avg_winner,
    avg_loser, risk_reward, total_r -- the same data points as the template's
    "Total Percentage" sheet.
    """

    def summarize(name: str, rows: list[PaperPosition]) -> dict:
        rs = [r for r in (_r_multiple(p) for p in rows) if r is not None]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0
        # Per-trade percentage return on the original debit risked (includes
        # any profit banked from trims), averaged across the group. Same basis
        # as R (risk = the debit paid), shown as a percent for readability.
        avg_pct = (sum(rs) / len(rs)) if rs else 0.0
        return {
            "type": name,
            "wins": len(wins),
            "losses": len(losses),
            "total": len(rs),
            "win_rate": (len(wins) / len(rs)) if rs else 0.0,
            "avg_winner": avg_win,
            "avg_loser": avg_loss,
            "risk_reward": rr,
            "total_r": sum(rs),
            "avg_pct": avg_pct,
        }

    by_strategy: dict[str, list[PaperPosition]] = {}
    for p in closed:
        by_strategy.setdefault(p.strategy or "default", []).append(p)

    rows = [summarize(name, by_strategy[name]) for name in sorted(by_strategy)]
    if len(rows) > 1:
        rows.append(summarize("Total", closed))
    return rows


def _render_stats_section(closed: list[PaperPosition]) -> str:
    """Render the R-based trade-stats table (tracker template layout)."""
    stats = _trade_stats(closed)
    if not any(row["total"] for row in stats):
        body = (
            "<tr><td colspan='10' class='muted'>No closed trades yet &mdash; "
            "stats appear once trades are managed to exit.</td></tr>"
        )
    else:
        cells = []
        for row in stats:
            is_total = row["type"] == "Total"
            name = html.escape(row["type"])
            if is_total:
                name = f"<strong>{name}</strong>"
            rcls = "pos" if row["total_r"] >= 0 else "neg"
            pcls = "pos" if row["avg_pct"] >= 0 else "neg"
            cells.append(
                f"<tr><td>{name}</td>"
                f"<td>{row['wins']}</td>"
                f"<td>{row['losses']}</td>"
                f"<td>{row['total']}</td>"
                f"<td>{row['win_rate']:.0%}</td>"
                f"<td class='{pcls}'>{row['avg_pct']:+.0%}</td>"
                f"<td class='pos'>{row['avg_winner']:+.2f}R</td>"
                f"<td class='neg'>{row['avg_loser']:+.2f}R</td>"
                f"<td>{row['risk_reward']:.2f}</td>"
                f"<td class='{rcls}'>{row['total_r']:+.2f}R</td></tr>"
            )
        body = "".join(cells)

    return f"""
  <h2 style="font-size:15px;">Trade stats
    <span class="warn" style="font-weight:400;">(R = multiple of risk; a full stop-out is -1R)</span>
  </h2>
  <table>
    <tr><th>Type</th><th>Wins</th><th>Losses</th><th>Total</th>
        <th>Win rate</th><th>Avg P/L %</th><th>Avg winner</th><th>Avg loser</th>
        <th>Risk:Reward</th><th>Total (R)</th></tr>
    {body}
  </table>
"""


def _render_strategy_pl_bars(closed: list[PaperPosition]) -> str:
    """Render a horizontal bar chart of realized P/L per strategy.

    One bar per strategy, drawn from a central zero axis: green to the right
    for net-positive strategies, red to the left for net-negative ones, so you
    can see at a glance which method is carrying (or dragging) the account.
    Uses inline SVG only, matching the equity-curve style (no dependencies).
    """
    by_strategy: dict[str, float] = {}
    for p in closed:
        pl = p.realized_pl()
        if pl is None:
            continue
        name = p.strategy or "default"
        by_strategy[name] = by_strategy.get(name, 0.0) + pl

    if not by_strategy:
        return ""

    # Largest strategies (by absolute P/L) first for a stable, readable order.
    items = sorted(by_strategy.items(), key=lambda kv: kv[1], reverse=True)
    magnitude = max((abs(v) for _, v in items), default=0.0) or 1.0

    w, row_h, pad_x, pad_top = 1040, 30, 120, 14
    label_w = 96  # left gutter for the strategy name
    axis_x = label_w + (w - label_w - pad_x) / 2  # centre (zero) line
    half = (w - label_w - pad_x) / 2  # pixels available each side of zero
    h = pad_top + row_h * len(items) + 10

    bars: list[str] = []
    for i, (name, pl) in enumerate(items):
        cy = pad_top + row_h * i + row_h / 2
        bar_len = (abs(pl) / magnitude) * half
        color = "#3fb950" if pl >= 0 else "#f85149"
        if pl >= 0:
            bx = axis_x
        else:
            bx = axis_x - bar_len
        bar_h = 14
        # Value label sits just past the outer end of the bar.
        if pl >= 0:
            tx, anchor = axis_x + bar_len + 6, "start"
        else:
            tx, anchor = axis_x - bar_len - 6, "end"
        bars.append(
            f"<text x='{label_w - 10}' y='{cy + 4:.1f}' fill='#c9d1d9' "
            f"font-size='12' text-anchor='end'>{html.escape(name)}</text>"
            f"<rect x='{bx:.1f}' y='{cy - bar_h / 2:.1f}' width='{bar_len:.1f}' "
            f"height='{bar_h}' rx='2' fill='{color}'/>"
            f"<text x='{tx:.1f}' y='{cy + 4:.1f}' fill='{color}' "
            f"font-size='11' text-anchor='{anchor}'>{_fmt_money(pl)}</text>"
        )

    svg = (
        f"<svg viewBox='0 0 {w} {h}' width='100%' height='{h}' "
        f"role='img' aria-label='Realized P/L by strategy'>"
        f"<line x1='{axis_x:.1f}' y1='{pad_top - 4}' x2='{axis_x:.1f}' "
        f"y2='{h - 6}' stroke='#30363d'/>"
        f"{''.join(bars)}"
        f"</svg>"
    )
    return f"""
  <h2 style="font-size:15px;">Realized P/L by strategy</h2>
  <div class="chart">{svg}</div>
"""


def _closed_qty_cell(p: PaperPosition) -> str:
    """Contracts for a closed trade, flagging scale-outs (trims).

    Shows the size the trade opened at. When the position was scaled out, adds
    a trim breakdown ("2 trimmed 1 + ran 1") so it's obvious the exit happened
    via the trim ladder rather than a single all-at-once close.
    """
    orig = p.original_quantity or p.quantity
    runner = p.quantity  # the final leg closed last
    trimmed = orig - runner
    if p.trims_done and trimmed > 0:
        return (
            f"{orig} "
            f"<span class='amber' style='font-size:11px;'>"
            f"&#9986; trimmed {trimmed} + ran {runner}</span>"
        )
    return str(orig)


def _render_closed_trades(closed: list[PaperPosition], limit: int = 25) -> str:
    """Table of individual past (closed) trades, most recent first."""
    if not closed:
        body = (
            "<tr><td colspan='10' class='muted'>No closed trades yet.</td></tr>"
        )
    else:
        # closed_positions() returns oldest-first by id; show newest first.
        rows = []
        for p in reversed(closed[-limit:]):
            pl = p.realized_pl() or 0.0
            cls = "pos" if pl >= 0 else "neg"
            pct = 0.0
            if p.exit_price is not None and p.entry_price > 0:
                pct = (p.exit_price - p.entry_price) / p.entry_price
            exit_d = p.exit_date.isoformat() if p.exit_date else "-"
            reason = html.escape(p.exit_reason or "")
            rows.append(
                f"<tr><td>{html.escape(p.underlying)}</td>"
                f"<td>{html.escape(p.side.value.upper())} {p.strike:g}</td>"
                f"<td>{p.expiration.isoformat()}</td>"
                f"<td>{_closed_qty_cell(p)}</td>"
                f"<td>{html.escape(p.strategy or 'default')}</td>"
                f"<td>{p.entry_date.isoformat()}</td>"
                f"<td>{exit_d}</td>"
                f"<td>{_fmt_money(p.entry_price)} &rarr; "
                f"{_fmt_money(p.exit_price or 0.0)}</td>"
                f"<td class='{cls}'>{_fmt_money(pl)} ({pct:+.0%})</td>"
                f"<td class='reasons'>{reason}</td></tr>"
            )
        body = "".join(rows)
    return f"""
  <h2 style="font-size:15px;">Closed trades</h2>
  <table>
    <tr><th>Underlying</th><th>Contract</th><th>Expires</th><th>Qty</th><th>Strategy</th><th>Opened</th>
        <th>Closed</th><th>Entry &rarr; Exit</th><th>Realized P/L</th>
        <th>Reason</th></tr>
    {body}
  </table>
"""


def _fmt_age(seconds: float) -> str:
    """Human-friendly age like '8s', '3m', '2h 5m'."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _render_status_banner(storage: "BaseStorage") -> str:
    """A live 'scans are running' indicator driven by the loop heartbeat.

    The run loop upserts ``loop_heartbeat`` (value = "scanning" while the market
    is open, "market_closed" when idle) every tick. If that timestamp goes stale
    the loop isn't running at all, so we show a red 'not running' state.
    """
    row = storage.get_state_row("loop_heartbeat")
    if row is None:
        return (
            "<div class='status status-off'>"
            "\u25cf Scans not running \u2014 the trading loop has never "
            "reported in. Start it with <code>run --source tradier</code> "
            "or enable it on the host (KOB_RUN=1)."
            "</div>"
        )
    state, updated_at = row
    try:
        beat = datetime.fromisoformat(updated_at)
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - beat).total_seconds()
    except ValueError:
        age = 1e9
    age_txt = _fmt_age(age)

    # Stale heartbeat => the loop process is gone even if it once ran. Use a
    # generous window so a slow tick or long 0DTE chain fetch doesn't flap.
    if age > 300:
        return (
            "<div class='status status-off'>"
            f"\u25cf Scans not running \u2014 last heartbeat {age_txt} ago. "
            "The loop appears stopped."
            "</div>"
        )
    if state == "scanning":
        return (
            "<div class='status status-on'>"
            f"\u25cf Scanning \u2014 live (last scan {age_txt} ago)."
            "</div>"
        )
    return (
        "<div class='status status-idle'>"
        f"\u25cf Loop alive, idle \u2014 market closed (heartbeat {age_txt} "
        "ago). Scans resume at the next open."
        "</div>"
    )


def _render_strategy_toggles(config: Config, storage: "BaseStorage") -> str:
    """Checkboxes to enable/disable each active strategy for new entries.

    Lets you match strategies to the market regime (flat / bullish / bearish)
    without a redeploy. Disabling a strategy stops NEW entries only; exit
    management of its existing positions always continues.
    """
    strategies = config.active_strategies
    if not strategies:
        return ""
    boxes = []
    for s in strategies:
        enabled = storage.strategy_enabled(s.name)
        checked = "checked" if enabled else ""
        state_txt = "on" if enabled else "off"
        state_cls = "pos" if enabled else "neg"
        boxes.append(
            f"<label class='toggle'>"
            f"<input type='checkbox' name='strategy' "
            f"value='{html.escape(s.name)}' {checked}>"
            f"{html.escape(s.name)} "
            f"<span class='{state_cls}'>({state_txt})</span>"
            f"<span class='muted'> &middot; {html.escape(s.signal)}</span>"
            f"</label>"
        )
    return (
        "<h2 style='font-size:15px;'>Strategies to scan</h2>"
        "<form method='post' action='/strategies' class='toggles'>"
        + "".join(boxes)
        + "<button type='submit'>Save strategy selection</button>"
        "<div class='warn'>Unchecked strategies stop opening NEW trades; "
        "open positions are still managed to their exits.</div>"
        "</form>"
    )


def _render_page(
    config: Config,
    storage: "BaseStorage",
    engine: PaperEngine,
    source: str,
    flash: str,
) -> str:
    open_positions = storage.open_positions()
    closed = storage.closed_positions()

    # P&L summary.
    realized = engine.realized_pl_total()
    wins = [p for p in closed if (p.realized_pl() or 0) > 0]
    losses = [p for p in closed if (p.realized_pl() or 0) <= 0]
    unrealized = 0.0
    pos_rows = []
    for p in open_positions:
        price = engine.mark_to_market(p)
        orig = p.original_quantity or p.quantity
        qty_txt = f"{p.quantity}/{orig}" if orig != p.quantity else str(p.quantity)
        if price is None:
            pos_rows.append(
                f"<tr><td>{html.escape(p.option_symbol)}</td>"
                f"<td>{html.escape(p.underlying)}</td>"
                f"<td>{html.escape(p.side.value.upper())}</td>"
                f"<td>{p.strike:g}</td>"
                f"<td>{qty_txt}</td>"
                f"<td>{_fmt_money(p.entry_price)}</td>"
                f"<td>-</td><td>-</td>"
                f"<td>{p.holding_days(engine.as_of)}</td>"
                f"<td>{p.dte(engine.as_of)}</td>"
                f"<td>{html.escape(p.strategy or 'default')}</td></tr>"
            )
            continue
        upl = p.unrealized_pl(price)
        unrealized += upl
        cls = "pos" if upl >= 0 else "neg"
        pl_txt = f"{_fmt_money(upl)} ({p.pl_pct(price):+.0%})"
        if p.realized_pl_banked:
            pl_txt += f" +${p.realized_pl_banked:,.0f} banked"
        if p.mode == "live":
            oid = html.escape(p.broker_order_id or "—")
            mode_cell = (
                f"<span class='badge badge-live'>LIVE</span> "
                f"<span class='oid'>#{oid}</span>"
            )
        else:
            mode_cell = "<span class='badge badge-paper'>paper</span>"
        pos_rows.append(
            f"<tr><td>{html.escape(p.option_symbol)}</td>"
            f"<td>{html.escape(p.underlying)}</td>"
            f"<td>{html.escape(p.side.value.upper())}</td>"
            f"<td>{p.strike:g}</td>"
            f"<td>{qty_txt}</td>"
            f"<td>{_fmt_money(p.entry_price)}</td>"
            f"<td>{_fmt_money(price)}</td>"
            f"<td class='{cls}'>{pl_txt}</td>"
            f"<td>{p.holding_days(engine.as_of)}</td>"
            f"<td>{p.expiration.isoformat()}</td>"
            f"<td>{p.dte(engine.as_of)}</td>"
            f"<td>{html.escape(p.strategy or 'default')}</td>"
            f"<td>{mode_cell}</td></tr>"
        )

    total = realized + unrealized
    win_rate = (len(wins) / len(closed)) if closed else 0.0

    def money_cell(v: float) -> str:
        cls = "pos" if v >= 0 else "neg"
        return f"<span class='{cls}'>{_fmt_money(v)}</span>"

    # Recent candidates.
    cand_rows = []
    for r in storage.recent_candidates(limit=15):
        reasons = html.escape(r["reasons"] or "")
        # Three-state verdict: REJECT (failed risk), BLOCKED (passed risk but a
        # guardrail stopped the open, e.g. already holding the underlying), or
        # ALLOW (passed and opened). A risk-allowed row with a reason recorded
        # means it was blocked downstream, so it is not a real ALLOW.
        if not r["allowed"]:
            verdict, vcls = "REJECT", "neg"
        elif r["reasons"]:
            verdict, vcls = "BLOCKED", "amber"
        else:
            verdict, vcls = "ALLOW", "pos"
        cand_rows.append(
            f"<tr><td class='reasons'>{_fmt_scan_time(r.get('created_at'))}</td>"
            f"<td class='{vcls}'>{verdict}</td>"
            f"<td>{html.escape(r['underlying'])}</td>"
            f"<td>{html.escape(r['side'].upper())}</td>"
            f"<td>{r['strike']:g}</td>"
            f"<td>{r['dte']}</td>"
            f"<td>{_fmt_money(r['mid'])}</td>"
            f"<td>{_fmt_money(r['cost'])}</td>"
            f"<td class='reasons'>{reasons}</td></tr>"
        )

    flash_html = (
        f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""
    )
    pos_body = (
        "".join(pos_rows)
        or "<tr><td colspan='13' class='muted'>No open positions.</td></tr>"
    )
    cand_body = (
        "".join(cand_rows)
        or "<tr><td colspan='9' class='muted'>No candidates logged yet.</td></tr>"
    )
    max_risk = config.account_value * config.risk.max_trade_risk_pct
    equity_svg = _equity_curve_svg(closed, unrealized=unrealized)

    withdraw_html = _render_withdraw_section(config, storage)
    stats_html = _render_stats_section(closed)
    strategy_pl_html = _render_strategy_pl_bars(closed)
    closed_html = _render_closed_trades(closed)
    status_html = _render_status_banner(storage)
    toggles_html = _render_strategy_toggles(config, storage)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Killer Options Bot</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
          background: #0e1116; color: #e6edf3; }}
  header {{ padding: 16px 24px; background: #161b22;
            border-bottom: 1px solid #30363d; }}
  h1 {{ font-size: 18px; margin: 0; }}
  .sub {{ color: #8b949e; font-size: 13px; margin-top: 4px; }}
  main {{ padding: 24px; max-width: 1100px; margin: 0 auto; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 14px 18px; min-width: 130px; }}
  .card .label {{ color: #8b949e; font-size: 12px; }}
  .card .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 28px;
           background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           overflow: hidden; }}
  th, td {{ text-align: left; padding: 8px 12px; font-size: 13px;
            border-bottom: 1px solid #21262d; }}
  th {{ color: #8b949e; font-weight: 600; background: #12161c; }}
  .pos {{ color: #3fb950; }}
  .neg {{ color: #f85149; }}
  .amber {{ color: #d29922; }}
  .muted {{ color: #8b949e; text-align: center; }}
  .reasons {{ color: #8b949e; font-size: 12px; }}
  .actions {{ display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }}
  button {{ background: #238636; color: #fff; border: 0; border-radius: 6px;
            padding: 9px 16px; font-size: 14px; cursor: pointer; }}
  button.secondary {{ background: #30363d; }}
  button:hover {{ filter: brightness(1.1); }}
  .flash {{ background: #1f6feb22; border: 1px solid #1f6feb;
            color: #cae1ff; padding: 10px 14px; border-radius: 6px;
            margin-bottom: 18px; font-size: 14px; }}
  .status {{ padding: 10px 14px; border-radius: 6px; margin-bottom: 18px;
             font-size: 14px; font-weight: 600; border: 1px solid; }}
  .status-on {{ background: #23863622; border-color: #238636;
                color: #3fb950; }}
  .status-idle {{ background: #d2992222; border-color: #d29922;
                  color: #e3b341; }}
  .status-off {{ background: #f8514922; border-color: #f85149;
                 color: #f85149; }}
  .status code {{ background: #0e1116; padding: 1px 5px; border-radius: 4px;
                  font-size: 12px; color: #e6edf3; }}
  .toggles {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
              background: #161b22; border: 1px solid #30363d;
              border-radius: 8px; padding: 14px 18px; margin-bottom: 28px; }}
  .toggle {{ font-size: 14px; display: flex; align-items: center; gap: 6px;
             cursor: pointer; }}
  .toggles .warn {{ flex-basis: 100%; margin: 0; }}
  .warn {{ color: #d29922; font-size: 12px; margin-top: 8px; }}
  .chart {{ background: #161b22; border: 1px solid #30363d;
            border-radius: 8px; margin-bottom: 28px; }}
  .wd-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 12px 18px; margin-bottom: 28px; }}
  .wd-list {{ margin: 0; padding-left: 18px; }}
  .wd-list li {{ font-size: 13px; color: #c9d1d9; margin: 6px 0; }}
  .wd-list strong {{ color: #d29922; }}
  a.nav {{ color: #58a6ff; text-decoration: none; font-size: 13px; }}
  a.nav:hover {{ text-decoration: underline; }}
  .badge {{ font-size: 11px; font-weight: 700; padding: 1px 6px;
            border-radius: 10px; letter-spacing: 0.02em; }}
  .badge-live {{ background: #f8514922; border: 1px solid #f85149;
                 color: #f85149; }}
  .badge-paper {{ background: #30363d; border: 1px solid #484f58;
                  color: #8b949e; }}
  .oid {{ font-size: 11px; color: #8b949e; }}</style>
<script>
  // Auto-refresh the read-only dashboard so heartbeat/positions/P&L stay
  // current without a manual reload. Skips reloading while a form control is
  // focused (editing strategy toggles) or the tab is hidden, so it never
  // interrupts an interaction or hammers the server in the background.
  //
  // Action feedback (the flash banner) is server-side and consumed on first
  // render, so a blind auto-refresh would wipe it before it could be read.
  // When a flash is present we hold off reloading, keep it visible for a few
  // seconds, then dismiss it and let live refresh resume.
  (function () {{
    var INTERVAL_MS = 30000;
    var flash = document.querySelector('.flash');
    if (flash) {{
      setTimeout(function () {{ flash.remove(); }}, 12000);
    }}
    setInterval(function () {{
      if (document.hidden) return;
      if (document.querySelector('.flash')) return;
      var el = document.activeElement;
      if (el && /^(INPUT|BUTTON|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      window.location.reload();
    }}, INTERVAL_MS);
  }})();
</script>
</head>
<body>
<header>
  <h1>Killer Options Bot &mdash; paper dashboard</h1>
  <div class="sub">Account {_fmt_money(config.account_value)} &middot;
    max risk/trade {_fmt_money(max_risk)} &middot;
    source: {html.escape(source)} &middot; local only, no live orders &middot;
    <a class="nav" href="/config">edit config</a></div>
</header>
<main>
  {flash_html}
  {status_html}
  <div class="actions">
    <form method="post" action="/manage">
      <button type="submit">Manage exits</button>
    </form>
    <a class="nav" href="/export.csv" style="align-self:center;">Export CSV</a>
  </div>

  {toggles_html}

  <div class="cards">
    <div class="card"><div class="label">Total P/L</div>
      <div class="value">{money_cell(total)}</div></div>
    <div class="card"><div class="label">Realized</div>
      <div class="value">{money_cell(realized)}</div></div>
    <div class="card"><div class="label">Unrealized</div>
      <div class="value">{money_cell(unrealized)}</div></div>
    <div class="card"><div class="label">Open</div>
      <div class="value">{len(open_positions)}</div></div>
    <div class="card"><div class="label">Closed</div>
      <div class="value">{len(closed)}</div></div>
    <div class="card"><div class="label">Win rate</div>
      <div class="value">{win_rate:.0%}</div></div>
  </div>

  <h2 style="font-size:15px;">Equity curve (realized solid, open marks dashed)</h2>
  <div class="chart">{equity_svg}</div>

  {stats_html}

  {strategy_pl_html}

  {withdraw_html}

  <h2 style="font-size:15px;">Open positions</h2>
  <table>
    <tr><th>Contract</th><th>Underlying</th><th>Side</th><th>Strike</th><th>Qty</th><th>Entry</th>
        <th>Mark</th><th>Unreal P/L</th><th>Held</th><th>Expires</th><th>DTE</th><th>Strategy</th><th>Mode</th></tr>
    {pos_body}
  </table>

  {closed_html}

  <h2 style="font-size:15px;">Recent candidates</h2>
  <table>
    <tr><th>Scanned</th><th>Verdict</th><th>Underlying</th><th>Side</th><th>Strike</th>
        <th>DTE</th><th>Mid</th><th>Cost</th><th>Reasons</th></tr>
    {cand_body}
  </table>
  <div class="warn">This is paper/scan only. No real orders are ever placed.</div>
</main>
</body>
</html>"""


def _shared_css() -> str:
    return """
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #0e1116; color: #e6edf3; }
  header { padding: 16px 24px; background: #161b22;
           border-bottom: 1px solid #30363d; }
  h1 { font-size: 18px; margin: 0; }
  .sub { color: #8b949e; font-size: 13px; margin-top: 4px; }
  main { padding: 24px; max-width: 760px; margin: 0 auto; }
  a.nav { color: #58a6ff; text-decoration: none; font-size: 13px; }
  a.nav:hover { text-decoration: underline; }
  .flash { background: #1f6feb22; border: 1px solid #1f6feb;
           color: #cae1ff; padding: 10px 14px; border-radius: 6px;
           margin-bottom: 18px; font-size: 14px; }
  .field { display: flex; justify-content: space-between; align-items: center;
           padding: 9px 0; border-bottom: 1px solid #21262d; }
  .field label { color: #c9d1d9; font-size: 14px; }
  .field input { background: #0d1117; border: 1px solid #30363d;
                 color: #e6edf3; border-radius: 6px; padding: 6px 10px;
                 width: 140px; font-size: 14px; text-align: right; }
  .section { color: #8b949e; font-size: 12px; text-transform: uppercase;
             letter-spacing: 0.05em; margin: 20px 0 4px; }
  button { background: #238636; color: #fff; border: 0; border-radius: 6px;
           padding: 9px 16px; font-size: 14px; cursor: pointer; margin-top: 16px; }
  button:hover { filter: brightness(1.1); }
  .warn { color: #d29922; font-size: 12px; margin-top: 12px; }
"""


def _config_fields_html(
    fields: list,
    raw_section_getter,
    prefix: str = "",
) -> str:
    """Render a list of field rows for the config form.

    ``fields`` is a list of (section, key, label, kind, lo, hi) tuples.
    ``raw_section_getter(section, key)`` returns the current value string.
    ``prefix`` is prepended to the field name (e.g. "strategy.zerodte.").
    """
    rows = []
    last_section = None
    section_labels = {
        "contract_filters": "Contract filters",
        "exits": "Exits",
        "account": "Account",
        "risk": "Risk",
        "limits": "Limits",
    }
    for section, key, label, kind, lo, hi in fields:
        if section != last_section:
            rows.append(
                f"<div class='cfg-section'>"
                f"{html.escape(section_labels.get(section, section))}"
                f"</div>"
            )
            last_section = section
        current = raw_section_getter(section, key)
        field_id = f"{prefix}{section}.{key}"
        if kind == "bool":
            checked = "checked" if str(current).lower() in ("true", "1", "yes") else ""
            rows.append(
                f"<div class='field'>"
                f"<label for='{field_id}'>{html.escape(label)}</label>"
                f"<input type='checkbox' id='{field_id}' "
                f"name='{field_id}' value='true' {checked}>"
                f"</div>"
            )
        else:
            step = "1" if kind == "int" else "any"
            rows.append(
                f"<div class='field'>"
                f"<label for='{field_id}'>{html.escape(label)}</label>"
                f"<input type='number' step='{step}' id='{field_id}' "
                f"name='{field_id}' value='{html.escape(str(current))}' "
                f"min='{lo:g}' max='{hi:g}'>"
                f"</div>"
            )
    return "".join(rows)


def _render_config_page(
    raw: dict,
    source: str,
    flash: str,
    active_strategies: tuple | None = None,
) -> str:
    flash_html = (
        f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""
    )

    # --- Tab: Global ---------------------------------------------------------
    def _global_val(section: str, key: str) -> str:
        return str((raw.get(section, {}) or {}).get(key, ""))

    global_fields = [(s, k, lbl, knd, lo, hi)
                     for s, k, lbl, knd, lo, hi in EDITABLE_FIELDS
                     if s in ("account", "risk")]
    global_html = _config_fields_html(global_fields, _global_val)

    # --- Tab: default (weekly) -----------------------------------------------
    default_strat = next(
        (s for s in (active_strategies or ()) if s.name == "default"), None
    )

    def _default_val(section: str, key: str) -> str:
        if section == "contract_filters":
            return str(getattr(default_strat.filters, key, "")) if default_strat else str((raw.get(section, {}) or {}).get(key, ""))
        return str(getattr(default_strat.exits, key, "")) if default_strat else str((raw.get(section, {}) or {}).get(key, ""))

    default_fields = [(s, k, lbl, knd, lo, hi)
                      for s, k, lbl, knd, lo, hi in EDITABLE_FIELDS
                      if s in ("contract_filters", "exits")]
    default_html = _config_fields_html(default_fields, _default_val)

    # --- Tabs: per non-default strategy --------------------------------------
    strat_tabs_nav = ""
    strat_tabs_content = ""
    for strat in (active_strategies or ()):
        if strat.name == "default":
            continue
        tab_id = html.escape(strat.name)
        prefix = f"strategy.{strat.name}."

        def _strat_val(section: str, key: str, _s=strat) -> str:
            if section == "limits":
                return str(getattr(_s, key, 0))
            if section == "exits":
                if key.startswith("trim_") and key.endswith("_at_pct"):
                    try:
                        idx = int(key[5:-7])
                        return str(_s.exits.trims[idx].at_pct) if idx < len(_s.exits.trims) else "0"
                    except (ValueError, IndexError):
                        return "0"
                if key.startswith("trim_") and key.endswith("_fraction"):
                    try:
                        idx = int(key[5:-9]); return str(_s.exits.trims[idx].fraction) if idx < len(_s.exits.trims) else "0"
                    except (ValueError, IndexError):
                        return "0"
                return str(getattr(_s.exits, key, ""))
            return str(getattr(_s.filters, key, ""))

        content = _config_fields_html(STRATEGY_EDITABLE_FIELDS, _strat_val, prefix)
        display_name = strat.name.replace("_", " ").title().replace("Zerodte", "0DTE").replace("0Dte", "0DTE")
        strat_tabs_nav += (
            f"<button type='button' class='tab-btn' "
            f"data-tab='tab-{tab_id}' onclick='switchTab(this)'>"
            f"{html.escape(display_name)}</button>"
        )
        strat_tabs_content += (
            f"<div id='tab-{tab_id}' class='tab-panel' style='display:none'>"
            f"{content}</div>"
        )

    has_strat_tabs = bool(strat_tabs_nav)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Config &mdash; Killer Options Bot</title>
<style>
{_shared_css()}
  .tabs {{ display: flex; gap: 2px; border-bottom: 1px solid #30363d;
           margin-bottom: 20px; }}
  .tab-btn {{ background: transparent; border: none; border-bottom: 2px solid transparent;
              color: #8b949e; padding: 8px 16px; font-size: 14px; cursor: pointer;
              margin: 0; margin-bottom: -1px; border-radius: 0; }}
  .tab-btn:hover {{ color: #e6edf3; filter: none; }}
  .tab-btn.active {{ color: #e6edf3; border-bottom-color: #2f81f7; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .cfg-section {{ color: #8b949e; font-size: 11px; text-transform: uppercase;
                  letter-spacing: 0.07em; margin: 20px 0 4px; }}
  .save-bar {{ margin-top: 20px; padding-top: 16px;
               border-top: 1px solid #30363d; }}
</style>
</head>
<body>
<header>
  <h1>Killer Options Bot &mdash; config</h1>
  <div class="sub">source: {html.escape(source)} &middot;
    <a class="nav" href="/">&larr; back to dashboard</a></div>
</header>
<main>
  {flash_html}
  <form method="post" action="/config">
    <div class="tabs">
      <button type="button" class="tab-btn active" data-tab="tab-global" onclick="switchTab(this)">Global</button>
      <button type="button" class="tab-btn" data-tab="tab-default" onclick="switchTab(this)">Weekly (default)</button>
      {strat_tabs_nav}
    </div>
    <div id="tab-global" class="tab-panel active">{global_html}</div>
    <div id="tab-default" class="tab-panel">{default_html}</div>
    {strat_tabs_content}
    <div class="save-bar">
      <button type="submit">Save config</button>
      <span class="warn" style="margin-left:14px;">Watchlist, data source,
        and storage paths require editing config.yaml directly.</span>
    </div>
  </form>
</main>
<script>
  function switchTab(btn) {{
    document.querySelectorAll('.tab-btn').forEach(function(b) {{
      b.classList.remove('active');
    }});
    document.querySelectorAll('.tab-panel').forEach(function(p) {{
      p.style.display = 'none';
      p.classList.remove('active');
    }});
    btn.classList.add('active');
    var panel = document.getElementById(btn.dataset.tab);
    if (panel) {{ panel.style.display = 'block'; panel.classList.add('active'); }}
  }}
  // Restore the active tab from the URL hash so Save config keeps you on the
  // same tab after the redirect (browser re-sends the hash on reload).
  (function () {{
    var hash = window.location.hash.replace('#', '');
    if (hash) {{
      var btn = document.querySelector('.tab-btn[data-tab="' + hash + '"]');
      if (btn) switchTab(btn);
    }}
    document.querySelectorAll('.tab-btn').forEach(function(b) {{
      b.addEventListener('click', function() {{
        history.replaceState(null, '', '#' + b.dataset.tab);
      }});
    }});
  }})();
</script>
</body>
</html>"""


def _make_handler(dashboard: Dashboard, auth: tuple[str, str] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def _send_html(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_csv(self, body: str, filename: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _authorized(self) -> bool:
            if auth is None:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            # Constant-time comparison to avoid timing leaks.
            ok_user = hmac.compare_digest(user, auth[0])
            ok_pwd = hmac.compare_digest(pwd, auth[1])
            return ok_user and ok_pwd

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Killer Options Bot"'
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if not self._require_auth():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send_html(dashboard.render(_pop_flash()))
                return
            if path == "/config":
                self._send_html(dashboard.render_config(_pop_flash()))
                return
            if path == "/export.csv":
                self._send_csv(dashboard.export_csv(), "positions.csv")
                return
            self._send_html("<h1>404</h1>", status=404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]

            if path == "/config":
                form = parse_qs(body.decode("utf-8"))
                try:
                    _set_flash(dashboard.save_config(form))
                except Exception as exc:
                    _set_flash(f"Error: {exc}")
                self._redirect("/config")
                return

            if path == "/strategies":
                form = parse_qs(body.decode("utf-8"))
                try:
                    _set_flash(dashboard.set_strategies(form))
                except Exception as exc:
                    _set_flash(f"Error: {exc}")
                self._redirect("/")
                return

            action: Callable[[], str] | None = {
                "/scan": lambda: dashboard.run_scan(paper=False),
                "/scan-paper": lambda: dashboard.run_scan(paper=True),
                "/manage": dashboard.run_manage,
            }.get(path)

            if action is None:
                self._send_html("<h1>404</h1>", status=404)
                return
            try:
                _set_flash(action())
            except Exception as exc:  # surface errors in the UI
                _set_flash(f"Error: {exc}")
            self._redirect("/")

        def log_message(self, *_args) -> None:  # keep the console quiet
            pass

    return Handler


# Simple process-local flash message (single-user local dashboard).
_FLASH: list[str] = []


def _set_flash(message: str) -> None:
    _FLASH.clear()
    _FLASH.append(message)


def _pop_flash() -> str:
    return _FLASH.pop() if _FLASH else ""


def serve(
    config_path: str,
    source: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    auth: tuple[str, str] | None = None,
    run_loop: bool = False,
    run_tick: int = 60,
    run_paper: bool = True,
    run_ignore_market_hours: bool = False,
) -> None:
    dashboard = Dashboard(config_path, source)
    handler = _make_handler(dashboard, auth=auth)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Killer Options Bot dashboard: http://{host}:{port}  (Ctrl+C to stop)")
    auth_txt = f"basic auth as '{auth[0]}'" if auth else "no auth"
    print(f"  config={config_path} source={source} ({auth_txt})")
    if auth is None and host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "  WARNING: bound to a non-local host without auth. "
            "Anyone on the network can control this dashboard."
        )

    # Optionally run the automated scan/manage loop in a background thread so a
    # single process (e.g. one Railway service) both trades and serves the UI.
    stop_event = None
    loop_thread = None
    if run_loop:
        import threading

        from killer_options_bot.cli import run_loop as _run_loop

        stop_event = threading.Event()

        def _loop() -> None:
            try:
                _run_loop(
                    config_path=config_path,
                    source=source,
                    tick=run_tick,
                    paper=run_paper,
                    ignore_market_hours=run_ignore_market_hours,
                    stop_event=stop_event,
                    log=lambda m: print(f"[run] {m}"),
                )
            except Exception as exc:  # keep the web server alive on loop error
                print(f"[run] loop crashed: {exc}")

        loop_thread = threading.Thread(target=_loop, name="run-loop", daemon=True)
        loop_thread.start()
        print(f"  automated run loop: ON (tick={run_tick}s)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        if stop_event is not None:
            stop_event.set()
        if loop_thread is not None:
            loop_thread.join(timeout=5)
        server.server_close()

