"""Estrategia híbrida para opciones binarias.

Prioridad:
1) Rechazo confirmado en soporte/resistencia.
2) Continuidad de alta calidad, únicamente con tendencia, impulso y espacio.

La vela analizada es N (cerrada); la ejecución debe hacerse en N+1.
El score es calidad de señal, no una probabilidad estadística.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple
import numpy as np
import pandas as pd

MIN_BARS = 40
MAX_CANDLES = 150
ATR_PERIOD = 14
BODY_PERIOD = 10
SWING_LOOKBACK = 20
ZONE_ATR_TOLERANCE = 0.28
MIN_WICK_RATIO = 0.30
MIN_CLOSE_POSITION = 0.64
MIN_ROOM_ATR = 0.85
MAX_BODY_ATR = 1.45
MIN_SCORE = 78
MAX_CONTINUATION_SCORE = 88
EPS = 1e-10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.rename(columns={"max": "high", "min": "low", "timestamp": "from"}, inplace=True)
    required = ["open", "high", "low", "close"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()
    for c in required:
        out[c] = pd.to_numeric(out[c], errors="coerce")
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
    return _normalize(pd.concat([history, current_df], ignore_index=True)) if not current_df.empty else history


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = (_safe_float(row.get(k)) for k in ("open", "high", "low", "close"))
    rng = max(h - l, EPS)
    body = abs(c - o)
    return {
        "open": o, "high": h, "low": l, "close": c, "range": rng, "body": body,
        "upper": max(h - max(o, c), 0.0), "lower": max(min(o, c) - l, 0.0),
        "body_ratio": body / rng, "close_position": (c - l) / rng,
    }


def _bull(m: Dict[str, float]) -> bool:
    return m["close"] > m["open"]


def _bear(m: Dict[str, float]) -> bool:
    return m["close"] < m["open"]


def _trend(data: pd.DataFrame, index: int, side: str) -> int:
    start = max(0, index - 6)
    closes = data["close"].iloc[start:index]
    if len(closes) < 4:
        return 0
    delta = float(closes.iloc[-1] - closes.iloc[0])
    atr = _safe_float(data["atr"].iloc[index], 0.0)
    if atr <= 0:
        return 0
    aligned = delta > atr * 0.20 if side == "call" else delta < -atr * 0.20
    return 12 if aligned else 0


def _rejection(m: Dict[str, float], side: str, zone: float, atr: float) -> bool:
    tol = max(atr * ZONE_ATR_TOLERANCE, m["range"] * 0.18)
    if side == "call":
        return (abs(m["low"] - zone) <= tol and m["lower"] / m["range"] >= MIN_WICK_RATIO
                and m["close_position"] >= MIN_CLOSE_POSITION and _bull(m))
    return (abs(m["high"] - zone) <= tol and m["upper"] / m["range"] >= MIN_WICK_RATIO
            and m["close_position"] <= 1.0 - MIN_CLOSE_POSITION and _bear(m))


def _continuation(data: pd.DataFrame, i: int, side: str) -> bool:
    if i < 3:
        return False
    cur, prev, prev2 = (_metrics(data.iloc[j]) for j in (i, i - 1, i - 2))
    avg = _safe_float(data["avg_body"].iloc[i], 0.0)
    atr = _safe_float(data["atr"].iloc[i], 0.0)
    if avg <= 0 or atr <= 0 or cur["body"] < avg * 1.10 or cur["body"] > atr * MAX_BODY_ATR:
        return False
    if side == "call":
        return (_bull(cur) and _bull(prev) and prev2["close"] <= prev["close"]
                and cur["close_position"] >= 0.70 and cur["upper"] <= cur["body"] * 0.60)
    return (_bear(cur) and _bear(prev) and prev2["close"] >= prev["close"]
            and cur["close_position"] <= 0.30 and cur["lower"] <= cur["body"] * 0.60)


def _score_rejection(m: Dict[str, float], trend: int, room: float) -> int:
    score = 62 + trend
    wick = max(m["lower"], m["upper"]) / m["range"]
    score += 10 if wick >= 0.45 else 5
    score += 8 if m["body_ratio"] >= 0.35 else 3
    score += 8 if room >= 1.30 else 0
    return min(score, 100)


def _score_continuation(m: Dict[str, float], trend: int, room: float) -> int:
    score = 55 + trend
    score += 10 if m["body_ratio"] >= 0.55 else 4
    score += 8 if m["close_position"] >= 0.75 or m["close_position"] <= 0.25 else 0
    score += 8 if room >= 1.30 else 0
    return min(score, MAX_CONTINUATION_SCORE)


def _empty(reason: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"signal": None, "direction": "range", "score": 0, "entry_quality": 0,
            "entry_type": None, "blocked": True, "reason": reason, "analysis": analysis or {}}


def analyze_market(df: Optional[pd.DataFrame] = None, candle_1m: Any = None,
                   candles_5s: Optional[pd.DataFrame] = None,
                   previous_m1: Optional[pd.DataFrame] = None,
                   pair: Optional[str] = None) -> Dict[str, Any]:
    data = _build_dataframe(candle_1m, previous_m1, df)
    if len(data) < MIN_BARS:
        return _empty(f"Historial insuficiente {len(data)}/{MIN_BARS}")
    data = data.copy()
    data["body"] = (data["close"] - data["open"]).abs()
    prev_close = data["close"].shift(1)
    tr = pd.concat([(data["high"] - data["low"]), (data["high"] - prev_close).abs(),
                    (data["low"] - prev_close).abs()], axis=1).max(axis=1)
    data["atr"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    data["avg_body"] = data["body"].rolling(BODY_PERIOD, min_periods=BODY_PERIOD).mean()
    i = len(data) - 1
    if not math.isfinite(_safe_float(data["atr"].iloc[i], np.nan)):
        return _empty("ATR insuficiente")
    cur = _metrics(data.iloc[i])
    atr = _safe_float(data["atr"].iloc[i])
    lookback = data.iloc[max(0, i - SWING_LOOKBACK):i]
    if len(lookback) < 8:
        return _empty("Estructura insuficiente")
    support, resistance = float(lookback["low"].min()), float(lookback["high"].max())
    call_room = (resistance - cur["close"]) / atr if atr else 0.0
    put_room = (cur["close"] - support) / atr if atr else 0.0
    not_extended = cur["body"] / atr <= MAX_BODY_ATR if atr else False
    call_trend, put_trend = _trend(data, i, "call"), _trend(data, i, "put")
    call_rej = _rejection(cur, "call", support, atr)
    put_rej = _rejection(cur, "put", resistance, atr)
    call_cont, put_cont = _continuation(data, i, "call"), _continuation(data, i, "put")
    analysis = {"pair": pair, "pattern": "rejection_primary_continuation_filtered",
                "execution_mode": "next_candle", "expiration_minutes": 1, "atr": atr,
                "support": support, "resistance": resistance, "candle_n": cur,
                "call_rejection": call_rej, "put_rejection": put_rej,
                "call_continuation": call_cont, "put_continuation": put_cont,
                "call_room_atr": call_room, "put_room_atr": put_room,
                "structure": "bullish" if call_trend else "bearish" if put_trend else "mixed"}
    candidates = []
    if call_rej and call_room >= MIN_ROOM_ATR and not_extended:
        candidates.append((_score_rejection(cur, call_trend, call_room), "call", "rejection",
                           "CALL: rechazo confirmado en soporte + espacio"))
    if put_rej and put_room >= MIN_ROOM_ATR and not_extended:
        candidates.append((_score_rejection(cur, put_trend, put_room), "put", "rejection",
                           "PUT: rechazo confirmado en resistencia + espacio"))
    if call_cont and call_trend >= 12 and call_room >= MIN_ROOM_ATR and not_extended:
        candidates.append((_score_continuation(cur, call_trend, call_room), "call", "continuation",
                           "CALL: continuidad filtrada + tendencia + espacio"))
    if put_cont and put_trend >= 12 and put_room >= MIN_ROOM_ATR and not_extended:
        candidates.append((_score_continuation(cur, put_trend, put_room), "put", "continuation",
                           "PUT: continuidad filtrada + tendencia + espacio"))
    candidates = [x for x in candidates if x[0] >= MIN_SCORE]
    if not candidates:
        return _empty("Sin patrón de alta calidad", analysis)
    score, signal, entry_type, reason = max(candidates, key=lambda x: x[0])
    analysis["force"] = True
    analysis["entry_quality"] = score
    return {"signal": signal, "direction": "bullish" if signal == "call" else "bearish",
            "score": score, "entry_quality": score, "entry_type": entry_type, "blocked": False,
            "reason": reason + " | N cerrada / ejecución N+1", "signal_price": cur["close"],
            "candle_timestamp": int(data.iloc[i]["from"]) if "from" in data.columns else None,
            "analysis": analysis}


def pro_signal(df: pd.DataFrame, aggressive: bool = False) -> Tuple[Optional[str], Optional[str], int]:
    result = analyze_market(df=df)
    return result.get("signal"), result.get("entry_type"), int(result.get("score", 0))


def update_result(result: Any) -> None:
    # Compatibilidad con bot.py. El registro persistente debe gestionarse fuera de la estrategia.
    print(f"Resultado de operación: {result}")


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
