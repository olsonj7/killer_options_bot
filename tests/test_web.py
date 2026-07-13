"""Tests for the web dashboard rendering and actions."""

from __future__ import annotations

import yaml

from killer_options_bot.web import Dashboard, _equity_curve_svg, _render_strategy_pl_bars
from killer_options_bot.storage import Storage
from killer_options_bot.models import PaperPosition, PositionStatus, Side
from datetime import date


def _write_config(tmp_path, account_value=25000.0):
    db_path = tmp_path / "web.db"
    cfg = {
        "account": {"value": account_value},
        "mode": {"trading_mode": "scan"},
        "watchlist": ["SPY", "NVDA", "AAPL"],
        "risk": {
            "max_trade_risk_pct": 0.05,
            "max_open_positions": 1,
            "max_trades_per_week": 2,
        },
        "contract_filters": {
            "min_dte": 30,
            "max_dte": 60,
            "min_delta": 0.30,
            "max_delta": 0.45,
            "max_spread_pct": 0.12,
            "min_volume": 100,
            "min_open_interest": 500,
        },
        "signal": {
            "sma_period": 20,
            "rsi_period": 14,
            "rsi_min": 45,
            "rsi_max": 70,
        },
        "exits": {
            "profit_target_pct": 0.35,
            "stop_loss_pct": 0.45,
            "max_holding_days": 21,
            "min_dte_exit": 21,
        },
        "storage": {"db_path": str(db_path)},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def test_dashboard_renders_html(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")
    html = dash.render()
    assert "<title>Killer Options Bot</title>" in html
    assert "Total P/L" in html
    assert "Open positions" in html
    assert "no live orders" in html


def test_dashboard_scan_action(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")
    message = dash.run_scan(paper=False)
    assert "Scan complete" in message
    # Rendering after a scan still works.
    assert "<html" in dash.render(flash=message)


def test_dashboard_manage_action_no_positions(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")
    message = dash.run_manage()
    assert "no open positions" in message.lower()


def test_equity_curve_needs_two_points():
    assert "Not enough closed trades" in _equity_curve_svg([])


def _closed(symbol, entry, exit_, epx, xpx, strategy="default"):
    return PaperPosition(
        option_symbol=symbol,
        underlying="AAPL",
        side=Side.CALL,
        strike=150.0,
        expiration=exit_,
        quantity=1,
        entry_price=epx,
        entry_date=entry,
        status=PositionStatus.CLOSED,
        exit_price=xpx,
        exit_date=exit_,
        exit_reason="test",
        strategy=strategy,
    )


def test_equity_curve_renders_svg():
    trades = [
        _closed("A", date(2026, 1, 1), date(2026, 1, 5), 1.0, 1.5),
        _closed("B", date(2026, 1, 2), date(2026, 1, 8), 2.0, 1.0),
        _closed("C", date(2026, 1, 3), date(2026, 1, 12), 1.0, 1.4),
    ]
    svg = _equity_curve_svg(trades)
    assert "<svg" in svg
    assert "polyline" in svg
    assert "ending" in svg


def test_equity_curve_projects_unrealized():
    trades = [
        _closed("A", date(2026, 1, 1), date(2026, 1, 5), 1.0, 1.5),
        _closed("B", date(2026, 1, 2), date(2026, 1, 8), 2.0, 1.0),
    ]
    svg = _equity_curve_svg(trades, unrealized=25.0)
    # Dashed projection segment + label appear when unrealized is supplied.
    assert "stroke-dasharray='5 4'" in svg
    assert "open marks" in svg


def test_equity_curve_single_closed_plus_unrealized():
    # One closed trade alone is not enough, but adding an open mark makes two
    # points so a curve can render.
    trades = [_closed("A", date(2026, 1, 1), date(2026, 1, 5), 1.0, 1.5)]
    svg = _equity_curve_svg(trades, unrealized=-10.0)
    assert "<svg" in svg


def test_strategy_pl_bars_render_per_strategy():
    trades = [
        _closed("A", date(2026, 1, 1), date(2026, 1, 5), 1.0, 1.5, "default"),
        _closed("B", date(2026, 1, 2), date(2026, 1, 8), 2.0, 1.0, "zerodte"),
        _closed("C", date(2026, 1, 3), date(2026, 1, 9), 1.0, 1.4, "zerodte"),
    ]
    svg = _render_strategy_pl_bars(trades)
    assert "<svg" in svg
    assert "Realized P/L by strategy" in svg
    # One labelled bar per distinct strategy.
    assert "default" in svg
    assert "zerodte" in svg
    assert svg.count("<rect") == 2


def test_strategy_pl_bars_empty_when_no_trades():
    assert _render_strategy_pl_bars([]) == ""


def _candidate(symbol, strike, decision):
    from killer_options_bot.models import Candidate, OptionContract, RiskDecision, Side

    return Candidate(
        contract=OptionContract(
            symbol=symbol,
            underlying="SPY",
            side=Side.CALL,
            strike=strike,
            expiration=date(2026, 1, 16),
            bid=1.50,
            ask=1.54,
            last=1.52,
            delta=0.35,
            implied_volatility=0.30,
            volume=1000,
            open_interest=2000,
        ),
        side=Side.CALL,
        signal_note="test",
        decision=decision,
        max_loss=154.0,
    )


def test_dashboard_candidate_three_state_verdict(tmp_path):
    from killer_options_bot.models import RiskDecision

    config_path = _write_config(tmp_path)
    storage = Storage(str(tmp_path / "web.db"))

    # REJECT: failed risk.
    storage.record_candidate(
        _candidate("SPY260116C00760000", 760.0, RiskDecision.reject("bad spread"))
    )
    # ALLOW: passed risk, no downstream block recorded.
    storage.record_candidate(
        _candidate("SPY260116C00755000", 755.0, RiskDecision.accept())
    )
    # BLOCKED: passed risk but blocked at open (annotated afterwards).
    cid = storage.record_candidate(
        _candidate("SPY260116C00759000", 759.0, RiskDecision.accept())
    )
    storage.mark_candidate_blocked(cid, "blocked: already holding SPY")

    dash = Dashboard(config_path, source="mock")
    html = dash.render()
    assert ">REJECT<" in html
    assert ">ALLOW<" in html
    assert ">BLOCKED<" in html
    assert "already holding SPY" in html
    assert "Scanned" in html  # scan-time column header


def test_config_page_renders_current_values(tmp_path):
    config_path = _write_config(tmp_path, account_value=12345.0)
    dash = Dashboard(config_path, source="mock")
    page = dash.render_config()
    assert "config" in page.lower()
    assert "12345" in page
    assert "Max risk / trade" in page


def test_config_save_updates_and_validates(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")

    msg = dash.save_config({"account.value": ["50000"],
                            "risk.max_trades_per_week": ["3"]})
    assert "saved" in msg.lower()

    saved = yaml.safe_load(open(config_path, encoding="utf-8"))
    assert saved["account"]["value"] == 50000.0
    assert saved["risk"]["max_trades_per_week"] == 3


def test_config_save_rejects_out_of_range(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")
    # max_trade_risk_pct must be <= 1.0.
    msg = dash.save_config({"risk.max_trade_risk_pct": ["5"]})
    assert "not saved" in msg.lower()
    saved = yaml.safe_load(open(config_path, encoding="utf-8"))
    assert saved["risk"]["max_trade_risk_pct"] == 0.05  # unchanged


def test_config_save_rejects_bad_cross_field(tmp_path):
    config_path = _write_config(tmp_path)
    dash = Dashboard(config_path, source="mock")
    # min_dte > max_dte should be rejected.
    msg = dash.save_config({"contract_filters.min_dte": ["90"],
                            "contract_filters.max_dte": ["30"]})
    assert "not saved" in msg.lower()
    saved = yaml.safe_load(open(config_path, encoding="utf-8"))
    assert saved["contract_filters"]["min_dte"] == 30  # unchanged
