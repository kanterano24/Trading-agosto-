"""
strategy.py

ESTRATEGIA PROFESIONAL MULTI-SETUP (COMPATIBLE CON bot.py)

- Rechazo
- Continuidad
- Descanso
- Fuerza
- Divergencia RSI

✔ No rompe compatibilidad
✔ Mantiene estructura esperada por bot.py
✔ Selecciona el mejor setup automáticamente
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import math

import numpy as np
import pandas as pd


# =========================
# CONFIGURACIÓN
# =========================

MIN_CANDLES = 22
MIN_SCORE_TO_TRADE = 75

PRIORITY = {
    "force": 5,
    "rejection_resistance": 4,
    "rejection_support": 4,
    "continuity": 3,
    "rest": 2,
    "rsi_divergence": 2,
}


# =========================
# UTILIDADES
# =========================

def safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < MIN_CANDLES:
        return pd.DataFrame()

    df = df.copy()
    df = df.sort_values("from")
    df = df.drop_duplicates(subset=["from"])

    return df.tail(100)


def candle_info(c) -> Tuple[float, float, float, str]:
    body = abs(c["close"] - c["open"])
    wick_up = c["high"] - max(c["open"], c["close"])
    wick_down = min(c["open"], c["close"]) - c["low"]

    direction = "bull" if c["close"] > c["open"] else "bear"

    return body, wick_up, wick_down, direction


# =========================
# ESTRUCTURA
# =========================

def detect_structure(df: pd.DataFrame) -> Dict[str, Any]:
    highs = df["high"].tail(10).values
    lows = df["low"].tail(10).values

    higher_highs = all(highs[i] > highs[i - 1] for i in range(1, len(highs)))
    lower_lows = all(lows[i] < lows[i - 1] for i in range(1, len(lows)))

    if higher_highs:
        return {"trend": "up", "score": 15}
    elif lower_lows:
        return {"trend": "down", "score": 15}
    else:
        return {"trend": "range", "score": 5}


# =========================
# RSI
# =========================

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()

    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


# =========================
# ANÁLISIS PRINCIPAL
# =========================

def analyze_market(df: pd.DataFrame) -> Dict[str, Any]:
    df = safe_dataframe(df)

    result: Dict[str, Any] = {
        "valid": False,
        "signal": None,
        "score": 0,
        "entry_type": None,
        "entry_quality": 0,
        "analysis": {}
    }

    if df.empty:
        return result

    structure = detect_structure(df)
    df["rsi"] = calculate_rsi(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    avg_range = (df["high"] - df["low"]).mean()

    candidates = []

    # =========================
    # 1. FUERZA
    # =========================
    body, _, _, direction = candle_info(last)

    if body > avg_range * 0.6:
        candidates.append({
            **result,
            "valid": True,
            "signal": "call" if direction == "bull" else "put",
            "score": 80,
            "entry_type": "force",
            "entry_quality": 9,
            "analysis": {"structure_score": structure["score"]}
        })

    # =========================
    # 2. CONTINUIDAD
    # =========================
    if structure["trend"] == "up" and last["close"] > prev["close"]:
        candidates.append({
            **result,
            "valid": True,
            "signal": "call",
            "score": 78,
            "entry_type": "continuity",
            "entry_quality": 7,
            "analysis": {"structure_score": structure["score"]}
        })

    if structure["trend"] == "down" and last["close"] < prev["close"]:
        candidates.append({
            **result,
            "valid": True,
            "signal": "put",
            "score": 78,
            "entry_type": "continuity",
            "entry_quality": 7,
            "analysis": {"structure_score": structure["score"]}
        })

    # =========================
    # 3. RECHAZO
    # =========================
    body, wick_up, wick_down, _ = candle_info(last)

    if wick_down > body * 1.5:
        candidates.append({
            **result,
            "valid": True,
            "signal": "call",
            "score": 82,
            "entry_type": "rejection_support",
            "entry_quality": 8,
            "analysis": {"structure_score": structure["score"]}
        })

    if wick_up > body * 1.5:
        candidates.append({
            **result,
            "valid": True,
            "signal": "put",
            "score": 82,
            "entry_type": "rejection_resistance",
            "entry_quality": 8,
            "analysis": {"structure_score": structure["score"]}
        })

    # =========================
    # 4. DESCANSO
    # =========================
    last5 = df.tail(5)
    bodies = abs(last5["close"] - last5["open"])

    if bodies.mean() < avg_range * 0.4:
        candidates.append({
            **result,
            "valid": True,
            "signal": "call" if structure["trend"] == "up" else "put",
            "score": 75,
            "entry_type": "rest",
            "entry_quality": 6,
            "analysis": {"structure_score": structure["score"]}
        })

    # =========================
    # 5. DIVERGENCIA RSI
    # =========================
    rsi_last = df["rsi"].iloc[-1]

    if rsi_last < 30:
        candidates.append({
            **result,
            "valid": True,
            "signal": "call",
            "score": 77,
            "entry_type": "rsi_divergence",
            "entry_quality": 7,
            "analysis": {"structure_score": structure["score"]}
        })

    if rsi_last > 70:
        candidates.append({
            **result,
            "valid": True,
            "signal": "put",
            "score": 77,
            "entry_type": "rsi_divergence",
            "entry_quality": 7,
            "analysis": {"structure_score": structure["score"]}
        })

    # =========================
    # SELECCIÓN FINAL
    # =========================
    if not candidates:
        return result

    best = sorted(
        candidates,
        key=lambda x: (
            PRIORITY.get(x["entry_type"], 0),
            x["score"],
            x["entry_quality"],
            x["analysis"].get("structure_score", 0)
        ),
        reverse=True
    )[0]

    if best["score"] < MIN_SCORE_TO_TRADE:
        best["valid"] = False
        best["signal"] = None

    return best
