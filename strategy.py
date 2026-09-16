"""
strategy.py

ESTRATEGIA SOLO DESCANSO
PARA BINARY OTC M1

Detecta vela de descanso en N
y prepara entrada para N+1.

NO usa:
- rechazo
- continuidad
- indecisión
- fuerza
- divergencia
"""

from __future__ import annotations
from typing import Any, Dict, Optional
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

# DESCANSO
REST_MAX_BODY_RATIO = 0.50
REST_MIN_PREVIOUS_BODY_RATIO = 0.55


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

    # ATR simple
    df["atr"] = (df["high"] - df["low"]).rolling(ATR_PERIOD).mean()

    return df


# ============================================================
# VELA
# ============================================================

def candle_metrics(c):
    o = float(c["open"])
    h = float(c["high"])
    l = float(c["low"])
    cl = float(c["close"])

    rng = max(h - l, 1e-12)
    body = abs(cl - o)

    return {
        "open": o,
        "close": cl,
        "body_ratio": body / rng
    }


def candle_direction(c):
    if c["close"] > c["open"]:
        return "bull"
    if c["close"] < c["open"]:
        return "bear"
    return "neutral"


# ============================================================
# DESCANSO
# ============================================================

def _is_rest_candle(current, previous, direction):

    # cuerpo pequeño en vela actual
    if current["body_ratio"] > REST_MAX_BODY_RATIO:
        return False

    # vela anterior debe ser fuerte
    if previous["body_ratio"] < REST_MIN_PREVIOUS_BODY_RATIO:
        return False

    # dirección correcta
    if direction == "bullish":
        return previous["close"] > previous["open"]

    if direction == "bearish":
        return previous["close"] < previous["open"]

    return False


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
    previous = df.iloc[-2]

    structure = detect_structure(df)

    c = candle_metrics(current)
    p = candle_metrics(previous)

    # ========================================================
    # SOLO DESCANSO
    # ========================================================

    if _is_rest_candle(c, p, structure):

        if structure == "bullish":
            signal = "call"
            zone = "descanso_alcista"

        elif structure == "bearish":
            signal = "put"
            zone = "descanso_bajista"

        else:
            return result

        result.update({
            "signal": signal,
            "score": 75,
            "blocked": False,
            "continuity": True,
            "entry_type": "rest",
            "reason": f"{signal.upper()} | DESCANSO | entrada en N+1",
            "analysis": {
                "structure": structure,
                "current_candle": c,
                "previous_candle": p,
                "note": "vela de pausa tras impulso, esperar continuación"
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
    print("Strategy SOLO DESCANSO lista.")
