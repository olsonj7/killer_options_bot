"""Tests for the `run` loop command (single-cycle, market hours bypassed)."""

from __future__ import annotations

import yaml

from killer_options_bot.cli import main


def _write_run_config(tmp_path):
    """Minimal config with two strategies at different scan cadences."""
    cfg = {
        "account": {"value": 50000.0},
        "mode": {"trading_mode": "paper"},
        "watchlist": ["SPY", "QQQ"],
        "risk": {
            "max_trade_risk_pct": 0.30,
            "max_open_positions": 10,
            "max_trades_per_week": 100,
        },
        "contract_filters": {
            "min_dte": 2,
            "max_dte": 7,
            "min_delta": 0.20,
            "max_delta": 0.60,
            "max_spread_pct": 0.20,
            "min_volume": 1,
            "min_open_interest": 1,
        },
        "exits": {
            "profit_target_pct": 0.40,
            "stop_loss_pct": 0.40,
            "max_holding_days": 5,
            "min_dte_exit": 0,
        },
        "storage": {"db_path": str(tmp_path / "run.db")},
        "account_tiers": [
            {"min_value": 0, "strategies": ["default", "swing"]},
        ],
        "strategies": {
            "default": {"scan_interval_minutes": 15},
            "swing": {
                "scan_interval_minutes": 30,
                "contract_filters": {"min_dte": 5, "max_dte": 21},
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_run_once_ignoring_market_hours(tmp_path, capsys):
    config_path = _write_run_config(tmp_path)
    rc = main(
        [
            "--config",
            str(config_path),
            "run",
            "--source",
            "mock",
            "--ignore-market-hours",
            "--once",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Run loop starting" in out
    assert "finished after 1 active cycle" in out
    # Both strategies' cadences should be reported.
    assert "default=15m" in out
    assert "swing=30m" in out


def test_run_once_no_paper_opens_nothing(tmp_path, capsys):
    config_path = _write_run_config(tmp_path)
    rc = main(
        [
            "--config",
            str(config_path),
            "run",
            "--source",
            "mock",
            "--ignore-market-hours",
            "--once",
            "--no-paper",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "paper=off" in out
    # In no-paper mode no positions are opened.
    assert "opened #" not in out


def test_run_rejects_tradier_without_token(tmp_path, capsys, monkeypatch):
    # Force an empty token so load_dotenv(override=False) won't repopulate it
    # from a real local .env (hermetic regardless of the developer's machine).
    monkeypatch.setenv("TRADIER_API_TOKEN", "")
    config_path = _write_run_config(tmp_path)
    rc = main(
        [
            "--config",
            str(config_path),
            "run",
            "--source",
            "tradier",
            "--once",
            "--ignore-market-hours",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires a token" in err


def test_run_loop_stops_on_event(tmp_path):
    """The extracted run_loop honors a stop_event (used by serve --run)."""
    import threading

    from killer_options_bot.cli import run_loop

    config_path = _write_run_config(tmp_path)
    stop = threading.Event()
    logs: list[str] = []

    def target():
        run_loop(
            config_path=str(config_path),
            source="mock",
            tick=1,
            paper=True,
            ignore_market_hours=True,
            stop_event=stop,
            log=logs.append,
        )

    t = threading.Thread(target=target)
    t.start()
    # Let at least one cycle run, then request stop.
    import time

    time.sleep(0.5)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert any("Run loop starting" in m for m in logs)
    assert any("Run loop finished" in m for m in logs)


# --- KOB_RUN resolution (regression: env must not silently suppress --run) ---


def test_resolve_run_loop_flag_only():
    from killer_options_bot.cli import _resolve_run_loop

    # No KOB_RUN: honor the --run flag as-is.
    assert _resolve_run_loop(None, True) == (True, None)
    assert _resolve_run_loop(None, False) == (False, None)


def test_resolve_run_loop_env_truthy_enables():
    from killer_options_bot.cli import _resolve_run_loop

    for val in ("1", "true", "TRUE", "yes", "on", " On "):
        run, _note = _resolve_run_loop(val, False)
        assert run is True


def test_resolve_run_loop_env_falsy_disables_even_with_flag():
    from killer_options_bot.cli import _resolve_run_loop

    for val in ("0", "false", "no", "off"):
        run, note = _resolve_run_loop(val, True)
        assert run is False
        assert "DISABLED by KOB_RUN" in note


def test_resolve_run_loop_blank_env_does_not_suppress_flag():
    """The Railway footgun: blank KOB_RUN must NOT override --run."""
    from killer_options_bot.cli import _resolve_run_loop

    assert _resolve_run_loop("", True) == (True, None)


def test_resolve_run_loop_garbage_env_warns_and_falls_back():
    from killer_options_bot.cli import _resolve_run_loop

    run, note = _resolve_run_loop("maybe", True)
    assert run is True
    assert "not a recognized boolean" in note

