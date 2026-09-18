"""
strategy.py
Estrategia exclusiva de rechazo de soporte/resistencia para M1.

La función analyze_rejection():
- Usa únicamente velas cerradas.
- Busca zonas basadas en máximos/mínimos recientes.
- Exige interacción con la zona y mecha de rechazo.
- Añade filtros de cuerpo, cierre y contexto.
- Devuelve una señal con score, nivel y timestamp de la vela.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Signal:
    action: str  # "call", "put" o "none"
    score: int
    reason: str
    candle_time: Optional[int] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    entry_price: Optional[float] = None


def _value(candle: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        if key in candle and candle[key] is not None:
            return float(candle[key])
    raise KeyError(f"No se encontró ninguno de estos campos: {keys}")


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    open_price = _value(candle, "open", "open_price")
    close_price = _value(candle, "close", "close_price")
    low = _value(candle, "min", "low")
    high = _value(candle, "max", "high")
    return open_price, close_price, low, high


def _candle_time(candle: Dict[str, Any]) -> Optional[int]:
    value = candle.get("from", candle.get("at", candle.get("timestamp")))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0

    true_ranges = []
    for index in range(1, len(candles)):
        _, _, low, high = _ohlc(candles[index])
        _, previous_close, _, _ = _ohlc(candles[index - 1])
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    values = true_ranges[-period:]
    return sum(values) / len(values) if values else 0.0


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0

    alpha = 2.0 / (period + 1.0)
    result = values[0]

    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result

    return result


def _find_zones(
    candles: List[Dict[str, Any]],
    lookback: int = 60,
) -> Tuple[float, float]:
    sample = candles[-lookback:]
    highs = [_ohlc(candle)[3] for candle in sample]
    lows = [_ohlc(candle)[2] for candle in sample]
    return min(lows), max(highs)


def analyze_rejection(
    candles: List[Dict[str, Any]],
    min_score: int = 80,
    lookback: int = 60,
    zone_atr_factor: float = 0.30,
) -> Signal:
    """
    Analiza la última vela cerrada recibida.

    Importante:
    El llamador debe eliminar la vela actualmente en formación antes
    de llamar a esta función.
    """
    minimum = max(30, lookback // 2)
    if len(candles) < minimum:
        return Signal(action="none", score=0, reason="insufficient_candles")

    previous = candles[:-1]
    candle = candles[-1]

    try:
        open_price, close_price, low, high = _ohlc(candle)
    except (KeyError, TypeError, ValueError):
        return Signal(action="none", score=0, reason="invalid_ohlc")

    candle_range = max(high - low, 1e-12)
    body = abs(close_price - open_price)
    upper_wick = high - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low

    atr = _atr(previous)
    if atr <= 0:
        return Signal(action="none", score=0, reason="invalid_atr")

    support, resistance = _find_zones(previous, lookback)
    tolerance = max(atr * zone_atr_factor, candle_range * 0.10)

    near_support = low <= support + tolerance and close_price > support
    near_resistance = high >= resistance - tolerance and close_price < resistance

    closes = [_ohlc(item)[1] for item in previous[-30:]]
    ema_fast = _ema(closes, 8)
    ema_slow = _ema(closes, 21)

    # Contexto: no se exige una tendencia concreta, pero se evita
    # una señal cuando el cierre está extremadamente extendido.
    bullish_context = ema_fast >= ema_slow
    bearish_context = ema_fast <= ema_slow

    bullish_rejection = (
        near_support
        and lower_wick >= max(body * 1.40, atr * 0.20)
        and close_price > open_price
        and (close_price - low) / candle_range >= 0.62
        and body / candle_range <= 0.65
    )

    bearish_rejection = (
        near_resistance
        and upper_wick >= max(body * 1.40, atr * 0.20)
        and close_price < open_price
        and (high - close_price) / candle_range >= 0.62
        and body / candle_range <= 0.65
    )

    candle_time = _candle_time(candle)

    if bullish_rejection:
        score = 76
        reasons = ["bullish_support_rejection"]

        if lower_wick >= body * 2.0:
            score += 7
            reasons.append("long_lower_wick")
        if bullish_context:
            score += 5
            reasons.append("ema_context")
        if close_price > support + tolerance * 0.25:
            score += 5
            reasons.append("close_above_support")

        if score >= min_score:
            return Signal(
                "call",
                min(score, 100),
                ",".join(reasons),
                candle_time=candle_time,
                support=support,
                resistance=resistance,
                entry_price=close_price,
            )

    if bearish_rejection:
        score = 76
        reasons = ["bearish_resistance_rejection"]

        if upper_wick >= body * 2.0:
            score += 7
            reasons.append("long_upper_wick")
        if bearish_context:
            score += 5
            reasons.append("ema_context")
        if close_price < resistance - tolerance * 0.25:
            score += 5
            reasons.append("close_below_resistance")

        if score >= min_score:
            return Signal(
                "put",
                min(score, 100),
                ",".join(reasons),
                candle_time=candle_time,
                support=support,
                resistance=resistance,
                entry_price=close_price,
            )

    return Signal(
        "none",
        0,
        "no_valid_rejection",
        candle_time=candle_time,
        support=support,
        resistance=resistance,
        entry_price=close_price,
    )
