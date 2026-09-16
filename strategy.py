from __future__ import annotations

from typing import Dict, Any
import pandas as pd

# ============================================================
# CONFIGURACIÓN
# ============================================================

LOOKBACK_ZONES = 10

WICK_BODY_RATIO = 1.5
MIN_WICK_RATIO = 0.5

FORCE_CANDLES = 3
MIN_FORCE_COUNT = 2

# ============================================================
# UTILIDADES
# ============================================================

def safe_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < 20:
        return pd.DataFrame()
    return df.copy().reset_index(drop=True)


def get_zones(df: pd.DataFrame):
    resistance = df["high"].tail(LOOKBACK_ZONES).max()
    support = df["low"].tail(LOOKBACK_ZONES).min()
    return support, resistance


def candle_metrics(candle: Dict[str, float]):
    open_ = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])

    body = abs(close - open_)
    range_ = max(high - low, 1e-6)

    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low

    return open_, close, high, low, body, range_, upper_wick, lower_wick


# ============================================================
# RECHAZO PURO
# ============================================================

def detect_rejection(candle, support, resistance):

    open_, close, high, low, body, range_, uw, lw = candle_metrics(candle)

    rejection_support = (
        low <= support * 1.001 and
        lw > body * WICK_BODY_RATIO and
        close > open_ and
        (lw / range_) > MIN_WICK_RATIO
    )

    rejection_resistance = (
        high >= resistance * 0.999 and
        uw > body * WICK_BODY_RATIO and
        close < open_ and
        (uw / range_) > MIN_WICK_RATIO
    )

    return rejection_support, rejection_resistance, uw, lw, body


# ============================================================
# FUERZA DEL MOVIMIENTO
# ============================================================

def detect_strength(df: pd.DataFrame):

    last = df.tail(FORCE_CANDLES)

    bull = 0
    bear = 0

    for _, row in last.iterrows():
        if row["close"] > row["open"]:
            bull += 1
        elif row["close"] < row["open"]:
            bear += 1

    strong_bull = bull >= MIN_FORCE_COUNT
    strong_bear = bear >= MIN_FORCE_COUNT

    momentum_bull = df["close"].iloc[-1] > df["close"].iloc[-2]
    momentum_bear = df["close"].iloc[-1] < df["close"].iloc[-2]

    return strong_bull, strong_bear, momentum_bull, momentum_bear


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def analyze_market(
    candle_1m: Dict[str, float],
    previous_m1: pd.DataFrame,
    pair: str = ""
) -> Dict[str, Any]:

    df = safe_df(previous_m1)

    if df.empty:
        return {
            "signal": None,
            "score": 0,
            "reason": "Sin datos suficientes",
            "analysis": {}
        }

    # =========================
    # ZONAS
    # =========================
    support, resistance = get_zones(df)

    # =========================
    # RECHAZO
    # =========================
    (
        rejection_support,
        rejection_resistance,
        uw,
        lw,
        body
    ) = detect_rejection(candle_1m, support, resistance)

    # =========================
    # FUERZA
    # =========================
    strong_bull, strong_bear, momentum_bull, momentum_bear = detect_strength(df)

    signal = None
    score = 0
    reason = ""

    # =========================
    # CONDICIÓN FINAL
    # =========================
    if rejection_support and strong_bull and momentum_bull:
        signal = "call"
        reason = "Rechazo fuerte en SOPORTE + fuerza alcista"

    elif rejection_resistance and strong_bear and momentum_bear:
        signal = "put"
        reason = "Rechazo fuerte en RESISTENCIA + fuerza bajista"

    # =========================
    # SCORE
    # =========================
    if signal:

        score += 40

        if signal == "call" and lw > body * 2:
            score += 20

        if signal == "put" and uw > body * 2:
            score += 20

        if signal == "call" and strong_bull:
            score += 20

        if signal == "put" and strong_bear:
            score += 20

        if signal == "call" and momentum_bull:
            score += 20

        if signal == "put" and momentum_bear:
            score += 20

    # =========================
    # OUTPUT
    # =========================
    return {
        "signal": signal,
        "score": score,
        "reason": reason,
        "analysis": {
            "zone": "support" if signal == "call" else "resistance" if signal == "put" else "none",
            "strength": {
                "bull": strong_bull,
                "bear": strong_bear
            },
            "momentum": {
                "bull": momentum_bull,
                "bear": momentum_bear
            }
        }
    }
