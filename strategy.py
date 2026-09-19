"""Estrategia de reversion en soporte/resistencia para binarias de 1 minuto.

Reglas principales:
- Estudia la estructura previa antes de evaluar la vela activa.
- Calcula soporte y resistencia usando velas anteriores, sin usar la vela de entrada.
- Revisa las velas que se aproximan al nivel.
- Solo genera CALL en soporte o PUT en resistencia cuando existe rechazo real.
- Rechaza velas sin mecha de rechazo, con cuerpo insuficiente o con ruptura clara.
- La señal se produce sobre la vela activa cuando toca el nivel y cumple las condiciones.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 35
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 90

LEVEL_LOOKBACK = 24
LEVEL_TOLERANCE_ATR = 0.30
MIN_REJECTION_WICK_ATR = 0.12
MIN_BODY_ATR = 0.08
MIN_REJECTION_RATIO = 0.35
MAX_BREAK_ATR = 0.20
MAX_ENTRY_EXTENSION_ATR = 1.25
MIN_APPROACH_CANDLES = 2


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _timestamp(candle: Dict[str, Any]) -> Optional[int]:
    value = candle.get("from", candle.get("timestamp", candle.get("time")))
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def _clean_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared = [item for item in records if isinstance(item, dict)]
    prepared.sort(key=lambda item: (_timestamp(item) is None, _timestamp(item) or 0))
    unique: Dict[int, Dict[str, Any]] = {}
    without_ts: List[Dict[str, Any]] = []
    for item in prepared:
        ts = _timestamp(item)
        if ts is None:
            without_ts.append(item)
        else:
            unique[ts] = item
    return list(unique.values()) + without_ts


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
        "timestamp": _timestamp(candle),
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


def _empty(reason: str) -> Dict[str, Any]:
    return {
        "signal": None, "score": 0, "score_100": 0, "entry_quality": 0,
        "quality": 0, "entry_type": "none", "direction": "range",
        "reason": reason,
        "analysis": {"force": False, "structure": "range", "atr": 0.0,
                      "support": None, "resistance": None, "tolerance": 0.0,
                      "entry_quality": 0, "reversal": {"valid": False}},
    }


def _structure(records: List[Dict[str, Any]], lookback: int) -> Dict[str, Any]:
    sample = records[-max(LEVEL_LOOKBACK, min(lookback, len(records))):]
    closes = [_ohlc(item)[1] for item in sample]
    highs = [_ohlc(item)[3] for item in sample]
    lows = [_ohlc(item)[2] for item in sample]
    fast = _ema(closes[-9:], 9)
    slow = _ema(closes[-21:], 21)
    half = max(6, len(sample) // 2)
    recent = sample[-half:]
    older = sample[:-half] or sample
    recent_high = max(_ohlc(item)[3] for item in recent)
    recent_low = min(_ohlc(item)[2] for item in recent)
    older_high = max(_ohlc(item)[3] for item in older)
    older_low = min(_ohlc(item)[2] for item in older)
    bullish = fast > slow and recent_high >= older_high and recent_low >= older_low
    bearish = fast < slow and recent_high <= older_high and recent_low <= older_low
    direction = "bullish" if bullish else "bearish" if bearish else "range"
    return {
        "direction": direction, "fast_ema": fast, "slow_ema": slow,
        "support": min(lows), "resistance": max(highs),
        "recent_high": recent_high, "recent_low": recent_low,
        "older_high": older_high, "older_low": older_low,
    }


def _approach_ok(history: List[Dict[str, Any]], level: float, side: str, atr: float) -> bool:
    if len(history) < MIN_APPROACH_CANDLES:
        return False
    recent = [_metrics(item) for item in history[-MIN_APPROACH_CANDLES:]]
    if side == "support":
        # El precio debe venir descendiendo hacia el soporte, sin una caída extrema.
        return recent[-1]["close"] <= recent[0]["close"] + atr * 0.35
    # El precio debe venir ascendiendo hacia la resistencia.
    return recent[-1]["close"] >= recent[0]["close"] - atr * 0.35


def _reversal_at_level(current: Dict[str, Any], side: str, level: float, atr: float) -> Tuple[bool, str, int]:
    tolerance = atr * LEVEL_TOLERANCE_ATR
    touched = current["low"] <= level + tolerance if side == "support" else current["high"] >= level - tolerance
    if not touched:
        return False, "no toco el nivel", 0

    body_ok = current["body"] >= atr * MIN_BODY_ATR
    if side == "support":
        wick_ok = current["lower"] >= atr * MIN_REJECTION_WICK_ATR
        rejection_ratio_ok = current["lower"] >= max(current["body"] * MIN_REJECTION_RATIO, 1e-12)
        close_ok = current["close"] > current["open"] and current["close_position"] >= 0.55
        break_ok = current["close"] >= level - atr * MAX_BREAK_ATR
        valid = all((body_ok, wick_ok, rejection_ratio_ok, close_ok, break_ok))
        missing = []
        if not body_ok: missing.append("cuerpo insuficiente")
        if not wick_ok or not rejection_ratio_ok: missing.append("mecha inferior de rechazo insuficiente")
        if not close_ok: missing.append("cierre no alcista")
        if not break_ok: missing.append("ruptura del soporte")
    else:
        wick_ok = current["upper"] >= atr * MIN_REJECTION_WICK_ATR
        rejection_ratio_ok = current["upper"] >= max(current["body"] * MIN_REJECTION_RATIO, 1e-12)
        close_ok = current["close"] < current["open"] and current["close_position"] <= 0.45
        break_ok = current["close"] <= level + atr * MAX_BREAK_ATR
        valid = all((body_ok, wick_ok, rejection_ratio_ok, close_ok, break_ok))
        missing = []
        if not body_ok: missing.append("cuerpo insuficiente")
        if not wick_ok or not rejection_ratio_ok: missing.append("mecha superior de rechazo insuficiente")
        if not close_ok: missing.append("cierre no bajista")
        if not break_ok: missing.append("ruptura de la resistencia")

    return valid, "; ".join(missing) if missing else "rechazo confirmado", 70 if valid else 0


def analyze_market(candle_1m: Any = None, previous_m1: Any = None, pair: Optional[str] = None,
                   df: Any = None, min_score: int = DEFAULT_MIN_SCORE,
                   lookback: int = DEFAULT_LOOKBACK, **_: Any) -> Dict[str, Any]:
    if previous_m1 is not None:
        records = _as_records(previous_m1)
        if isinstance(candle_1m, dict):
            records.append(dict(candle_1m))
    elif df is not None:
        records = _as_records(df)
    else:
        records = _as_records(candle_1m)
    records = _clean_records(records)
    if len(records) < MIN_CANDLES:
        return _empty("insufficient_candles")

    current = _metrics(records[-1])
    history = records[:-1]
    atr = _atr(records)
    if atr <= 0:
        return _empty("invalid_atr")

    structure = _structure(history, lookback)
    support = float(structure["support"])
    resistance = float(structure["resistance"])
    near_support = current["low"] <= support + atr * LEVEL_TOLERANCE_ATR
    near_resistance = current["high"] >= resistance - atr * LEVEL_TOLERANCE_ATR

    support_approach = _approach_ok(history, support, "support", atr)
    resistance_approach = _approach_ok(history, resistance, "resistance", atr)
    call_valid, call_reason, call_points = _reversal_at_level(current, "support", support, atr)
    put_valid, put_reason, put_points = _reversal_at_level(current, "resistance", resistance, atr)

    signal: Optional[str] = None
    reversal_side = "none"
    reversal_valid = False
    score = 0
    reasons: List[str] = []

    # En soporte se busca CALL; en resistencia se busca PUT.
    if near_support and support_approach and call_valid:
        signal, reversal_side, reversal_valid, score = "call", "support", True, call_points
        reasons.extend(["precio toco soporte", "velas llegaron al soporte", call_reason])
    elif near_resistance and resistance_approach and put_valid:
        signal, reversal_side, reversal_valid, score = "put", "resistance", True, put_points
        reasons.extend(["precio toco resistencia", "velas llegaron a la resistencia", put_reason])
    else:
        if near_support or near_resistance:
            reasons.append("nivel tocado pero condiciones de reversion no cumplidas")
        if near_support and not support_approach:
            reasons.append("llegada al soporte sin aproximacion valida")
        if near_resistance and not resistance_approach:
            reasons.append("llegada a resistencia sin aproximacion valida")
        if near_support and support_approach and not call_valid:
            reasons.append(f"CALL rechazado: {call_reason}")
        if near_resistance and resistance_approach and not put_valid:
            reasons.append(f"PUT rechazado: {put_reason}")

    if signal:
        distance_ema = abs(current["close"] - structure["fast_ema"]) / atr
        if distance_ema > MAX_ENTRY_EXTENSION_ATR:
            signal = None
            reversal_valid = False
            score = 0
            reasons.append("entrada descartada por extension excesiva")
        else:
            score += 20 if structure["direction"] != "range" else 10
            score += 10 if distance_ema <= 0.75 else 0
            reasons.append(f"estructura previa: {structure['direction']}")
            reasons.append("entrada de reversion validada")

    force = bool(signal and reversal_valid and score >= int(min_score))
    if not force:
        signal = None
        score = 0
        reason = "; ".join(reasons) if reasons else "sin toque ni rechazo valido"
        reason = "senal descartada: " + reason
    else:
        reason = "; ".join(reasons)

    analysis = {
        "force": force, "structure": structure["direction"],
        "structure_direction": structure["direction"], "atr": atr,
        "support": support, "resistance": resistance,
        # Campos de compatibilidad consumidos por bot.py para revalidar el espacio.
        "last_swing_high": resistance,
        "last_swing_low": support,
        "tolerance": atr * LEVEL_TOLERANCE_ATR,
        "entry_quality": score if force else 0,
        "fast_ema": structure["fast_ema"], "slow_ema": structure["slow_ema"],
        "room_atr": None, "location_ok": True,
        "impulse_phase": "touch_reversal" if force else "waiting_touch_or_reversal",
        "reversal_side": reversal_side, "reversal_valid": reversal_valid,
        "support_approach": support_approach, "resistance_approach": resistance_approach,
        "rejection_timestamp": current["timestamp"],
        "confirmation_timestamp": current["timestamp"],
        "rejection_candle": current, "confirmation_candle": current,
        "current_candle": current, "structure_reasons": reasons,
        "reversal": {"valid": reversal_valid, "side": reversal_side},
        "pullback": {
            "valid": bool(reversal_valid),
            "previous_candle_confirmed": bool(reversal_valid),
            "extreme_confirmed": bool(reversal_valid),
        },
    }
    return {
        "signal": signal if force else None,
        "score": score if force else 0,
        "score_100": score if force else 0,
        "entry_quality": score if force else 0,
        "quality": score if force else 0,
        "entry_type": "force" if force else "none",
        "direction": structure["direction"], "reason": reason,
        "analysis": analysis, "candle_timestamp": current["timestamp"], "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
