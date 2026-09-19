"""strategy.py - Rechazo de soporte/resistencia con confirmacion N-2/N-1."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
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


def _zones(candles: List[Dict[str, Any]], lookback: int = DEFAULT_LOOKBACK) -> Tuple[float, float]:
    sample = candles[-max(1, lookback):]
    lows = [_ohlc(candle)[2] for candle in sample]
    highs = [_ohlc(candle)[3] for candle in sample]
    return (min(lows), max(highs)) if lows and highs else (0.0, 0.0)


def _candle_metrics(candle: Dict[str, Any]) -> Dict[str, Any]:
    open_price, close_price, low, high = _ohlc(candle)
    candle_range = max(high - low, 1e-12)
    body = abs(close_price - open_price)
    upper = max(high - max(open_price, close_price), 0.0)
    lower = max(min(open_price, close_price) - low, 0.0)
    close_position = (close_price - low) / candle_range
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close_price,
        "range": candle_range,
        "body": body,
        "upper": upper,
        "lower": lower,
        "close_position": close_position,
        "bullish": close_price > open_price,
        "bearish": close_price < open_price,
    }


def _empty_result(reason: str = "no_valid_rejection") -> Dict[str, Any]:
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


def _score_rejection(metrics: Dict[str, Any], side: str, level: float, tolerance: float, atr: float) -> Tuple[int, List[str]]:
    body = metrics["body"]
    wick = metrics["lower"] if side == "call" else metrics["upper"]
    close_position = metrics["close_position"] if side == "call" else 1.0 - metrics["close_position"]
    close_distance = (metrics["close"] - level) if side == "call" else (level - metrics["close"])

    score = 0
    reasons: List[str] = []
    if wick >= max(body * 1.25, atr * 0.20):
        score += 35
        reasons.append("mecha de rechazo válida")
    if wick >= body * 2:
        score += 15
        reasons.append("mecha claramente superior al cuerpo")
    if close_position >= 0.60:
        score += 20
        reasons.append("cierre favorable")
    if close_distance >= tolerance * 0.25:
        score += 15
        reasons.append("cierre separado de la zona")
    if body > 0:
        score += 10
        reasons.append("cuerpo confirmado")
    if metrics["range"] >= atr * 0.50:
        score += 5
        reasons.append("rango suficiente")
    return min(score, 100), reasons


def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
) -> Dict[str, Any]:
    records = _as_records(candles)
    if len(records) < max(MIN_CANDLES, lookback // 2):
        return {"signal": None, "score": 0, "reason": "insufficient_candles"}

    previous = records[:-1]
    candle = records[-1]
    metrics = _candle_metrics(candle)
    atr = _atr(previous)
    if atr <= 0:
        return {"signal": None, "score": 0, "reason": "invalid_atr"}

    support, resistance = _zones(previous, lookback)
    tolerance = atr * max(zone_atr_factor, 0.01)
    near_support = metrics["low"] <= support + tolerance and metrics["close"] > support
    near_resistance = metrics["high"] >= resistance - tolerance and metrics["close"] < resistance

    if near_support and metrics["bullish"]:
        score, reasons = _score_rejection(metrics, "call", support, tolerance, atr)
        if score >= int(min_score):
            return {"signal": "call", "score": score, "reason": "bullish_support_rejection", "level": support, "reasons": reasons, "metrics": metrics, "atr": atr, "support": support, "resistance": resistance, "tolerance": tolerance}

    if near_resistance and metrics["bearish"]:
        score, reasons = _score_rejection(metrics, "put", resistance, tolerance, atr)
        if score >= int(min_score):
            return {"signal": "put", "score": score, "reason": "bearish_resistance_rejection", "level": resistance, "reasons": reasons, "metrics": metrics, "atr": atr, "support": support, "resistance": resistance, "tolerance": tolerance}

    return {"signal": None, "score": 0, "reason": "no_valid_rejection", "support": support, "resistance": resistance, "tolerance": tolerance, "atr": atr}


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    **_: Any,
) -> Dict[str, Any]:
    if previous_m1 is not None:
        records = _as_records(previous_m1)
        if candle_1m is not None and isinstance(candle_1m, dict):
            records.append(dict(candle_1m))
    elif df is not None:
        all_records = _as_records(df)
        records = all_records[:-1] if len(all_records) > 1 else []
    else:
        all_records = _as_records(candle_1m)
        records = all_records[:-1] if len(all_records) > 1 else []

    result = _empty_result()
    if len(records) < MIN_CANDLES + 1:
        result["reason"] = "insufficient_candles"
        return result

    # La última vela de records es N-1; la anterior es N-2.
    n2 = records[-2]
    n1 = records[-1]
    n2_metrics = _candle_metrics(n2)
    n1_metrics = _candle_metrics(n1)
    n1_result = analyze_rejection(records, min_score=min_score)
    signal = n1_result.get("signal")
    atr = _atr(records[:-1])
    support = n1_result.get("support")
    resistance = n1_result.get("resistance")
    tolerance = n1_result.get("tolerance", atr * 0.35)

    confirmation_ok = False
    confirmation_reason = "sin confirmación de N-2"
    if signal == "call":
        confirmation_ok = n2_metrics["low"] <= float(support or 0.0) + tolerance and n2_metrics["bullish"] or n2_metrics["close"] >= n2_metrics["open"]
        confirmation_reason = "N-2 acompaña el movimiento alcista" if confirmation_ok else "N-2 no confirma el movimiento alcista"
    elif signal == "put":
        confirmation_ok = n2_metrics["high"] >= float(resistance or 0.0) - tolerance and n2_metrics["bearish"] or n2_metrics["close"] <= n2_metrics["open"]
        confirmation_reason = "N-2 acompaña el movimiento bajista" if confirmation_ok else "N-2 no confirma el movimiento bajista"

    if not signal:
        result["reason"] = n1_result.get("reason", "no_valid_rejection")
        return result

    # Se exige que N-2 no contradiga fuertemente la dirección de N-1.
    if signal == "call" and n2_metrics["bearish"] and n2_metrics["body"] > n1_metrics["body"] * 1.5:
        confirmation_ok = False
        confirmation_reason = "N-2 contradice con cuerpo bajista dominante"
    if signal == "put" and n2_metrics["bullish"] and n2_metrics["body"] > n1_metrics["body"] * 1.5:
        confirmation_ok = False
        confirmation_reason = "N-2 contradice con cuerpo alcista dominante"

    score = int(n1_result.get("score", 0))
    if confirmation_ok:
        score = min(100, score + 10)
    else:
        score = max(0, score - 25)

    force = bool(signal and confirmation_ok and score >= int(min_score))
    direction = "bullish" if signal == "call" else "bearish"
    reason = f"{n1_result.get('reason', '')}; {confirmation_reason}"
    if not force:
        reason = f"señal descartada: {reason}"

    analysis = {
        "force": force,
        "structure": direction if force else "range",
        "atr": atr,
        "support": support,
        "resistance": resistance,
        "tolerance": tolerance,
        "entry_quality": score if force else 0,
        "rejection_timestamp": n1.get("from", n1.get("timestamp")),
        "confirmation_timestamp": n2.get("from", n2.get("timestamp")),
        "rejection_candle": n1_metrics,
        "confirmation_candle": n2_metrics,
        "last_swing_high": resistance,
        "last_swing_low": support,
        "confirmation_ok": confirmation_ok,
        "confirmation_reason": confirmation_reason,
        "n2_direction": "bullish" if n2_metrics["bullish"] else "bearish" if n2_metrics["bearish"] else "neutral",
        "pullback": {
            "valid": force,
            "previous_candle_confirmed": confirmation_ok,
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
        "direction": direction if force else "range",
        "reason": reason,
        "analysis": analysis,
        "candle_timestamp": n1.get("from", n1.get("timestamp")),
        "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
