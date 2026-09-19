"""Estrategia M1: doble techo/suelo + rechazo + Stochastic + RSI + Bollinger."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 40
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 90
STOCH_K_PERIOD = 13
STOCH_D_PERIOD = 3
STOCH_SMOOTHING = 3
CALL_K_MAX = 15.0
CALL_D_MAX = 15.0
PUT_K_MIN = 85.0
PUT_D_MIN = 85.0
MIN_STOCH_SEPARATION = 0.5
RSI_PERIOD = 14
RSI_CALL_MAX = 30.0
RSI_PUT_MIN = 70.0
BB_PERIOD = 20
BB_STD = 2.0


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    op = _number(candle.get("open", candle.get("open_price")))
    cl = _number(candle.get("close", candle.get("close_price")))
    lo = _number(candle.get("min", candle.get("low")))
    hi = _number(candle.get("max", candle.get("high")))
    return op, cl, lo, hi


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


def _closes(candles: List[Dict[str, Any]]) -> List[float]:
    return [_ohlc(c)[1] for c in candles]


def _atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        _, prev_close, _, _ = _ohlc(candles[i - 1])
        _, _, low, high = _ohlc(candles[i])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / len(trs[-period:]) if trs else 0.0


def _zones(candles: List[Dict[str, Any]], lookback: int = DEFAULT_LOOKBACK) -> Tuple[float, float]:
    sample = candles[-max(1, lookback):]
    lows = [_ohlc(c)[2] for c in sample]
    highs = [_ohlc(c)[3] for c in sample]
    return (min(lows), max(highs)) if lows and highs else (0.0, 0.0)


def _stochastic_series(candles: List[Dict[str, Any]]) -> Tuple[List[float], List[float]]:
    if len(candles) < STOCH_K_PERIOD:
        return [], []
    highs = [_ohlc(c)[3] for c in candles]
    lows = [_ohlc(c)[2] for c in candles]
    closes = _closes(candles)
    raw: List[float] = []
    for i in range(STOCH_K_PERIOD - 1, len(candles)):
        hi = max(highs[i - STOCH_K_PERIOD + 1:i + 1])
        lo = min(lows[i - STOCH_K_PERIOD + 1:i + 1])
        raw.append(50.0 if hi <= lo else 100.0 * (closes[i] - lo) / (hi - lo))
    smooth: List[float] = []
    for i in range(len(raw)):
        window = raw[max(0, i - STOCH_SMOOTHING + 1):i + 1]
        smooth.append(sum(window) / len(window))
    d_values: List[float] = []
    for i in range(len(smooth)):
        window = smooth[max(0, i - STOCH_D_PERIOD + 1):i + 1]
        d_values.append(sum(window) / len(window))
    return smooth, d_values


def _rsi_series(candles: List[Dict[str, Any]], period: int = RSI_PERIOD) -> List[float]:
    closes = _closes(candles)
    if len(closes) < period + 1:
        return []
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = [50.0] * period
    def value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    result.append(value())
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        result.append(value())
    return result


def _bollinger(candles: List[Dict[str, Any]], period: int = BB_PERIOD, std_mult: float = BB_STD) -> Tuple[float, float, float]:
    closes = _closes(candles)
    if len(closes) < period:
        return 0.0, 0.0, 0.0
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    deviation = math.sqrt(max(variance, 0.0))
    return middle + std_mult * deviation, middle, middle - std_mult * deviation


def _empty_result(reason: str = "no_valid_rejection") -> Dict[str, Any]:
    return {"signal": None, "score": 0, "score_100": 0, "entry_quality": 0, "quality": 0,
            "entry_type": "none", "direction": "range", "reason": reason,
            "analysis": {"force": False, "structure": "range", "atr": 0.0,
                         "support": None, "resistance": None, "tolerance": 0.0,
                         "entry_quality": 0, "pullback": {"valid": False}}}


def analyze_rejection(candles: Any, min_score: int = DEFAULT_MIN_SCORE,
                      lookback: int = DEFAULT_LOOKBACK, **_: Any) -> Signal:
    records = _as_records(candles)
    if len(records) < MIN_CANDLES:
        return Signal("none", 0, "insufficient_candles")
    candle = records[-1]
    previous = records[:-1]
    op, cl, low, high = _ohlc(candle)
    body = abs(cl - op)
    candle_range = max(high - low, 1e-12)
    upper_wick = max(high - max(op, cl), 0.0)
    lower_wick = max(min(op, cl) - low, 0.0)
    atr = _atr(previous)
    support, resistance = _zones(previous, lookback)
    tolerance = max(atr * 0.35, 1e-12)
    upper, middle, lower = _bollinger(records)
    k_series, d_series = _stochastic_series(records)
    rsi_series = _rsi_series(records)
    if not k_series or not d_series or not rsi_series or not atr:
        return Signal("none", 0, "indicators_unavailable", support, resistance)
    k, d = k_series[-1], d_series[-1]
    pk, pd = (k_series[-2], d_series[-2]) if len(k_series) >= 2 else (k, d)
    rsi = rsi_series[-1]
    near_resistance = high >= resistance - tolerance and cl < resistance
    near_support = low <= support + tolerance and cl > support
    put_cross = pk >= pd and k < d
    call_cross = pk <= pd and k > d
    put_stoch = k >= PUT_K_MIN and d >= PUT_D_MIN and abs(k - d) >= MIN_STOCH_SEPARATION and put_cross
    call_stoch = k <= CALL_K_MAX and d <= CALL_D_MAX and abs(k - d) >= MIN_STOCH_SEPARATION and call_cross
    put_bb = upper > 0 and high >= upper and cl < upper
    call_bb = lower > 0 and low <= lower and cl > lower
    put_rejection = near_resistance and upper_wick >= max(body * 1.25, atr * 0.20) and cl < op and (high - cl) / candle_range >= 0.60
    call_rejection = near_support and lower_wick >= max(body * 1.25, atr * 0.20) and cl > op and (cl - low) / candle_range >= 0.60
    double_top = near_resistance and upper_wick >= max(body * 1.25, atr * 0.20)
    double_bottom = near_support and lower_wick >= max(body * 1.25, atr * 0.20)
    if put_rejection and double_top and put_stoch and rsi >= RSI_PUT_MIN and put_bb:
        score = 100 if upper_wick >= body * 2 else 95
        return Signal("put", score, "double_top_rejection_put", support, resistance)
    if call_rejection and double_bottom and call_stoch and rsi <= RSI_CALL_MAX and call_bb:
        score = 100 if lower_wick >= body * 2 else 95
        return Signal("call", score, "double_bottom_rejection_call", support, resistance)
    return Signal("none", 0, "filters_not_confirmed", support, resistance)


def analyze_market(candle_1m: Any = None, previous_m1: Any = None,
                   pair: Optional[str] = None, df: Any = None,
                   min_score: int = DEFAULT_MIN_SCORE, **_: Any) -> Dict[str, Any]:
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
    k_series, d_series = _stochastic_series(records)
    rsi_series = _rsi_series(records)
    k = k_series[-1] if k_series else 50.0
    d = d_series[-1] if d_series else 50.0
    pk = k_series[-2] if len(k_series) >= 2 else k
    pd = d_series[-2] if len(d_series) >= 2 else d
    rsi = rsi_series[-1] if rsi_series else 50.0
    upper, middle, lower = _bollinger(records)
    timestamp = records[-1].get("from", records[-1].get("timestamp"))
    atr = _atr(records[:-1])
    direction = "bullish" if signal.action == "call" else "bearish" if signal.action == "put" else "range"
    force = signal.action in {"call", "put"} and signal.score >= int(min_score)
    analysis = {"force": force, "structure": direction, "atr": atr,
                "support": signal.support, "resistance": signal.resistance,
                "tolerance": atr * 0.35, "entry_quality": signal.score if force else 0,
                "stochastic_k": round(k, 2), "stochastic_d": round(d, 2),
                "stochastic_prev_k": round(pk, 2), "stochastic_prev_d": round(pd, 2),
                "stochastic_cross": "bearish" if pk >= pd and k < d else "bullish" if pk <= pd and k > d else "none",
                "rsi": round(rsi, 2), "bb_upper": upper, "bb_middle": middle, "bb_lower": lower,
                "rejection_timestamp": timestamp, "confirmation_timestamp": timestamp,
                "rejection_candle": records[-1], "confirmation_candle": records[-1],
                "pullback": {"valid": force, "previous_candle_confirmed": force, "extreme_confirmed": force}}
    return {"signal": signal.action if force else None, "score": signal.score if force else 0,
            "score_100": signal.score if force else 0, "entry_quality": signal.score if force else 0,
            "quality": signal.score if force else 0, "entry_type": "force" if force else "none",
            "direction": direction,
            "reason": f"{signal.reason} | STOCH K={k:.1f} D={d:.1f} | RSI={rsi:.1f} | BB={upper:.5f}/{lower:.5f}",
            "analysis": analysis, "candle_timestamp": timestamp, "pair": pair}


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
