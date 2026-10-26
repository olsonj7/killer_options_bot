"""RSI sanity-only mode and the IV / realized-vol contract ceiling."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import make_config

from killer_options_bot.config import ContractFilters, RiskConfig, SignalConfig
from killer_options_bot.indicators import realized_vol
from killer_options_bot.models import OptionContract, Side
from killer_options_bot.scanner import _best_contract, _rsi_allows


def _sig(sanity: bool) -> SignalConfig:
    return SignalConfig(
        sma_period=20, rsi_period=14, rsi_min=50, rsi_max=75,
        rsi_sanity_only=sanity,
    )


def test_band_mode_requires_rsi_band():
    s = _sig(False)
    assert not _rsi_allows(45, s, up=True)
    assert _rsi_allows(60, s, up=True)
    assert not _rsi_allows(55, s, up=False)
    assert _rsi_allows(40, s, up=False)


def test_sanity_mode_only_vetoes_extremes():
    s = _sig(True)
    assert _rsi_allows(45, s, up=True)       # weak RSI no longer blocks a call
    assert not _rsi_allows(80, s, up=True)   # blown-out top still blocked
    assert _rsi_allows(55, s, up=False)      # put no longer needs RSI < 50
    assert not _rsi_allows(20, s, up=False)  # capitulation low still blocked


def test_realized_vol():
    assert realized_vol([100.0] * 5, period=20) is None
    assert realized_vol([100.0] * 21, period=20) == pytest.approx(0.0)
    # Alternating +/-1% moves -> ~1% daily stdev -> ~16% annualized.
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    assert realized_vol(closes, period=20) == pytest.approx(0.163, abs=0.01)


def _contract(as_of: date, delta: float, iv: float) -> OptionContract:
    return OptionContract(
        symbol=f"X{delta}{iv}", underlying="X", side=Side.CALL, strike=100.0,
        expiration=as_of + timedelta(days=5), bid=1.00, ask=1.02, last=1.01,
        delta=delta, implied_volatility=iv, volume=500, open_interest=2000,
    )


def _iv_cfg(tmp_path, ratio: float):
    return make_config(
        tmp_path,
        account_value=10000.0,
        risk=RiskConfig(max_trade_risk_pct=0.5, max_open_positions=1,
                        max_trades_per_week=5),
        filters=ContractFilters(
            min_dte=2, max_dte=7, min_delta=0.30, max_delta=0.40,
            max_spread_pct=0.12, min_volume=100, min_open_interest=500,
            max_iv_hv_ratio=ratio,
        ),
    )


def test_iv_ceiling_skips_rich_contract(tmp_path):
    as_of = date(2026, 1, 5)
    rich = _contract(as_of, delta=0.35, iv=0.90)   # closest to target delta
    fair = _contract(as_of, delta=0.32, iv=0.30)
    chain = [rich, fair]

    assert _best_contract(chain, _iv_cfg(tmp_path, 0.0), as_of, hv=0.30) is rich
    assert _best_contract(chain, _iv_cfg(tmp_path, 1.5), as_of, hv=0.30) is fair
    assert _best_contract([rich], _iv_cfg(tmp_path, 1.5), as_of, hv=0.30) is None
    # No realized-vol reading -> filter can't judge, so it doesn't block.
    assert _best_contract([rich], _iv_cfg(tmp_path, 1.5), as_of, hv=None) is rich
