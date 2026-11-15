"""Technical indicators, computed in plain Python so the numbers the AI sees
are deterministic and checkable."""
from __future__ import annotations

from .models import Candle


def ema(values: list[float], period: int) -> float | None:
    """Exponential moving average of the whole series; None if too short."""
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    average = sum(values[:period]) / period  # seed with a simple average
    for value in values[period:]:
        average = value * alpha + average * (1 - alpha)
    return average


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. None if there are fewer than period + 1 values."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for previous, current in zip(values[:period], values[1 : period + 1]):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for previous, current in zip(values[period:], values[period + 1 :]):
        change = current - previous
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Wilder's average true range. None if there are fewer than period + 1 candles."""
    if len(candles) < period + 1:
        return None
    true_ranges = [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in zip(candles, candles[1:])
    ]
    average = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        average = (average * (period - 1) + value) / period
    return average


def recent_range(candles: list[Candle], lookback: int) -> tuple[float, float] | None:
    """(highest high, lowest low) over the last `lookback` candles."""
    window = candles[-lookback:]
    if not window:
        return None
    return max(candle.high for candle in window), min(candle.low for candle in window)
