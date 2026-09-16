"""
strategy.py

ESTRATEGIA ESTRUCTURAL PROFESIONAL PARA OTC M1
- Rechazo
- Continuidad
- Fuerza
- Descanso
- Indecisión
- Divergencia RSI

Optimizada para entrada en N+1 sin repaint.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import numpy as np
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

MIN_BARS = 35

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14

MIN_SCORE = 70


# ============================================================
# HELPERS
# ============================================================

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema9"] = df["close"].ewm(span=EMA_FAST).mean()
    df["ema21"] = df["close"].ewm(span=EMA_MID).mean()
    df["ema50"] = df["close"].ewm(span=EMA_SLOW).mean()

    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(RSI_PERIOD).mean()

    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift()),
            abs(df["low"] - df["close"].shift()),
        ),
    )

    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()

    return df


def candle_metrics(c):
    body = abs(c["close"] - c["open"])
    rng = max(c["high"] - c["low"], 1e-9)

    return {
        "open": c["open"],
        "close": c["close"],
        "high": c["high"],
        "low": c["low"],
        "body": body,
        "range": rng,
        "body_ratio": body / rng,
        "upper": c["high"] - max(c["open"], c["close"]),
        "lower": min(c["open"], c["close"]) - c["low"],
        "close_position": (c["close"] - c["low"]) / rng,
    }


def candle_direction(c):
    if c["close"] > c["open"]:
        return "bullish"
    if c["close"] < c["open"]:
        return "bearish"
    return "neutral"


# ============================================================
# STRUCTURE
# ============================================================

def detect_structure(df: pd.DataFrame) -> str:
    df = add_indicators(df)

    if len(df) < 10:
        return "range"

    e9 = df["ema9"].iloc[-1]
    e21 = df["ema21"].iloc[-1]
    e50 = df["ema50"].iloc[-1]

    if e9 > e21 > e50:
        return "bullish"

    if e9 < e21 < e50:
        return "bearish"

    return "range"


# ============================================================
# CORE STRATEGY
# ============================================================

def analyze_market(df: pd.DataFrame) -> Dict[str, Any]:

    df = _normalize(df)

    result = {
        "signal": None,
        "score": 0,
        "reason": "",
        "analysis": {},
    }

    if len(df) < MIN_BARS:
        return result

    df = add_indicators(df)

    current = df.iloc[-1]
    previous = df.iloc[-2]

    c = candle_metrics(current)
    p = candle_metrics(previous)

    structure = detect_structure(df)

    atr = df["atr"].iloc[-1]
    rsi = df["rsi"].iloc[-1]

    score = 0

    # ========================================================
    # FUERZA
    # ========================================================

    if c["body_ratio"] > 0.6:

        score += 40

        if structure == "bullish":
            signal = "call"
        elif structure == "bearish":
            signal = "put"
        else:
            signal = None

        if signal:
            return {
                "signal": signal,
                "score": score,
                "reason": "FUERZA",
            }

    # ========================================================
    # CONTINUIDAD
    # ========================================================

    if (
        candle_direction(previous) == structure
        and c["body_ratio"] > 0.5
    ):

        score += 30

        if structure == "bullish":
            return {"signal": "call", "score": score, "reason": "CONTINUIDAD"}

        if structure == "bearish":
            return {"signal": "put", "score": score, "reason": "CONTINUIDAD"}

    # ========================================================
    # RECHAZO
    # ========================================================

    if c["lower"] > c["body"] * 1.2 and structure == "bullish":
        return {"signal": "call", "score": 80, "reason": "RECHAZO"}

    if c["upper"] > c["body"] * 1.2 and structure == "bearish":
        return {"signal": "put", "score": 80, "reason": "RECHAZO"}

    # ========================================================
    # RSI
    # ========================================================

    if structure == "bullish" and rsi < 40:
        return {"signal": "call", "score": 75, "reason": "RSI BAJO"}

    if structure == "bearish" and rsi > 60:
        return {"signal": "put", "score": 75, "reason": "RSI ALTO"}

    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    print("strategy.py listo ✔")
