"""Lightweight technical indicators used for the momentum signal.

Pure-Python, no numpy dependency, so the bot stays easy to install.
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Returns None if there is not enough data.

    Returns a value in [0, 100].
    """
    if period <= 0 or len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0
    # Seed with the first ``period`` changes.
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period

    # Wilder smoothing for the remaining changes.
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)
