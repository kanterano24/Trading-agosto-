"""Estrategia híbrida: rechazo estructural + continuidad filtrada.

Contrato público compatible con bot.py:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)

Se analiza únicamente la vela N cerrada y el bot ejecuta en N+1.
El score es una puntuación de calidad de señal, NO una probabilidad.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

MIN_BARS = 35
MAX_CANDLES = 120
ATR_PERIOD = 14
FORCE_PERIOD = 10
SWING_LOOKBACK = 12
ZONE_ATR_TOLERANCE = 0.35
MIN_REJECTION_WICK_RATIO = 0.28
MIN_CLOSE_POSITION = 0.62
MIN_ROOM_ATR = 0.90
MAX_SIGNAL_BODY_ATR = 1.35
MIN_SCORE = 76
EPS = 1e-10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.rename(columns={"max": "high", "min": "low", "timestamp": "from"}, inplace=True)
    required = ["open", "high", "low", "close"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out["from"] = out["from"].astype(int)
        out.sort_values("from", inplace=True)
        out.drop_duplicates("from", keep="last", inplace=True)
    out.dropna(subset=required, inplace=True)
    return out.reset_index(drop=True).tail(MAX_CANDLES).reset_index(drop=True)


def _build_dataframe(candle_1m: Any, previous_m1: Optional[pd.DataFrame], df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is not None:
        return _normalize(df)
    history = _normalize(previous_m1)
    if isinstance(candle_1m, pd.Series):
        current = candle_1m.to_dict()
    elif isinstance(candle_1m, dict):
        current = dict(candle_1m)
    else:
        return history
    current_df = _normalize(pd.DataFrame([current]))
    if current_df.empty:
        return history
    return _normalize(pd.concat([history, current_df], ignore_index=True))


def _true_range(data: pd.DataFrame) -> pd.Series:
    previous_close = data["close"].shift(1)
    return pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ], axis=1,
    ).max(axis=1)


def _metrics(row: pd.Series) -> Dict[str, float]:
    o = _safe_float(row.get("open"))
    h = _safe_float(row.get("high"))
    l = _safe_float(row.get("low"))
    c = _safe_float(row.get("close"))
    rng = max(h - l, EPS)
    body = abs(c - o)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "upper": max(h - max(o, c), 0.0),
        "lower": max(min(o, c) - l, 0.0),
        "close_position": (c - l) / rng,
        "body_ratio": body / rng,
    }


def _is_bullish(m: Dict[str, float]) -> bool:
    return m["close"] > m["open"]


def _is_bearish(m: Dict[str, float]) -> bool:
    return m["close"] < m["open"]


def _near(value: float, level: float, tolerance: float) -> bool:
    return abs(value - level) <= max(tolerance, EPS)


def _rejection(metrics: Dict[str, float], side: str, zone: float, atr: float) -> bool:
    tolerance = max(atr * ZONE_ATR_TOLERANCE, metrics["range"] * 0.20)
    if side == "call":
        return (
            _near(metrics["low"], zone, tolerance)
            and metrics["lower"] / metrics["range"] >= MIN_REJECTION_WICK_RATIO
            and metrics["close_position"] >= MIN_CLOSE_POSITION
            and _is_bullish(metrics)
        )
    return (
        _near(metrics["high"], zone, tolerance)
        and metrics["upper"] / metrics["range"] >= MIN_REJECTION_WICK_RATIO
        and metrics["close_position"] <= 1.0 - MIN_CLOSE_POSITION
        and _is_bearish(metrics)
    )


def _trend_score(data: pd.DataFrame, index: int, side: str) -> int:
    start = max(0, index - 5)
    closes = data["close"].iloc[start:index]
    if len(closes) < 3:
        return 0
    rising = closes.iloc[-1] > closes.iloc[0]
    falling = closes.iloc[-1] < closes.iloc[0]
    if side == "call":
        return 10 if rising else 0
    return 10 if falling else 0


def _continuation(data: pd.DataFrame, index: int, side: str, average_body: pd.Series) -> bool:
    if index <= 1 or index >= len(data):
        return False
    current = _metrics(data.iloc[index])
    previous = _metrics(data.iloc[index - 1])
    avg = _safe_float(average_body.iloc[index], 0.0)
    if avg <= 0.0:
        return False
    strong = current["body"] >= avg * 1.05
    controlled_wick = (
        current["upper"] <= max(current["body"] * 0.65, EPS)
        if side == "call"
        else current["lower"] <= max(current["body"] * 0.65, EPS)
    )
    if side == "call":
        return _is_bullish(current) and _is_bullish(previous) and strong and controlled_wick
    return _is_bearish(current) and _is_bearish(previous) and strong and controlled_wick


def _score_rejection(metrics: Dict[str, float], side: str, trend: int, room_atr: float) -> int:
    score = 58 + trend
    wick = metrics["lower"] if side == "call" else metrics["upper"]
    score += 10 if wick / metrics["range"] >= 0.40 else 5
    score += 10 if metrics["body_ratio"] >= 0.35 else 4
    score += 8 if room_atr >= 1.30 else 0
    return min(score, 100)


def _score_continuation(metrics: Dict[str, float], trend: int, room_atr: float) -> int:
    score = 54 + trend
    score += 10 if metrics["body_ratio"] >= 0.55 else 5
    score += 8 if metrics["close_position"] >= 0.70 or metrics["close_position"] <= 0.30 else 0
    score += 8 if room_atr >= 1.30 else 0
    return min(score, 100)


def _empty(reason: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "score": 0,
        "entry_quality": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason,
        "analysis": analysis or {},
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    data = _build_dataframe(candle_1m, previous_m1, df)
    if len(data) < MIN_BARS:
        return _empty(f"Historial insuficiente {len(data)}/{MIN_BARS}")

    data = data.copy()
    data["body"] = (data["close"] - data["open"]).abs()
    data["tr"] = _true_range(data)
    data["atr"] = data["tr"].rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    data["avg_body"] = data["body"].rolling(FORCE_PERIOD, min_periods=FORCE_PERIOD).mean()

    c3_i = len(data) - 1
    c2_i = len(data) - 2
    if not math.isfinite(_safe_float(data.iloc[c3_i]["atr"], np.nan)):
        return _empty("ATR insuficiente")

    c2 = _metrics(data.iloc[c2_i])
    c3 = _metrics(data.iloc[c3_i])
    atr = _safe_float(data.iloc[c3_i]["atr"], 0.0)
    average_body = data["avg_body"]

    lookback = data.iloc[max(0, c3_i - SWING_LOOKBACK - 1):c3_i - 1]
    if len(lookback) < 5:
        return _empty("Estructura insuficiente")
    support = float(lookback["low"].min())
    resistance = float(lookback["high"].max())

    call_rejection = _rejection(c2, "call", support, atr) or _rejection(c3, "call", support, atr)
    put_rejection = _rejection(c2, "put", resistance, atr) or _rejection(c3, "put", resistance, atr)
    call_continuation = _continuation(data, c3_i, "call", average_body)
    put_continuation = _continuation(data, c3_i, "put", average_body)

    call_room = (resistance - c3["close"]) / atr if atr > 0 else 0.0
    put_room = (c3["close"] - support) / atr if atr > 0 else 0.0
    not_extended = c3["body"] / atr <= MAX_SIGNAL_BODY_ATR if atr > 0 else False
    call_trend = _trend_score(data, c3_i, "call")
    put_trend = _trend_score(data, c3_i, "put")

    analysis: Dict[str, Any] = {
        "pair": pair,
        "force": False,
        "pattern": "rejection_or_continuation",
        "execution_mode": "next_candle",
        "expiration_minutes": 3,
        "atr": atr,
        "last_swing_high": resistance,
        "last_swing_low": support,
        "candle_2": c2,
        "candle_3": c3,
        "call_rejection": call_rejection,
        "put_rejection": put_rejection,
        "call_continuation": call_continuation,
        "put_continuation": put_continuation,
        "call_room_atr": call_room,
        "put_room_atr": put_room,
        "body_atr_ratio": c3["body"] / atr if atr > 0 else math.inf,
        "structure": "bullish" if call_trend else "bearish" if put_trend else "mixed",
    }

    candidates = []
    if call_rejection and call_room >= MIN_ROOM_ATR and not_extended:
        score = _score_rejection(c3, "call", call_trend, call_room)
        candidates.append((score, "call", "rejection", "CALL: rechazo confirmado cerca de soporte + espacio"))
    if put_rejection and put_room >= MIN_ROOM_ATR and not_extended:
        score = _score_rejection(c3, "put", put_trend, put_room)
        candidates.append((score, "put", "rejection", "PUT: rechazo confirmado cerca de resistencia + espacio"))

    # Continuidad permitida, pero solo si existe tendencia, vela controlada y espacio.
    if call_continuation and call_trend >= 10 and call_room >= MIN_ROOM_ATR and not_extended:
        score = _score_continuation(c3, call_trend, call_room)
        candidates.append((score, "call", "continuation", "CALL: continuidad filtrada + tendencia + espacio"))
    if put_continuation and put_trend >= 10 and put_room >= MIN_ROOM_ATR and not_extended:
        score = _score_continuation(c3, put_trend, put_room)
        candidates.append((score, "put", "continuation", "PUT: continuidad filtrada + tendencia + espacio"))

    candidates = [candidate for candidate in candidates if candidate[0] >= MIN_SCORE]
    if not candidates:
        return _empty("Sin rechazo o continuidad de calidad", analysis)

    score, signal, entry_type, reason = max(candidates, key=lambda item: item[0])
    analysis["force"] = True
    analysis["entry_quality"] = score
    analysis["pullback"] = {
        "valid": True,
        "previous_candle_confirmed": True,
        "extreme_confirmed": entry_type == "rejection",
    }

    return {
        "signal": signal,
        "direction": "bullish" if signal == "call" else "bearish",
        "score": score,
        "entry_quality": score,
        "entry_type": entry_type,
        "blocked": False,
        "reason": reason + " | N cerrada / ejecución N+1",
        "signal_price": c3["close"],
        "candle_timestamp": int(data.iloc[c3_i]["from"]) if "from" in data.columns else None,
        "analysis": analysis,
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente: rechazo estructural + continuidad filtrada")
