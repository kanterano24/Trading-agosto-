# ============================================================
# VERSION SIN INDICADORES (SIN EMA, RSI, ATR)
# ============================================================

from __future__ import annotations
from typing import Any, Dict, Optional
import pandas as pd
import math

MIN_BARS = 35
MAX_CANDLES = 80

EPS = 1e-12


# ============================================================
# UTILIDADES
# ============================================================

def _safe_float(v, d=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except:
        return d


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    rename = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "max": "high",
        "min": "low",
    }

    out.rename(columns=rename, inplace=True)

    for c in ["open", "high", "low", "close"]:
        if c not in out.columns:
            return pd.DataFrame()
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out.dropna(inplace=True)
    out = out.tail(MAX_CANDLES).reset_index(drop=True)

    return out


# ============================================================
# ATR SIMPLIFICADO (SIN INDICADORES)
# ============================================================

def _atr_simple(df: pd.DataFrame) -> float:
    if len(df) < 5:
        return 0.0001

    ranges = (df["high"] - df["low"]).tail(14)
    val = ranges.mean()

    return max(float(val), 0.0001)


# ============================================================
# VELAS
# ============================================================

def candle_metrics(c):
    o = _safe_float(c["open"])
    h = _safe_float(c["high"])
    l = _safe_float(c["low"])
    cl = _safe_float(c["close"])

    rng = max(h - l, EPS)
    body = abs(cl - o)

    return {
        "open": o,
        "close": cl,
        "high": h,
        "low": l,
        "body": body,
        "range": rng,
        "body_ratio": body / rng,
    }


def candle_direction(c):
    if c["close"] > c["open"]:
        return "bull"
    elif c["close"] < c["open"]:
        return "bear"
    return "neutral"


# ============================================================
# ESTRUCTURA SIMPLE (SIN EMA)
# ============================================================

def detect_structure(df: pd.DataFrame):
    if len(df) < 10:
        return "range"

    highs = df["high"]
    lows = df["low"]

    if highs.iloc[-1] > highs.iloc[-5] and lows.iloc[-1] > lows.iloc[-5]:
        return "bullish"

    if highs.iloc[-1] < highs.iloc[-5] and lows.iloc[-1] < lows.iloc[-5]:
        return "bearish"

    return "range"


# ============================================================
# IMPULSO SIMPLE
# ============================================================

def analyze_impulse(df, direction):
    if len(df) < 5:
        return False

    last = df.tail(3)

    bulls = sum(1 for i in range(len(last)) if last.iloc[i]["close"] > last.iloc[i]["open"])
    bears = sum(1 for i in range(len(last)) if last.iloc[i]["close"] < last.iloc[i]["open"])

    if direction == "bullish":
        return bulls >= 2

    if direction == "bearish":
        return bears >= 2

    return False


# ============================================================
# API PRINCIPAL
# ============================================================

def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    result = {
        "signal": None,
        "score": 0,
        "reason": "sin señal",
        "blocked": True,
    }

    data = _normalize(df)

    if len(data) < MIN_BARS:
        result["reason"] = "datos insuficientes"
        return result

    current = data.iloc[-1]
    history = data.iloc[:-1]

    structure = detect_structure(history)

    c = candle_metrics(current)
    atr = _atr_simple(history)

    # ========================================================
    # FUERZA (SIN INDICADORES)
    # ========================================================

    if structure == "bullish":
        if c["body_ratio"] > 0.6 and c["close"] > c["open"]:
            result.update({
                "signal": "call",
                "score": 80,
                "blocked": False,
                "reason": "FUERZA ALCISTA (sin indicadores)"
            })
            return result

    if structure == "bearish":
        if c["body_ratio"] > 0.6 and c["close"] < c["open"]:
            result.update({
                "signal": "put",
                "score": 80,
                "blocked": False,
                "reason": "FUERZA BAJISTA (sin indicadores)"
            })
            return result

    result["reason"] = "sin condiciones limpias"
    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)


if __name__ == "__main__":
    print("strategy SIN indicadores cargada")
