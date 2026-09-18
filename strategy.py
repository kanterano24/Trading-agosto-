"""Estrategia CONTINUIDAD FILTRADA + RECHAZO + ESPACIO.

Interfaz compatible con el bot:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)

La estrategia solo analiza la vela N cerrada. El bot ejecuta exclusivamente
la señal preparada en N durante la apertura de N+1.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

MIN_BARS = 35
MAX_CANDLES = 120
FORCE_PERIOD = 10
FORCE_PERCENT = 120.0
WICK_PERCENT = 50.0
MIN_REJECTION_WICK_RATIO = 0.35
MIN_CLOSE_POSITION = 0.70
MIN_ROOM_ATR = 0.90
SWING_LOOKBACK = 8
MAX_SIGNAL_BODY_ATR = 1.35
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
    prev_close = data["close"].shift(1)
    return pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - prev_close).abs(),
            (data["low"] - prev_close).abs(),
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


def _continuation(data: pd.DataFrame, index: int, direction: str, average_body: pd.Series) -> bool:
    if index <= 0 or index >= len(data):
        return False
    current = _metrics(data.iloc[index])
    previous = _metrics(data.iloc[index - 1])
    avg = _safe_float(average_body.iloc[index], 0.0)
    if avg <= 0.0:
        return False
    strong = current["body"] >= avg * FORCE_PERCENT / 100.0
    controlled_upper = current["upper"] <= current["body"] * WICK_PERCENT / 100.0
    controlled_lower = current["lower"] <= current["body"] * WICK_PERCENT / 100.0
    if direction == "bullish":
        return (
            _is_bullish(current)
            and _is_bullish(previous)
            and strong
            and current["close"] > previous["high"]
            and controlled_upper
        )
    return (
        _is_bearish(current)
        and _is_bearish(previous)
        and strong
        and current["close"] < previous["low"]
        and controlled_lower
    )


def _rejection(metrics: Dict[str, float], side: str) -> bool:
    if side == "call":
        return (
            metrics["lower"] / metrics["range"] >= MIN_REJECTION_WICK_RATIO
            and metrics["close_position"] >= MIN_CLOSE_POSITION
            and metrics["close"] >= metrics["open"]
        )
    return (
        metrics["upper"] / metrics["range"] >= MIN_REJECTION_WICK_RATIO
        and metrics["close_position"] <= 1.0 - MIN_CLOSE_POSITION
        and metrics["close"] <= metrics["open"]
    )


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
    data["atr"] = data["tr"].rolling(14, min_periods=14).mean()
    data["avg_body"] = data["body"].rolling(FORCE_PERIOD, min_periods=FORCE_PERIOD).mean()

    if not math.isfinite(_safe_float(data.iloc[-1]["atr"], np.nan)):
        return _empty("ATR insuficiente")

    # El patrón termina en la vela N cerrada: c1, c2, c3.
    c1_i, c2_i, c3_i = len(data) - 3, len(data) - 2, len(data) - 1
    c1 = _metrics(data.iloc[c1_i])
    c2 = _metrics(data.iloc[c2_i])
    c3 = _metrics(data.iloc[c3_i])
    atr = _safe_float(data.iloc[c3_i]["atr"], 0.0)
    avg_body = data["avg_body"]

    c1_sell = _continuation(data, c1_i, "bearish", avg_body)
    c1_buy = _continuation(data, c1_i, "bullish", avg_body)
    c3_sell = _continuation(data, c3_i, "bearish", avg_body)
    c3_buy = _continuation(data, c3_i, "bullish", avg_body)
    c2_has_signal = _continuation(data, c2_i, "bullish", avg_body) or _continuation(data, c2_i, "bearish", avg_body)

    lookback = data.iloc[max(0, c1_i - SWING_LOOKBACK):c1_i]
    if lookback.empty:
        lookback = data.iloc[:c1_i]
    swing_high = float(lookback["high"].max())
    swing_low = float(lookback["low"].min())

    call_rejection = _rejection(c3, "call")
    put_rejection = _rejection(c3, "put")
    call_room = (swing_high - c3["close"]) / atr if atr > 0 else 0.0
    put_room = (c3["close"] - swing_low) / atr if atr > 0 else 0.0
    not_extended = c3["body"] / atr <= MAX_SIGNAL_BODY_ATR if atr > 0 else False

    analysis = {
        "pair": pair,
        "force": False,
        "pattern": "1-2-3-4",
        "execution_mode": "next_candle",
        "strict_rejection_filter": True,
        "min_rejection_wick_ratio": MIN_REJECTION_WICK_RATIO,
        "min_close_position": MIN_CLOSE_POSITION,
        "expiration_minutes": 3,
        "atr": atr,
        "last_swing_high": swing_high,
        "last_swing_low": swing_low,
        "candle_1": c1,
        "candle_2": c2,
        "candle_3": c3,
        "c1_sell_continuation": c1_sell,
        "c1_buy_continuation": c1_buy,
        "c2_has_signal": c2_has_signal,
        "c3_sell_continuation": c3_sell,
        "c3_buy_continuation": c3_buy,
        "call_rejection": call_rejection,
        "put_rejection": put_rejection,
        "call_room_atr": call_room,
        "put_room_atr": put_room,
        "body_atr_ratio": c3["body"] / atr if atr > 0 else math.inf,
        "structure": "bullish" if c3_buy else "bearish" if c3_sell else "mixed",
        "impulse_phase": "confirmed_pattern" if not c2_has_signal else "blocked_intermediate_signal",
    }

    if c2_has_signal:
        return _empty("Descartada: la vela 2 tiene señal de continuidad", analysis)

    candidates = []
    if c1_sell and c3_buy and call_rejection and call_room >= MIN_ROOM_ATR and not_extended:
        score = 100
        candidates.append((score, "call", "CALL: VEN en vela 1 + vela 2 limpia + COM en vela 3 + rechazo + espacio"))
    if c1_buy and c3_sell and put_rejection and put_room >= MIN_ROOM_ATR and not_extended:
        score = 100
        candidates.append((score, "put", "PUT: COM en vela 1 + vela 2 limpia + VEN en vela 3 + rechazo + espacio"))

    if not candidates:
        return _empty("Sin patrón válido: continuidad + vela intermedia limpia + rechazo + espacio", analysis)

    score, signal, reason = candidates[0]
    analysis["force"] = True
    analysis["pullback"] = {
        "valid": True,
        "previous_candle_confirmed": True,
        "extreme_confirmed": True,
    }
    return {
        "signal": signal,
        "direction": "bullish" if signal == "call" else "bearish",
        "score": score,
        "entry_quality": score,
        "entry_type": "force",
        "blocked": False,
        "reason": reason + " | rechazo estricto en C3 | N cerrada / ejecución N+1",
        "signal_price": c3["close"],
        "candle_timestamp": int(data.iloc[c3_i]["from"]) if "from" in data.columns else None,
        "analysis": analysis,
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente: continuidad filtrada + rechazo + espacio")
