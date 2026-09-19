"""strategy.py - Analisis de estructura y entradas a favor de tendencia."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        _number(candle.get("open", candle.get("open_price"))),
        _number(candle.get("close", candle.get("close_price"))),
        _number(candle.get("min", candle.get("low"))),
        _number(candle.get("max", candle.get("high"))),
    )


def _as_records(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            return [dict(item) for item in value.to_dict("records")]
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        return [dict(value)]
    try:
        return [dict(item) for item in value if isinstance(item, dict)]
    except TypeError:
        return []


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges: List[float] = []
    for index in range(1, len(candles)):
        _, previous_close, _, _ = _ohlc(candles[index - 1])
        _, _, low, high = _ohlc(candles[index])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    values = ranges[-period:]
    return sum(values) / len(values) if values else 0.0


def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _metrics(candle: Dict[str, Any]) -> Dict[str, Any]:
    open_price, close_price, low, high = _ohlc(candle)
    candle_range = max(high - low, 1e-12)
    body = abs(close_price - open_price)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close_price,
        "range": candle_range,
        "body": body,
        "upper": max(high - max(open_price, close_price), 0.0),
        "lower": max(min(open_price, close_price) - low, 0.0),
        "close_position": (close_price - low) / candle_range,
        "bullish": close_price > open_price,
        "bearish": close_price < open_price,
    }


def _empty_result(reason: str = "no_valid_structure") -> Dict[str, Any]:
    return {
        "signal": None,
        "score": 0,
        "score_100": 0,
        "entry_quality": 0,
        "quality": 0,
        "entry_type": "none",
        "direction": "range",
        "reason": reason,
        "analysis": {
            "force": False,
            "structure": "range",
            "atr": 0.0,
            "support": None,
            "resistance": None,
            "tolerance": 0.0,
            "entry_quality": 0,
            "pullback": {"valid": False},
        },
    }


def _structure(records: List[Dict[str, Any]], lookback: int) -> Dict[str, Any]:
    closes = [_ohlc(item)[1] for item in records]
    highs = [_ohlc(item)[3] for item in records]
    lows = [_ohlc(item)[2] for item in records]
    fast = _ema(closes[-min(len(closes), 9):], 9)
    slow = _ema(closes[-min(len(closes), 21):], 21)
    sample = records[-max(10, min(lookback, len(records))):]
    sample_highs = [_ohlc(item)[3] for item in sample]
    sample_lows = [_ohlc(item)[2] for item in sample]
    midpoint = (max(sample_highs) + min(sample_lows)) / 2.0

    recent = records[-6:] if len(records) >= 6 else records
    older = records[-12:-6] if len(records) >= 12 else records[:-len(recent)]
    recent_high = max(_ohlc(item)[3] for item in recent)
    recent_low = min(_ohlc(item)[2] for item in recent)
    older_high = max((_ohlc(item)[3] for item in older), default=recent_high)
    older_low = min((_ohlc(item)[2] for item in older), default=recent_low)

    bullish = fast > slow and recent_high >= older_high and recent_low >= older_low
    bearish = fast < slow and recent_high <= older_high and recent_low <= older_low
    if bullish:
        direction = "bullish"
    elif bearish:
        direction = "bearish"
    else:
        direction = "range"

    return {
        "direction": direction,
        "fast_ema": fast,
        "slow_ema": slow,
        "support": min(sample_lows),
        "resistance": max(sample_highs),
        "midpoint": midpoint,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "older_high": older_high,
        "older_low": older_low,
    }


def _score_structure(last: Dict[str, Any], structure: Dict[str, Any], atr: float) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    direction = structure["direction"]
    close = last["close"]
    body = last["body"]

    if direction == "bullish":
        score += 35
        reasons.append("estructura alcista confirmada")
        if close > structure["fast_ema"]:
            score += 20
            reasons.append("precio sobre EMA rápida")
        if last["bullish"]:
            score += 15
            reasons.append("última vela acompaña al alza")
        if last["close_position"] >= 0.60:
            score += 10
            reasons.append("cierre en zona alta")
    elif direction == "bearish":
        score += 35
        reasons.append("estructura bajista confirmada")
        if close < structure["fast_ema"]:
            score += 20
            reasons.append("precio bajo EMA rápida")
        if last["bearish"]:
            score += 15
            reasons.append("última vela acompaña a la baja")
        if last["close_position"] <= 0.40:
            score += 10
            reasons.append("cierre en zona baja")

    if atr > 0 and body >= atr * 0.10:
        score += 10
        reasons.append("impulso suficiente")
    if atr > 0 and last["range"] >= atr * 0.50:
        score += 10
        reasons.append("rango compatible con movimiento")
    return min(score, 100), reasons


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    **_: Any,
) -> Dict[str, Any]:
    if previous_m1 is not None:
        records = _as_records(previous_m1)
        if isinstance(candle_1m, dict):
            records.append(dict(candle_1m))
    elif df is not None:
        all_records = _as_records(df)
        records = all_records[:-1] if len(all_records) > 1 else []
    else:
        all_records = _as_records(candle_1m)
        records = all_records[:-1] if len(all_records) > 1 else []

    result = _empty_result()
    if len(records) < MIN_CANDLES:
        result["reason"] = "insufficient_candles"
        return result

    atr = _atr(records)
    if atr <= 0:
        result["reason"] = "invalid_atr"
        return result

    structure = _structure(records[:-1], lookback)
    last = _metrics(records[-1])
    score, reasons = _score_structure(last, structure, atr)
    direction = structure["direction"]
    signal = "call" if direction == "bullish" else "put" if direction == "bearish" else None

    aligned = (signal == "call" and last["close"] >= structure["slow_ema"]) or (
        signal == "put" and last["close"] <= structure["slow_ema"]
    )
    if signal and not aligned:
        score = max(0, score - 20)
        reasons.append("precio no está alineado con la EMA lenta")

    force = bool(signal and aligned and score >= int(min_score))
    reason = "; ".join(reasons) if reasons else "estructura lateral o sin confirmación"
    if not force:
        reason = f"señal descartada: {reason}"

    analysis = {
        "force": force,
        "structure": direction if force else direction,
        "atr": atr,
        "support": structure["support"],
        "resistance": structure["resistance"],
        "tolerance": atr * 0.35,
        "entry_quality": score if force else 0,
        "fast_ema": structure["fast_ema"],
        "slow_ema": structure["slow_ema"],
        "rejection_timestamp": records[-1].get("from", records[-1].get("timestamp")),
        "confirmation_timestamp": records[-1].get("from", records[-1].get("timestamp")),
        "rejection_candle": last,
        "confirmation_candle": last,
        "last_swing_high": structure["resistance"],
        "last_swing_low": structure["support"],
        "structure_direction": direction,
        "structure_reasons": reasons,
        "pullback": {
            "valid": force,
            "previous_candle_confirmed": force,
            "extreme_confirmed": force,
        },
    }

    return {
        "signal": signal if force else None,
        "score": score if force else 0,
        "score_100": score if force else 0,
        "entry_quality": score if force else 0,
        "quality": score if force else 0,
        "entry_type": "force" if force else "none",
        "direction": direction,
        "reason": reason,
        "analysis": analysis,
        "candle_timestamp": records[-1].get("from", records[-1].get("timestamp")),
        "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
