"""
strategy.py
Estrategia exclusiva de rechazo de soporte/resistencia para velas M1.

No usa continuation. Devuelve señales únicamente cuando:
1) El precio interactúa con una zona S/R.
2) Existe mecha de rechazo.
3) El cierre confirma la dirección.
4) La vela está cerrada.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Signal:
    action: str                 # "call", "put" o "none"
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


def _ohlc(candle: Dict[str, Any]):
    return (
        float(candle.get("open", candle.get("open_price"))),
        float(candle.get("close", candle.get("close_price"))),
        float(candle.get("min", candle.get("low"))),
        float(candle.get("max", candle.get("high"))),
    )


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        o, c, low, high = _ohlc(candles[i])
        _, prev_close, _, _ = _ohlc(candles[i - 1])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    values = trs[-period:]
    return sum(values) / len(values) if values else 0.0


def _zones(candles: List[Dict[str, Any]], lookback: int = 60):
    sample = candles[-lookback:]
    highs = [_ohlc(c)[3] for c in sample]
    lows = [_ohlc(c)[2] for c in sample]
    return min(lows), max(highs)


def analyze_rejection(
    candles: List[Dict[str, Any]],
    min_score: int = 75,
    lookback: int = 60,
    zone_atr_factor: float = 0.35,
) -> Signal:
    """
    Analiza la última vela cerrada.
    Se recomienda enviar únicamente velas cerradas y eliminar la vela actual.
    """
    if len(candles) < max(20, lookback // 2):
        return Signal("none", 0, "insufficient_candles")

    prev = candles[:-1]
    candle = candles[-1]
    o, c, low, high = _ohlc(candle)

    body = abs(c - o)
    rng = max(high - low, 1e-12)
    upper_wick = high - max(o, c)
    lower_wick = min(o, c) - low
    atr = _atr(prev)

    if atr <= 0:
        return Signal("none", 0, "invalid_atr")

    support, resistance = _zones(prev, lookback)
    tolerance = atr * zone_atr_factor

    near_support = low <= support + tolerance and c > support
    near_resistance = high >= resistance - tolerance and c < resistance

    bullish_rejection = (
        near_support
        and lower_wick >= max(body * 1.25, atr * 0.20)
        and c > o
        and (c - low) / rng >= 0.60
    )
    bearish_rejection = (
        near_resistance
        and upper_wick >= max(body * 1.25, atr * 0.20)
        and c < o
        and (high - c) / rng >= 0.60
    )

    if bullish_rejection:
        score = 75
        if lower_wick >= body * 2:
            score += 8
        if c > support + tolerance * 0.25:
            score += 5
        if score >= min_score:
            return Signal("call", min(score, 100), "bullish_support_rejection",
                          support=support, resistance=resistance)

    if bearish_rejection:
        score = 75
        if upper_wick >= body * 2:
            score += 8
        if c < resistance - tolerance * 0.25:
            score += 5
        if score >= min_score:
            return Signal("put", min(score, 100), "bearish_resistance_rejection",
                          support=support, resistance=resistance)

    return Signal("none", 0, "no_valid_rejection",
                  support=support, resistance=resistance)
