"""strategy.py - Estructura + pullback + continuacion a favor de tendencia.

La estrategia trabaja exclusivamente con velas cerradas. La ultima vela del
conjunto es la vela de confirmacion (N-1) y la anterior es el retroceso (N-2).
Nunca se reutiliza la misma vela para ambos papeles.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 30
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75

# Filtros de ubicacion y calidad. Son conservadores para evitar entrar tarde.
PULLBACK_MAX_ATR = 1.25
MIN_ROOM_ATR = 0.45
MIN_BODY_ATR = 0.10
MIN_CONTINUATION_CLOSE = 0.60


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
    """Ordena y elimina timestamps duplicados para no comparar una vela consigo misma."""
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


def _empty_result(reason: str = "no_valid_setup") -> Dict[str, Any]:
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
    sample = records[-max(20, min(lookback, len(records))):]
    closes = [_ohlc(item)[1] for item in sample]
    highs = [_ohlc(item)[3] for item in sample]
    lows = [_ohlc(item)[2] for item in sample]
    fast = _ema(closes[-9:], 9)
    slow = _ema(closes[-21:], 21)

    # Compara dois bloques para exigir una secuencia de maximos/minimos.
    recent = sample[-8:]
    older = sample[-16:-8] if len(sample) >= 16 else sample[:-8]
    recent_high = max((_ohlc(item)[3] for item in recent), default=0.0)
    recent_low = min((_ohlc(item)[2] for item in recent), default=0.0)
    older_high = max((_ohlc(item)[3] for item in older), default=recent_high)
    older_low = min((_ohlc(item)[2] for item in older), default=recent_low)

    bullish = fast > slow and recent_high >= older_high and recent_low >= older_low
    bearish = fast < slow and recent_high <= older_high and recent_low <= older_low
    direction = "bullish" if bullish else "bearish" if bearish else "range"

    return {
        "direction": direction,
        "fast_ema": fast,
        "slow_ema": slow,
        "support": min(lows) if lows else None,
        "resistance": max(highs) if highs else None,
        "midpoint": (max(highs) + min(lows)) / 2.0 if highs and lows else None,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "older_high": older_high,
        "older_low": older_low,
    }


def _near(value: float, level: float, tolerance: float) -> bool:
    return abs(value - level) <= tolerance


def _pullback_and_continuation(
    direction: str,
    pullback: Dict[str, Any],
    continuation: Dict[str, Any],
    structure: Dict[str, Any],
    atr: float,
) -> Dict[str, Any]:
    """Valida N-2 como retroceso y N-1 como continuacion."""
    tolerance = atr * 0.35
    fast_ema = float(structure["fast_ema"])
    result = {
        "valid": False,
        "previous_candle_confirmed": False,
        "extreme_confirmed": False,
        "pullback_timestamp": pullback.get("timestamp"),
        "continuation_timestamp": continuation.get("timestamp"),
        "reason": "",
    }

    if direction == "bullish":
        touched_reference = (
            pullback["low"] <= fast_ema + tolerance
            or _near(pullback["close"], fast_ema, tolerance)
        )
        pullback_ok = pullback["bearish"] or pullback["close"] <= pullback["open"]
        structure_not_broken = pullback["low"] >= structure["older_low"] - atr * 0.35
        continuation_ok = (
            continuation["bullish"]
            and continuation["close"] > pullback["high"]
            and continuation["close_position"] >= MIN_CONTINUATION_CLOSE
        )
        result["previous_candle_confirmed"] = bool(touched_reference and pullback_ok and structure_not_broken)
        result["extreme_confirmed"] = bool(continuation_ok)
    elif direction == "bearish":
        touched_reference = (
            pullback["high"] >= fast_ema - tolerance
            or _near(pullback["close"], fast_ema, tolerance)
        )
        pullback_ok = pullback["bullish"] or pullback["close"] >= pullback["open"]
        structure_not_broken = pullback["high"] <= structure["older_high"] + atr * 0.35
        continuation_ok = (
            continuation["bearish"]
            and continuation["close"] < pullback["low"]
            and continuation["close_position"] <= (1.0 - MIN_CONTINUATION_CLOSE)
        )
        result["previous_candle_confirmed"] = bool(touched_reference and pullback_ok and structure_not_broken)
        result["extreme_confirmed"] = bool(continuation_ok)

    result["valid"] = result["previous_candle_confirmed"] and result["extreme_confirmed"]
    if not result["valid"]:
        missing = []
        if not result["previous_candle_confirmed"]:
            missing.append("retroceso no confirmado")
        if not result["extreme_confirmed"]:
            missing.append("continuacion no confirmada")
        result["reason"] = ", ".join(missing)
    else:
        result["reason"] = "retroceso validado y ruptura de continuacion confirmada"
    return result


def _location_filter(
    direction: str,
    continuation: Dict[str, Any],
    structure: Dict[str, Any],
    atr: float,
) -> Tuple[bool, float, str]:
    if atr <= 0:
        return False, 0.0, "ATR invalido"

    close = continuation["close"]
    if direction == "bullish":
        room = float(structure["resistance"] or close) - close
    else:
        room = close - float(structure["support"] or close)
    room_atr = room / atr
    if room_atr < MIN_ROOM_ATR:
        return False, room_atr, "entrada demasiado cerca del nivel opuesto"

    distance_from_ema = abs(close - float(structure["fast_ema"])) / atr
    if distance_from_ema > PULLBACK_MAX_ATR:
        return False, room_atr, "entrada extendida lejos de la EMA rapida"
    return True, room_atr, "ubicacion con espacio suficiente"


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
        records = _as_records(df)
    else:
        records = _as_records(candle_1m)

    records = _clean_records(records)
    result = _empty_result()
    if len(records) < MIN_CANDLES:
        result["reason"] = "insufficient_candles"
        return result

    atr = _atr(records)
    if atr <= 0:
        result["reason"] = "invalid_atr"
        return result

    # Las dos ultimas velas tienen roles diferentes: N-2 retroceso y N-1 continuidad.
    pullback = _metrics(records[-2])
    continuation = _metrics(records[-1])
    context_records = records[:-2]
    structure = _structure(context_records, lookback)
    direction = structure["direction"]

    pullback_data = _pullback_and_continuation(direction, pullback, continuation, structure, atr)
    location_ok, room_atr, location_reason = _location_filter(direction, continuation, structure, atr)

    score = 0
    reasons: List[str] = []
    if direction in {"bullish", "bearish"}:
        score += 30
        reasons.append(f"estructura {direction} con EMA alineadas")
    if pullback_data["previous_candle_confirmed"]:
        score += 25
        reasons.append("pullback validado sobre la zona de continuidad")
    if pullback_data["extreme_confirmed"]:
        score += 25
        reasons.append("vela N-1 rompe el extremo del pullback")
    if continuation["body"] >= atr * MIN_BODY_ATR:
        score += 10
        reasons.append("cuerpo de continuacion suficiente")
    if location_ok:
        score += 10
        reasons.append(f"espacio disponible {room_atr:.2f} ATR")
    else:
        reasons.append(location_reason)

    signal = "call" if direction == "bullish" else "put" if direction == "bearish" else None
    aligned = (
        signal == "call" and continuation["close"] > structure["slow_ema"]
    ) or (
        signal == "put" and continuation["close"] < structure["slow_ema"]
    )
    if signal and not aligned:
        score = max(0, score - 20)
        reasons.append("cierre no alineado con EMA lenta")

    force = bool(
        signal
        and aligned
        and pullback_data["valid"]
        and location_ok
        and score >= int(min_score)
    )
    reason = "; ".join(reasons) if reasons else "estructura lateral o sin confirmacion"
    if not force:
        reason = f"senal descartada: {reason}"

    analysis = {
        "force": force,
        "structure": direction,
        "structure_direction": direction,
        "atr": atr,
        "support": structure["support"],
        "resistance": structure["resistance"],
        "tolerance": atr * 0.35,
        "entry_quality": score if force else 0,
        "fast_ema": structure["fast_ema"],
        "slow_ema": structure["slow_ema"],
        "room_atr": room_atr,
        "location_ok": location_ok,
        "impulse_phase": "pullback_continuation" if pullback_data["valid"] else "waiting_confirmation",
        "rejection_timestamp": pullback.get("timestamp"),
        "confirmation_timestamp": continuation.get("timestamp"),
        "rejection_candle": pullback,
        "confirmation_candle": continuation,
        "pullback_candle": pullback,
        "continuation_candle": continuation,
        "last_swing_high": structure["resistance"],
        "last_swing_low": structure["support"],
        "structure_reasons": reasons,
        "pullback": pullback_data,
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
        "candle_timestamp": continuation.get("timestamp"),
        "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
