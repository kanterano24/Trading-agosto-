"""
strategy.py

ESTRATEGIA SOLO INDECISIÓN
PARA BINARY OTC M1

Analiza únicamente velas de indecisión en N
y prepara entrada para N+1.

NO usa:
- rechazo
- continuidad
- descanso
- fuerza
- divergencia
"""

from __future__ import annotations
from typing import Any, Dict, Optional
import math
import pandas as pd
import numpy as np


# ============================================================
# CONFIG
# ============================================================

MIN_BARS = 35
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

ATR_PERIOD = 14
RSI_PERIOD = 14

# INDECISIÓN
INDECISION_MAX_BODY_RATIO = 0.30
INDECISION_MIN_WICK_RATIO = 0.25

MIN_STRUCTURE_SCORE = 3


EPS = 1e-12


# ============================================================
# BASE
# ============================================================

def _empty_result(reason="sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "score": 0,
        "reason": reason,
        "blocked": True,
        "continuity": False,
        "entry_type": None,
        "analysis": {},
    }


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    df.rename(columns={
        "max": "high",
        "min": "low",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
    }, inplace=True)

    required = ["open", "high", "low", "close"]

    if any(c not in df.columns for c in required):
        return pd.DataFrame()

    df = df[required].apply(pd.to_numeric, errors="coerce")
    df.dropna(inplace=True)

    return df.reset_index(drop=True)


# ============================================================
# INDICADORES
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize(df)
    if df.empty:
        return df

    close = df["close"]

    df["ema9"] = close.ewm(span=EMA_FAST).mean()
    df["ema21"] = close.ewm(span=EMA_MID).mean()
    df["ema50"] = close.ewm(span=EMA_SLOW).mean()

    # ATR
    high = df["high"]
    low = df["low"]

    tr = (high - low)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def _atr(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return float(df["atr"].iloc[-1] or 0.0)


# ============================================================
# VELA
# ============================================================

def candle_metrics(c):
    o = float(c["open"])
    h = float(c["high"])
    l = float(c["low"])
    cl = float(c["close"])

    rng = max(h - l, EPS)
    body = abs(cl - o)

    return {
        "body_ratio": body / rng,
        "upper_ratio": (h - max(o, cl)) / rng,
        "lower_ratio": (min(o, cl) - l) / rng,
        "close": cl,
        "open": o
    }


def candle_direction(c):
    if c["close"] > c["open"]:
        return "bull"
    if c["close"] < c["open"]:
        return "bear"
    return "neutral"


# ============================================================
# INDECISIÓN
# ============================================================

def _is_indecision(c):
    if c["body_ratio"] > INDECISION_MAX_BODY_RATIO:
        return False

    return (
        c["upper_ratio"] >= INDECISION_MIN_WICK_RATIO
        or c["lower_ratio"] >= INDECISION_MIN_WICK_RATIO
    )


# ============================================================
# ESTRUCTURA SIMPLE
# ============================================================

def detect_structure(df):
    if len(df) < 10:
        return "range"

    last = df.iloc[-1]

    if last["ema9"] > last["ema21"] > last["ema50"]:
        return "bullish"

    if last["ema9"] < last["ema21"] < last["ema50"]:
        return "bearish"

    return "range"


# ============================================================
# API
# ============================================================

def analyze_market(df: Optional[pd.DataFrame] = None, **kwargs) -> Dict[str, Any]:

    result = _empty_result()

    df = add_indicators(df)

    if len(df) < MIN_BARS:
        result["reason"] = "historial insuficiente"
        return result

    current = df.iloc[-1]
    history = df.iloc[:-1]

    atr = _atr(df)

    structure = detect_structure(df)

    c = candle_metrics(current)

    # ========================================================
    # SOLO INDECISIÓN
    # ========================================================

    if _is_indecision(c):

        # dirección basada en tendencia
        if structure == "bullish":
            signal = "call"
            zone = "indecision_alcista"

        elif structure == "bearish":
            signal = "put"
            zone = "indecision_bajista"

        else:
            return result

        result.update({
            "signal": signal,
            "score": 75,
            "blocked": False,
            "continuity": True,
            "entry_type": "indecision",
            "reason": f"{signal.upper()} | INDECISIÓN | entrada en N+1",
            "analysis": {
                "structure": structure,
                "atr": atr,
                "candle": c,
                "note": "esperar confirmación siguiente vela"
            }
        })

        return result

    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)


if __name__ == "__main__":
    print("Strategy SOLO INDECISIÓN lista.")
