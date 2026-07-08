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
from datetime import date
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
]


class Dashboard:
    """Gathers data and runs actions for the web UI."""

    def __init__(self, config_path: str, source: str):
        self.config_path = config_path
        self.source = source

    def _context(self):
        config = load_config(self.config_path)
        storage = get_storage(config)
        data = _build_data_source(self.source, config)
        engine = PaperEngine(config, data, storage)
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
        """Validate and persist edited fields back to config.yaml.

        Only whitelisted fields are written. Values are range-checked. If the
        resulting config fails to load/validate, the file is not changed.
        """
        raw = self._load_raw_config()
        updated = 0
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
                value = int(text) if kind == "int" else float(text)
            except ValueError:
                errors.append(f"{label}: not a number")
                continue
            if not (lo <= value <= hi):
                errors.append(f"{label}: must be between {lo:g} and {hi:g}")
                continue
            raw.setdefault(section, {})[key] = value
            updated += 1

        # Cross-field sanity checks.
        cf = raw.get("contract_filters", {})
        if cf.get("min_dte", 0) > cf.get("max_dte", 0):
            errors.append("Min DTE must be <= Max DTE")
        if cf.get("min_delta", 0) > cf.get("max_delta", 0):
            errors.append("Min delta must be <= Max delta")

        if errors:
            return "Config not saved. " + "; ".join(errors)

        # Write to a temp file first, then validate by loading it.
        tmp_path = Path(self.config_path).with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
        try:
            load_config(str(tmp_path))
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            return f"Config rejected (validation failed): {exc}"

        tmp_path.replace(self.config_path)
        return f"Config saved: {updated} field(s) updated."

    def render_config(self, flash: str = "") -> str:
        raw = self._load_raw_config()
        return _render_config_page(raw, self.source, flash)

    # --- Rendering ---------------------------------------------------------

    def render(self, flash: str = "") -> str:
        config, storage, _data, engine = self._context()
        return _render_page(config, storage, engine, self.source, flash)


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


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
        if price is None:
            pos_rows.append(
                f"<tr><td>{html.escape(p.option_symbol)}</td>"
                f"<td>{html.escape(p.underlying)}</td>"
                f"<td>{html.escape(p.side.value.upper())}</td>"
                f"<td>{_fmt_money(p.entry_price)}</td>"
                f"<td>-</td><td>-</td>"
                f"<td>{p.holding_days(engine.as_of)}</td>"
                f"<td>{p.dte(engine.as_of)}</td></tr>"
            )
            continue
        upl = p.unrealized_pl(price)
        unrealized += upl
        cls = "pos" if upl >= 0 else "neg"
        pos_rows.append(
            f"<tr><td>{html.escape(p.option_symbol)}</td>"
            f"<td>{html.escape(p.underlying)}</td>"
            f"<td>{html.escape(p.side.value.upper())}</td>"
            f"<td>{_fmt_money(p.entry_price)}</td>"
            f"<td>{_fmt_money(price)}</td>"
            f"<td class='{cls}'>{_fmt_money(upl)} ({p.pl_pct(price):+.0%})</td>"
            f"<td>{p.holding_days(engine.as_of)}</td>"
            f"<td>{p.dte(engine.as_of)}</td></tr>"
        )

    total = realized + unrealized
    win_rate = (len(wins) / len(closed)) if closed else 0.0

    def money_cell(v: float) -> str:
        cls = "pos" if v >= 0 else "neg"
        return f"<span class='{cls}'>{_fmt_money(v)}</span>"

    # Recent candidates.
    cand_rows = []
    for r in storage.recent_candidates(limit=15):
        verdict = "ALLOW" if r["allowed"] else "REJECT"
        vcls = "pos" if r["allowed"] else "neg"
        reasons = html.escape(r["reasons"] or "")
        cand_rows.append(
            f"<tr><td class='{vcls}'>{verdict}</td>"
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
        or "<tr><td colspan='8' class='muted'>No open positions.</td></tr>"
    )
    cand_body = (
        "".join(cand_rows)
        or "<tr><td colspan='8' class='muted'>No candidates logged yet.</td></tr>"
    )
    max_risk = config.account_value * config.risk.max_trade_risk_pct
    equity_svg = _equity_curve_svg(closed, unrealized=unrealized)

    withdraw_html = _render_withdraw_section(config, storage)

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
  .warn {{ color: #d29922; font-size: 12px; margin-top: 8px; }}
  .chart {{ background: #161b22; border: 1px solid #30363d;
            border-radius: 8px; margin-bottom: 28px; }}
  .wd-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 12px 18px; margin-bottom: 28px; }}
  .wd-list {{ margin: 0; padding-left: 18px; }}
  .wd-list li {{ font-size: 13px; color: #c9d1d9; margin: 6px 0; }}
  .wd-list strong {{ color: #d29922; }}
  a.nav {{ color: #58a6ff; text-decoration: none; font-size: 13px; }}
  a.nav:hover {{ text-decoration: underline; }}</style>
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
  <div class="actions">
    <form method="post" action="/scan-paper">
      <button type="submit">Scan &amp; open paper trades</button>
    </form>
    <form method="post" action="/scan">
      <button class="secondary" type="submit">Scan only</button>
    </form>
    <form method="post" action="/manage">
      <button class="secondary" type="submit">Manage exits</button>
    </form>
    <a class="nav" href="/export.csv" style="align-self:center;">Export CSV</a>
  </div>

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

  {withdraw_html}

  <h2 style="font-size:15px;">Open positions</h2>
  <table>
    <tr><th>Contract</th><th>Underlying</th><th>Side</th><th>Entry</th>
        <th>Mark</th><th>Unreal P/L</th><th>Held</th><th>DTE</th></tr>
    {pos_body}
  </table>

  <h2 style="font-size:15px;">Recent candidates</h2>
  <table>
    <tr><th>Verdict</th><th>Underlying</th><th>Side</th><th>Strike</th>
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


def _render_config_page(raw: dict, source: str, flash: str) -> str:
    flash_html = (
        f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""
    )
    rows = []
    last_section = None
    for section, key, label, kind, lo, hi in EDITABLE_FIELDS:
        if section != last_section:
            rows.append(f"<div class='section'>{html.escape(section)}</div>")
            last_section = section
        current = (raw.get(section, {}) or {}).get(key, "")
        step = "1" if kind == "int" else "any"
        rows.append(
            f"<div class='field'>"
            f"<label for='{section}.{key}'>{html.escape(label)}</label>"
            f"<input type='number' step='{step}' "
            f"id='{section}.{key}' name='{section}.{key}' "
            f"value='{html.escape(str(current))}' "
            f"min='{lo:g}' max='{hi:g}'></div>"
        )
    fields_html = "".join(rows)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Config &mdash; Killer Options Bot</title>
<style>{_shared_css()}</style>
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
    {fields_html}
    <button type="submit">Save config</button>
  </form>
  <div class="warn">Only these numeric risk/filter/exit fields are editable
    here. Watchlist, data source, and storage paths are edited in
    config.yaml directly. Changes are validated before saving.</div>
</main>
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
