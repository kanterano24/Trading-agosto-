"""strategy.py - Rechazo S/R + confirmación N-1 + ejecución en N.

Compatible con bot.py:
- analyze_market(candle_1m=..., previous_m1=..., pair=...)
- Devuelve únicamente señales force con score >= min_score.
- La lógica busca:
    N-2: rechazo de soporte/resistencia.
    N-1: confirmación de la dirección.
    N: el bot ejecuta la señal seleccionada.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 90

STOCH_K_PERIOD = 13
STOCH_D_PERIOD = 3
STOCH_SMOOTHING = 3
STOCH_EXPANSION_LOOKBACK = 5


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None
    analysis: Optional[Dict[str, Any]] = None


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
            return [dict(x) for x in value.to_dict("records")]
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        return [dict(value)]
    try:
        return [dict(x) for x in value if isinstance(x, dict)]
    except TypeError:
        return []


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        _, _, low, high = _ohlc(candles[i])
        _, prev_close, _, _ = _ohlc(candles[i - 1])
        trs.append(max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        ))
    values = trs[-period:]
    return sum(values) / len(values) if values else 0.0


def _zones(
    candles: List[Dict[str, Any]],
    lookback: int = DEFAULT_LOOKBACK,
) -> Tuple[float, float]:
    if not candles:
        return 0.0, 0.0
    sample = candles[-max(1, lookback):]
    lows = [_ohlc(c)[2] for c in sample]
    highs = [_ohlc(c)[3] for c in sample]
    return (min(lows), max(highs)) if lows and highs else (0.0, 0.0)


def _candle_info(candle: Dict[str, Any]) -> Dict[str, Any]:
    o, c, low, high = _ohlc(candle)
    rng = max(high - low, 1e-12)
    body = abs(c - o)
    upper = max(high - max(o, c), 0.0)
    lower = max(min(o, c) - low, 0.0)
    close_position = (c - low) / rng
    return {
        "open": o,
        "high": high,
        "low": low,
        "close": c,
        "range": rng,
        "body": body,
        "upper": upper,
        "lower": lower,
        "close_position": close_position,
        "bullish": c > o,
        "bearish": c < o,
    }


def _stochastic_series(
    candles: List[Dict[str, Any]],
    k_period: int = STOCH_K_PERIOD,
    d_period: int = STOCH_D_PERIOD,
    slowing: int = STOCH_SMOOTHING,
) -> Tuple[List[float], List[float]]:
    if len(candles) < max(k_period + d_period + slowing, 5):
        return [], []

    highs = [_ohlc(c)[3] for c in candles]
    lows = [_ohlc(c)[2] for c in candles]
    closes = [_ohlc(c)[1] for c in candles]

    raw_k: List[float] = []
    for i in range(k_period - 1, len(candles)):
        wh = max(highs[i-k_period+1:i+1])
        wl = min(lows[i-k_period+1:i+1])
        span = wh - wl
        raw_k.append(50.0 if span <= 0 else 100.0 * (closes[i] - wl) / span)

    smoothed: List[float] = []
    for i in range(len(raw_k)):
        window = raw_k[max(0, i-slowing+1):i+1]
        smoothed.append(sum(window) / len(window))

    d_values: List[float] = []
    for i in range(len(smoothed)):
        window = smoothed[max(0, i-d_period+1):i+1]
        d_values.append(sum(window) / len(window))

    return (
        [max(0.0, min(100.0, x)) for x in smoothed],
        [max(0.0, min(100.0, x)) for x in d_values],
    )


def _stochastic_expansion(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    k_values, d_values = _stochastic_series(candles)
    minimum = STOCH_EXPANSION_LOOKBACK + 2

    if len(k_values) < minimum:
        return {
            "valid": False, "direction": None, "k": 50.0, "d": 50.0,
            "separation": 0.0, "previous_separation": 0.0,
            "maximum_separation": 0.0, "k_change": 0.0,
            "d_change": 0.0, "expanding": False, "maximum": False,
        }

    k, d = k_values[-1], d_values[-1]
    pk, pd = k_values[-2], d_values[-2]
    sep = abs(k - d)
    prev_sep = abs(pk - pd)
    start = max(0, len(k_values) - STOCH_EXPANSION_LOOKBACK)
    recent = [abs(k_values[i] - d_values[i]) for i in range(start, len(k_values))]
    maximum_sep = max(recent)
    k_change = k - pk
    d_change = d - pd
    expanding = sep > prev_sep
    maximum = sep >= maximum_sep - 1e-6

    bullish = k > d and k_change > 0 and expanding and maximum
    bearish = k < d and k_change < 0 and expanding and maximum
    direction = "call" if bullish else "put" if bearish else None

    return {
        "valid": direction is not None,
        "direction": direction,
        "k": k,
        "d": d,
        "separation": sep,
        "previous_separation": prev_sep,
        "maximum_separation": maximum_sep,
        "k_change": k_change,
        "d_change": d_change,
        "expanding": expanding,
        "maximum": maximum,
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
            "pullback": {
                "valid": False,
                "previous_candle_confirmed": False,
                "extreme_confirmed": False,
            },
        },
    }


def _build_signal(
    candles: List[Dict[str, Any]],
    min_score: int,
    lookback: int,
    zone_atr_factor: float,
) -> Signal:
    if len(candles) < max(MIN_CANDLES, lookback // 2):
        return Signal("none", 0, "insufficient_candles")

    # La última vela recibida representa N-1 para la preparación de la entrada.
    # La vela anterior representa N-2 y debe ser el rechazo.
    n2 = _candle_info(candles[-2])
    n1 = _candle_info(candles[-1])

    context = candles[:-2]
    if len(context) < 10:
        return Signal("none", 0, "insufficient_context")

    atr = _atr(candles[:-1])
    if atr <= 0:
        return Signal("none", 0, "invalid_atr")

    support, resistance = _zones(context, lookback)
    tolerance = atr * max(zone_atr_factor, 0.01)

    # Rechazo N-2.
    call_touch = (
        n2["low"] <= support + tolerance
        and n2["close"] > support
        and n2["lower"] >= max(n2["body"] * 1.25, atr * 0.20)
        and n2["close_position"] >= 0.60
    )
    put_touch = (
        n2["high"] >= resistance - tolerance
        and n2["close"] < resistance
        and n2["upper"] >= max(n2["body"] * 1.25, atr * 0.20)
        and (n2["high"] - n2["close"]) / n2["range"] >= 0.60
    )

    # Confirmación N-1: debe avanzar en la dirección del rechazo.
    bullish_confirmation = (
        n1["bullish"]
        and n1["close"] > n2["close"]
        and n1["close"] > (n1["open"] + n1["close"]) / 2.0
    )
    bearish_confirmation = (
        n1["bearish"]
        and n1["close"] < n2["close"]
        and n1["close"] < (n1["open"] + n1["close"]) / 2.0
    )

    # Evita confirmar una vela que ya haya recorrido demasiado.
    confirmation_room_ok_call = n1["close"] < resistance - tolerance * 0.10
    confirmation_room_ok_put = n1["close"] > support + tolerance * 0.10

    stoch = _stochastic_expansion(candles)
    stoch_call = stoch["valid"] and stoch["direction"] == "call"
    stoch_put = stoch["valid"] and stoch["direction"] == "put"

    call = (
        call_touch
        and bullish_confirmation
        and confirmation_room_ok_call
        and stoch_call
    )
    put = (
        put_touch
        and bearish_confirmation
        and confirmation_room_ok_put
        and stoch_put
    )

    if not call and not put:
        return Signal(
            "none",
            0,
            "no_valid_n2_rejection_n1_confirmation",
            support,
            resistance,
        )

    if call:
        score = 90
        reasons = [
            "rechazo_soporte_N-2",
            "confirmacion_alcista_N-1",
            "stochastic_expansion_call",
        ]
        if n2["lower"] >= n2["body"] * 2:
            score += 3
            reasons.append("mecha_inferior_fuerte")
        if n1["close"] > n2["high"]:
            score += 3
            reasons.append("confirmacion_supera_maximo_N-2")
        if stoch["separation"] >= 5:
            score += 2
        if stoch["separation"] >= 10:
            score += 2

        return Signal(
            "call",
            min(score, 100),
            "|".join(reasons),
            support,
            resistance,
            {
                "rejection_candle": n2,
                "confirmation_candle": n1,
                "atr": atr,
                "stoch": stoch,
            },
        )

    score = 90
    reasons = [
        "rechazo_resistencia_N-2",
        "confirmacion_bajista_N-1",
        "stochastic_expansion_put",
    ]
    if n2["upper"] >= n2["body"] * 2:
        score += 3
        reasons.append("mecha_superior_fuerte")
    if n1["close"] < n2["low"]:
        score += 3
        reasons.append("confirmacion_rompe_minimo_N-2")
    if stoch["separation"] >= 5:
        score += 2
    if stoch["separation"] >= 10:
        score += 2

    return Signal(
        "put",
        min(score, 100),
        "|".join(reasons),
        support,
        resistance,
        {
            "rejection_candle": n2,
            "confirmation_candle": n1,
            "atr": atr,
            "stoch": stoch,
        },
    )


def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
    stochastic_k: float = 50.0,
    stochastic_d: float = 50.0,
) -> Signal:
    records = _as_records(candles)
    return _build_signal(records, int(min_score), int(lookback), float(zone_atr_factor))


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

    if len(records) < MIN_CANDLES:
        result["reason"] = "insufficient_candles"
        return result

    signal = analyze_rejection(records, min_score=min_score)
    stoch = _stochastic_expansion(records)

    # ATR y zonas para diagnóstico.
    atr = _atr(records[:-1]) if len(records) > 1 else 0.0
    support, resistance = _zones(records[:-2], DEFAULT_LOOKBACK) if len(records) > 2 else (None, None)

    direction = (
        "bullish" if signal.action == "call"
        else "bearish" if signal.action == "put"
        else "range"
    )

    force = signal.action in {"call", "put"} and signal.score >= int(min_score)

    if signal.analysis:
        rejection_candle = signal.analysis.get("rejection_candle")
        confirmation_candle = signal.analysis.get("confirmation_candle")
        atr = float(signal.analysis.get("atr") or atr)
    else:
        rejection_candle = records[-2] if len(records) >= 2 else {}
        confirmation_candle = records[-1] if records else {}

    rejection_ts = rejection_candle.get("timestamp", rejection_candle.get("from")) if isinstance(rejection_candle, dict) else None
    confirmation_ts = confirmation_candle.get("timestamp", confirmation_candle.get("from")) if isinstance(confirmation_candle, dict) else None

    if signal.support is not None:
        support = signal.support
    if signal.resistance is not None:
        resistance = signal.resistance

    analysis = {
        "force": force,
        "structure": direction,
        "atr": atr,
        "support": support,
        "resistance": resistance,
        "tolerance": atr * 0.35,
        "entry_quality": signal.score if force else 0,
        "impulse_phase": "confirmed_reversal" if force else "waiting",
        "fast_ema": None,
        "slow_ema": None,
        "bullish_context": direction == "bullish",
        "bearish_context": direction == "bearish",

        "stochastic_k": round(stoch.get("k", 50.0), 2),
        "stochastic_d": round(stoch.get("d", 50.0), 2),
        "stochastic_separation": round(stoch.get("separation", 0.0), 2),
        "stochastic_previous_separation": round(stoch.get("previous_separation", 0.0), 2),
        "stochastic_maximum_separation": round(stoch.get("maximum_separation", 0.0), 2),
        "stochastic_k_change": round(stoch.get("k_change", 0.0), 2),
        "stochastic_d_change": round(stoch.get("d_change", 0.0), 2),
        "stochastic_expanding": bool(stoch.get("expanding", False)),
        "stochastic_maximum": bool(stoch.get("maximum", False)),
        "stochastic_direction": stoch.get("direction"),

        "last_swing_high": resistance,
        "last_swing_low": support,

        "rejection_timestamp": rejection_ts,
        "confirmation_timestamp": confirmation_ts,

        "rejection_candle": rejection_candle,
        "confirmation_candle": confirmation_candle,

        "pullback": {
            "valid": force,
            "previous_candle_confirmed": force,
            "extreme_confirmed": force,
        },
    }

    timestamp = confirmation_ts
    reason = (
        f"{signal.reason} | "
        f"STOCH K={stoch.get('k', 50.0):.1f} "
        f"D={stoch.get('d', 50.0):.1f} | "
        f"SEP={stoch.get('separation', 0.0):.2f}"
    )

    return {
        "signal": signal.action if force else None,
        "score": signal.score if force else 0,
        "score_100": signal.score if force else 0,
        "entry_quality": signal.score if force else 0,
        "quality": signal.score if force else 0,
        "entry_type": "force" if force else "none",
        "direction": direction,
        "reason": reason,
        "analysis": analysis,
        "candle_timestamp": timestamp,
        "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
