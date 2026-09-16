"""
strategy.py

ESTRATEGIA: DESCANSO + INDECISIÓN (PRICE ACTION PURO)
PARA BINARY OTC M1

✔ SOLO usa velas (OHLC)
✔ NO usa indicadores (EMA, RSI, ATR, etc.)

Detecta:
- Descanso
- Indecisión

Entrada en N+1
"""

from __future__ import annotations
from typing import Any, Dict, Optional
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

MIN_BARS = 20

# DESCANSO
REST_MAX_BODY_RATIO = 0.50
REST_MIN_PREVIOUS_BODY_RATIO = 0.55

# INDECISIÓN
INDECISION_MAX_BODY_RATIO = 0.30
INDECISION_MIN_WICK_RATIO = 0.25


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
# VELA
# ============================================================

def candle_metrics(c):
    o = float(c["open"])
    h = float(c["high"])
    l = float(c["low"])
    cl = float(c["close"])

    rng = max(h - l, 1e-12)
    body = abs(cl - o)

    upper = max(h - max(o, cl), 0.0)
    lower = max(min(o, cl) - l, 0.0)

    return {
        "open": o,
        "close": cl,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng
    }


def candle_direction(c):
    if c["close"] > c["open"]:
        return "bull"
    if c["close"] < c["open"]:
        return "bear"
    return "neutral"


# ============================================================
# ESTRUCTURA SIMPLE (SIN INDICADORES)
# ============================================================

def detect_structure(df: pd.DataFrame) -> str:
    if len(df) < 6:
        return "range"

    highs = df["high"]
    lows = df["low"]

    # últimos swings simples
    if highs.iloc[-1] > highs.iloc[-2] and lows.iloc[-1] > lows.iloc[-2]:
        return "bullish"

    if highs.iloc[-1] < highs.iloc[-2] and lows.iloc[-1] < lows.iloc[-2]:
        return "bearish"

    return "range"


# ============================================================
# DESCANSO
# ============================================================

def _is_rest_candle(c, p, direction):

    if c["body_ratio"] > REST_MAX_BODY_RATIO:
        return False

    if p["body_ratio"] < REST_MIN_PREVIOUS_BODY_RATIO:
        return False

    if direction == "bullish":
        return p["close"] > p["open"]

    if direction == "bearish":
        return p["close"] < p["open"]

    return False


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
# API
# ============================================================

def analyze_market(df: Optional[pd.DataFrame] = None, **kwargs) -> Dict[str, Any]:

    result = _empty_result()

    df = _normalize(df)

    if len(df) < MIN_BARS:
        result["reason"] = "historial insuficiente"
        return result

    current = df.iloc[-1]
    previous = df.iloc[-2]

    structure = detect_structure(df)

    c = candle_metrics(current)
    p = candle_metrics(previous)

    # ========================================================
    # 1. DESCANSO (PRIORIDAD)
    # ========================================================

    if _is_rest_candle(c, p, structure):

        if structure == "bullish":
            signal = "call"
        elif structure == "bearish":
            signal = "put"
        else:
            return result

        result.update({
            "signal": signal,
            "score": 75,
            "blocked": False,
            "continuity": True,
            "entry_type": "rest",
            "reason": f"{signal.upper()} | DESCANSO | price action | N+1",
            "analysis": {
                "structure": structure,
                "type": "rest"
            }
        })

        return result

    # ========================================================
    # 2. INDECISIÓN
    # ========================================================

    if _is_indecision(c):

        if structure == "bullish":
            signal = "call"
        elif structure == "bearish":
            signal = "put"
        else:
            return result

        result.update({
            "signal": signal,
            "score": 70,
            "blocked": False,
            "continuity": True,
            "entry_type": "indecision",
            "reason": f"{signal.upper()} | INDECISIÓN | price action | N+1",
            "analysis": {
                "structure": structure,
                "type": "indecision"
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
    print("Strategy DESCANSO + INDECISIÓN (SIN INDICADORES) lista.")
