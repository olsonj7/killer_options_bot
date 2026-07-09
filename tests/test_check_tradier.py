"""Tests for the `check-tradier` connectivity command (offline paths only).

The happy path hits the network, so it is not exercised here; we verify the
command is wired up and fails gracefully without a token.
"""

from __future__ import annotations

import yaml

from killer_options_bot.cli import main


def _write_config(tmp_path):
    cfg = {
        "account": {"value": 1000.0},
        "mode": {"trading_mode": "scan"},
        "watchlist": ["SPY"],
        "risk": {
            "max_trade_risk_pct": 0.05,
            "max_open_positions": 1,
            "max_trades_per_week": 2,
        },
        "contract_filters": {
            "min_dte": 2,
            "max_dte": 7,
            "min_delta": 0.20,
            "max_delta": 0.45,
            "max_spread_pct": 0.12,
            "min_volume": 1,
            "min_open_interest": 1,
        },
        "exits": {
            "profit_target_pct": 0.30,
            "stop_loss_pct": 0.25,
            "max_holding_days": 3,
            "min_dte_exit": 0,
        },
        "storage": {"db_path": str(tmp_path / "t.db")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_check_tradier_without_token(tmp_path, monkeypatch, capsys):
    # Empty token -> friendly error, no network call. Setting it (rather than
    # deleting) means load_dotenv(override=False) won't repopulate it from a
    # real local .env, so the test is hermetic.
    monkeypatch.setenv("TRADIER_API_TOKEN", "")
    config_path = _write_config(tmp_path)
    rc = main(["--config", str(config_path), "check-tradier"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no Tradier token" in err
