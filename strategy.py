"""strategy.py - Rechazo de soporte/resistencia compatible con bot.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75
STOCH_K_PERIOD = 13
STOCH_D_PERIOD = 3
STOCH_SMOOTHING = 3
STOCH_OVERSOLD = 7.0
STOCH_OVERBOUGHT = 98.0


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    open_price = _number(candle.get("open", candle.get("open_price")))
    close_price = _number(candle.get("close", candle.get("close_price")))
    low = _number(candle.get("min", candle.get("low")))
    high = _number(candle.get("max", candle.get("high")))
    return open_price, close_price, low, high


def _as_records(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
            return [dict(item) for item in records]
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        return [value]
    try:
        return [dict(item) for item in value if isinstance(item, dict)]
    except TypeError:
        return []


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    true_ranges: List[float] = []
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


def _zones(
    candles: List[Dict[str, Any]],
    lookback: int = DEFAULT_LOOKBACK,
) -> Tuple[float, float]:
    sample = candles[-max(1, lookback):]
    lows = [_ohlc(candle)[2] for candle in sample]
    highs = [_ohlc(candle)[3] for candle in sample]
    return (min(lows), max(highs)) if lows and highs else (0.0, 0.0)


def _stochastic(candles: List[Dict[str, Any]], k_period: int = STOCH_K_PERIOD,
                d_period: int = STOCH_D_PERIOD, slowing: int = STOCH_SMOOTHING) -> Tuple[float, float]:
    """Calcula Stochastic %K y %D usando únicamente velas cerradas."""
    if len(candles) < max(k_period + d_period + slowing, 5):
        return 50.0, 50.0

    highs = [_ohlc(c)[3] for c in candles]
    lows = [_ohlc(c)[2] for c in candles]
    closes = [_ohlc(c)[1] for c in candles]
    raw_k: List[float] = []
    for i in range(k_period - 1, len(candles)):
        window_high = max(highs[i - k_period + 1:i + 1])
        window_low = min(lows[i - k_period + 1:i + 1])
        span = window_high - window_low
        raw_k.append(50.0 if span <= 0 else 100.0 * (closes[i] - window_low) / span)

    if not raw_k:
        return 50.0, 50.0
    smoothed_k = [sum(raw_k[max(0, i - slowing + 1):i + 1]) /
                  len(raw_k[max(0, i - slowing + 1):i + 1]) for i in range(len(raw_k))]
    k_value = smoothed_k[-1]
    d_window = smoothed_k[-d_period:]
    d_value = sum(d_window) / len(d_window)
    return max(0.0, min(100.0, k_value)), max(0.0, min(100.0, d_value))


def _stochastic_confirms(direction: str, k_value: float, d_value: float) -> bool:
    """Filtro adicional estricto usando los niveles 20/80 del gráfico.

    CALL: Stochastic en sobreventa y %K cruzando/por encima de %D.
    PUT:  Stochastic en sobrecompra y %K cruzando/por debajo de %D.
    """
    if direction == "call":
        return k_value <= STOCH_OVERSOLD and k_value >= d_value
    if direction == "put":
        return k_value >= STOCH_OVERBOUGHT and k_value <= d_value
    return False


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


def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
    stochastic_k: float = 50.0,
    stochastic_d: float = 50.0,
) -> Signal:
    records = _as_records(candles)
    if len(records) < max(MIN_CANDLES, lookback // 2):
        return Signal("none", 0, "insufficient_candles")

    previous = records[:-1]
    candle = records[-1]
    open_price, close_price, low, high = _ohlc(candle)
    body = abs(close_price - open_price)
    candle_range = max(high - low, 1e-12)
    upper_wick = max(high - max(open_price, close_price), 0.0)
    lower_wick = max(min(open_price, close_price) - low, 0.0)
    atr = _atr(previous)

    if atr <= 0:
        return Signal("none", 0, "invalid_atr")

    support, resistance = _zones(previous, lookback)
    tolerance = atr * max(zone_atr_factor, 0.01)
    near_support = low <= support + tolerance and close_price > support
    near_resistance = high >= resistance - tolerance and close_price < resistance

    bullish = (
        near_support
        and lower_wick >= max(body * 1.25, atr * 0.20)
        and close_price > open_price
        and (close_price - low) / candle_range >= 0.60
        and _stochastic_confirms("call", stochastic_k, stochastic_d)
    )
    bearish = (
        near_resistance
        and upper_wick >= max(body * 1.25, atr * 0.20)
        and close_price < open_price
        and (high - close_price) / candle_range >= 0.60
        and _stochastic_confirms("put", stochastic_k, stochastic_d)
    )

    if bullish:
        score = 90
        if lower_wick >= body * 2:
            score += 8
        if close_price > support + tolerance * 0.25:
            score += 5
        if score >= int(min_score):
            return Signal(
                "call",
                min(score, 100),
                "bullish_support_rejection",
                support,
                resistance,
            )

    if bearish:
        score = 90
        if upper_wick >= body * 2:
            score += 8
        if close_price < resistance - tolerance * 0.25:
            score += 5
        if score >= int(min_score):
            return Signal(
                "put",
                min(score, 100),
                "bearish_resistance_rejection",
                support,
                resistance,
            )

    return Signal("none", 0, "no_valid_rejection", support, resistance)


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    **_: Any,
) -> Dict[str, Any]:
    """
    Analiza únicamente una vela cerrada anterior.

    - Cuando bot.py envía previous_m1 + candle_1m, candle_1m ya debe ser N-1.
    - Cuando se recibe df directamente, se excluye la última vela del DataFrame
      porque puede estar activa/en formación; se analiza la vela anterior.
    """
    if previous_m1 is not None:
        records = _as_records(previous_m1)
        if candle_1m is not None:
            current_closed = (
                dict(candle_1m)
                if isinstance(candle_1m, dict)
                else {}
            )
            records.append(current_closed)
    elif df is not None:
        all_records = _as_records(df)
        # No analizar la última vela recibida: puede estar en formación.
        records = all_records[:-1] if len(all_records) > 1 else []
    else:
        all_records = _as_records(candle_1m)
        # Para llamadas directas, también se descarta la última vela.
        records = all_records[:-1] if len(all_records) > 1 else []

    result = _empty_result()
    if len(records) < MIN_CANDLES:
        result["reason"] = "insufficient_candles"
        return result

    stochastic_k, stochastic_d = _stochastic(records)
    signal = analyze_rejection(
        records,
        min_score=min_score,
        stochastic_k=stochastic_k,
        stochastic_d=stochastic_d,
    )
    records_last = records[-1]
    timestamp = records_last.get("from", records_last.get("timestamp"))
    atr = _atr(records[:-1]) if len(records) > 1 else 0.0
    direction = (
        "bullish"
        if signal.action == "call"
        else "bearish"
        if signal.action == "put"
        else "range"
    )
    force = signal.action in {"call", "put"} and signal.score >= int(min_score)
    analysis = {
        "force": force,
        "structure": direction,
        "atr": atr,
        "support": signal.support,
        "resistance": signal.resistance,
        "tolerance": atr * 0.35,
        "entry_quality": signal.score,
        "stochastic_k": round(stochastic_k, 2),
        "stochastic_d": round(stochastic_d, 2),
        "stochastic_oversold": STOCH_OVERSOLD,
        "stochastic_overbought": STOCH_OVERBOUGHT,
        "last_swing_high": signal.resistance,
        "last_swing_low": signal.support,
        "rejection_timestamp": timestamp,
        "confirmation_timestamp": timestamp,
        "rejection_candle": records_last,
        "confirmation_candle": records_last,
        "pullback": {
            "valid": force,
            "previous_candle_confirmed": force,
            "extreme_confirmed": force,
        },
    }
    return {
        "signal": signal.action if force else None,
        "score": signal.score if force else 0,
        "score_100": signal.score if force else 0,
        "entry_quality": signal.score if force else 0,
        "quality": signal.score if force else 0,
        "entry_type": "force" if force else "none",
        "direction": direction,
        "reason": f"{signal.reason} | STOCH K={stochastic_k:.1f} D={stochastic_d:.1f}",
        "analysis": analysis,
        "candle_timestamp": timestamp,
        "pair": pair,
    }


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
