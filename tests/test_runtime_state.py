"""Tests for cross-process runtime state: scan heartbeat + strategy toggles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from killer_options_bot.storage import SQLiteStorage
from killer_options_bot.web import (
    _fmt_age,
    _render_status_banner,
    _render_strategy_toggles,
)


def _store(tmp_path) -> SQLiteStorage:
    # A file-backed DB (not :memory:) so schema persists across connections.
    return SQLiteStorage(tmp_path / "state.db")


# --- runtime_state key/value -------------------------------------------------


def test_get_state_default_when_unset(tmp_path):
    st = _store(tmp_path)
    assert st.get_state("nope") is None
    assert st.get_state("nope", "fallback") == "fallback"


def test_set_and_get_state_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.set_state("k", "v1")
    assert st.get_state("k") == "v1"
    # Upsert overwrites in place (PRIMARY KEY on key).
    st.set_state("k", "v2")
    assert st.get_state("k") == "v2"


def test_get_state_row_returns_value_and_timestamp(tmp_path):
    st = _store(tmp_path)
    st.set_state("k", "v")
    row = st.get_state_row("k")
    assert row is not None
    value, updated_at = row
    assert value == "v"
    # updated_at parses as an ISO timestamp.
    datetime.fromisoformat(updated_at)


# --- strategy toggles --------------------------------------------------------


def test_strategy_enabled_defaults_true(tmp_path):
    st = _store(tmp_path)
    assert st.strategy_enabled("zerodte") is True


def test_set_strategy_enabled_false_then_true(tmp_path):
    st = _store(tmp_path)
    st.set_strategy_enabled("zerodte", False)
    assert st.strategy_enabled("zerodte") is False
    st.set_strategy_enabled("zerodte", True)
    assert st.strategy_enabled("zerodte") is True


def test_strategy_toggles_are_independent(tmp_path):
    st = _store(tmp_path)
    st.set_strategy_enabled("default", True)
    st.set_strategy_enabled("zerodte", False)
    assert st.strategy_enabled("default") is True
    assert st.strategy_enabled("zerodte") is False


# --- _fmt_age ----------------------------------------------------------------


def test_fmt_age_seconds_minutes_hours():
    assert _fmt_age(5) == "5s"
    assert _fmt_age(125) == "2m"
    assert _fmt_age(3 * 3600 + 4 * 60) == "3h 4m"


def test_fmt_age_clamps_negative():
    assert _fmt_age(-10) == "0s"


# --- status banner -----------------------------------------------------------


def test_status_banner_never_reported(tmp_path):
    st = _store(tmp_path)
    html = _render_status_banner(st)
    assert "status-off" in html
    assert "not running" in html.lower()


def test_status_banner_scanning_when_fresh(tmp_path):
    st = _store(tmp_path)
    st.set_state("loop_heartbeat", "scanning")
    html = _render_status_banner(st)
    assert "status-on" in html
    assert "scanning" in html.lower()


def test_status_banner_idle_when_market_closed(tmp_path):
    st = _store(tmp_path)
    st.set_state("loop_heartbeat", "market_closed")
    html = _render_status_banner(st)
    assert "status-idle" in html


def test_status_banner_off_when_stale(tmp_path):
    st = _store(tmp_path)
    # Manually write a stale heartbeat (>5 min old) to simulate a dead loop.
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    st._execute(
        "INSERT INTO runtime_state (key, value, updated_at) VALUES (?,?,?)",
        ("loop_heartbeat", "scanning", stale),
    )
    html = _render_status_banner(st)
    assert "status-off" in html


# --- strategy toggle rendering ----------------------------------------------


class _FakeStrategy:
    def __init__(self, name: str, signal: str):
        self.name = name
        self.signal = signal


class _FakeConfig:
    def __init__(self, strategies):
        self.active_strategies = tuple(strategies)


def test_render_toggles_reflects_state(tmp_path):
    st = _store(tmp_path)
    st.set_strategy_enabled("zerodte", False)
    cfg = _FakeConfig(
        [
            _FakeStrategy("default", "momentum"),
            _FakeStrategy("zerodte", "intraday_momentum"),
        ]
    )
    html = _render_strategy_toggles(cfg, st)
    # default enabled -> checkbox checked; zerodte disabled -> not checked.
    assert "value='default' checked" in html
    assert "value='zerodte' " in html
    assert "value='zerodte' checked" not in html
    assert "/strategies" in html


def test_render_toggles_empty_when_no_strategies(tmp_path):
    st = _store(tmp_path)
    assert _render_strategy_toggles(_FakeConfig([]), st) == ""
