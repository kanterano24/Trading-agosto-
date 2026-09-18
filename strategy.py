"""
strategy_mejorada.py

Estrategia estricta de rechazo estructural para M1.

Importante:
- No existe garantía de ganancias.
- Solo devuelve señales cuando se cumplen todos los filtros.
- Mantiene la interfaz analyze_rejection() compatible con bot.py.
- Usa únicamente la última vela cerrada recibida.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Signal:
    action: str  # call, put o none
    score: int
    reason: str
    candle_time: Optional[int] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    entry_price: Optional[float] = None


def _value(candle: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = candle.get(key)
        if value is not None:
            return float(value)
    raise KeyError(keys)


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        _value(candle, "open", "open_price"),
        _value(candle, "close", "close_price"),
        _value(candle, "min", "low"),
        _value(candle, "max", "high"),
    )


def _candle_time(candle: Dict[str, Any]) -> Optional[int]:
    value = candle.get("from", candle.get("at", candle.get("timestamp")))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0

    ranges: List[float] = []
    for i in range(1, len(candles)):
        _, previous_close, _, _ = _ohlc(candles[i - 1])
        _, _, low, high = _ohlc(candles[i])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    selected = ranges[-period:]
    return sum(selected) / len(selected) if selected else 0.0


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _find_zones(candles: List[Dict[str, Any]], lookback: int = 60) -> Tuple[float, float]:
    sample = candles[-lookback:]
    highs = [_ohlc(c)[3] for c in sample]
    lows = [_ohlc(c)[2] for c in sample]
    return min(lows), max(highs)


def _recent_swing_context(candles: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    """Confirma el contexto usando EMA y pendiente reciente del precio."""
    closes = [_ohlc(c)[1] for c in candles[-35:]]
    if len(closes) < 22:
        return False, False

    fast = _ema(closes, 8)
    slow = _ema(closes, 21)
    recent_slope = closes[-1] - closes[-6]

    bullish = fast > slow and recent_slope > 0
    bearish = fast < slow and recent_slope < 0
    return bullish, bearish


def _invalid(reason: str, candle_time: Optional[int] = None, support: Optional[float] = None,
             resistance: Optional[float] = None, entry_price: Optional[float] = None) -> Signal:
    return Signal("none", 0, reason, candle_time, support, resistance, entry_price)


def analyze_rejection(
    candles: List[Dict[str, Any]],
    min_score: int = 90,
    lookback: int = 60,
    zone_atr_factor: float = 0.20,
) -> Signal:
    """Analiza exclusivamente la última vela cerrada."""
    minimum = max(40, lookback // 2)
    if len(candles) < minimum:
        return _invalid("insufficient_candles")

    previous = candles[:-1]
    candle = candles[-1]

    try:
        open_price, close_price, low, high = _ohlc(candle)
        support, resistance = _find_zones(previous, lookback)
    except (KeyError, TypeError, ValueError):
        return _invalid("invalid_ohlc")

    candle_time = _candle_time(candle)
    candle_range = max(high - low, 1e-12)
    body = abs(close_price - open_price)
    body_ratio = body / candle_range
    upper_wick = high - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low

    atr = _atr(previous)
    if atr <= 0:
        return _invalid("invalid_atr", candle_time, support, resistance, close_price)

    tolerance = max(atr * zone_atr_factor, candle_range * 0.06)
    bullish_context, bearish_context = _recent_swing_context(previous)

    near_support = low <= support + tolerance and close_price > support + tolerance * 0.10
    near_resistance = high >= resistance - tolerance and close_price < resistance - tolerance * 0.10

    close_position = (close_price - low) / candle_range
    upper_close_position = (high - close_price) / candle_range

    # Filtros de rechazo: cuerpo moderado, mecha dominante y cierre de recuperación.
    bullish = (
        near_support
        and close_price > open_price
        and lower_wick >= max(body * 1.80, atr * 0.18)
        and close_position >= 0.68
        and body_ratio <= 0.58
        and lower_wick > upper_wick
    )

    bearish = (
        near_resistance
        and close_price < open_price
        and upper_wick >= max(body * 1.80, atr * 0.18)
        and upper_close_position >= 0.68
        and body_ratio <= 0.58
        and upper_wick > lower_wick
    )

    # Exigimos alineación de contexto para evitar entradas contra la pendiente.
    if bullish:
        score = 76
        reasons = ["support_rejection"]

        if bullish_context:
            score += 8
            reasons.append("bullish_ema_and_slope")
        else:
            return _invalid("call_context_not_aligned", candle_time, support, resistance, close_price)

        if lower_wick >= body * 2.40:
            score += 5
            reasons.append("strong_lower_wick")

        if close_position >= 0.78:
            score += 4
            reasons.append("strong_recovery_close")

        if body_ratio <= 0.40:
            score += 3
            reasons.append("controlled_body")

        if close_price > support + tolerance * 0.50:
            score += 4
            reasons.append("clear_support_reclaim")

        if score >= min_score:
            return Signal("call", min(score, 100), ",".join(reasons), candle_time,
                          support, resistance, close_price)

    if bearish:
        score = 76
        reasons = ["resistance_rejection"]

        if bearish_context:
            score += 8
            reasons.append("bearish_ema_and_slope")
        else:
            return _invalid("put_context_not_aligned", candle_time, support, resistance, close_price)

        if upper_wick >= body * 2.40:
            score += 5
            reasons.append("strong_upper_wick")

        if upper_close_position >= 0.78:
            score += 4
            reasons.append("strong_rejection_close")

        if body_ratio <= 0.40:
            score += 3
            reasons.append("controlled_body")

        if close_price < resistance - tolerance * 0.50:
            score += 4
            reasons.append("clear_resistance_reclaim")

        if score >= min_score:
            return Signal("put", min(score, 100), ",".join(reasons), candle_time,
                          support, resistance, close_price)

    return _invalid("no_valid_strict_rejection", candle_time, support, resistance, close_price)
