from __future__ import annotations

from typing import Any, Dict, Optional
import pandas as pd

# ============================================================
# CONFIG
# ============================================================

MIN_CONTINUITY_BODY_RATIO = 0.40
MIN_SCORE_TO_TRADE = 75

# ============================================================
# UTILIDADES
# ============================================================

def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except:
        return None


def safe_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    required = {"open", "close", "high", "low"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    df = df.copy()
    for c in required:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(subset=list(required), inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def analyze_candle(candle):
    if candle is None:
        return None

    o = _to_float(candle.get("open"))
    c = _to_float(candle.get("close"))
    h = _to_float(candle.get("high"))
    l = _to_float(candle.get("low"))

    if None in (o, c, h, l):
        return None

    rng = h - l
    body = abs(c - o)

    return {
        "open": o,
        "close": c,
        "high": h,
        "low": l,
        "body_ratio": body / rng if rng else 0,
        "upper_wick": (h - max(o, c)) / rng if rng else 0,
        "lower_wick": (min(o, c) - l) / rng if rng else 0,
    }

# ============================================================
# 🧠 MULTI-PAIR ANALYSIS REAL
# ============================================================

def analyze_market(candle_1m, previous_m1=None, pair: str = None):

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "reason": ""
    }

    # ❌ SI NO HAY PAR → NO OPERAR
    if not pair:
        return result

    hist = safe_dataframe(previous_m1)

    if len(hist) < 6:
        return result

    # =========================
    # DIRECCIÓN
    # =========================
    direction = "BULLISH" if hist["close"].iloc[-1] > hist["close"].iloc[0] else "BEARISH"
    result["direction"] = direction

    candles = hist.tail(6).to_dict("records")

    same = 0
    body = 0
    progression = 0

    for i, c in enumerate(candles):
        is_bull = c["close"] > c["open"]

        if (direction == "BULLISH" and is_bull) or (direction == "BEARISH" and not is_bull):
            same += 1

        if c["body_ratio"] > MIN_CONTINUITY_BODY_RATIO:
            body += 1

        if i > 0:
            prev = candles[i - 1]
            if direction == "BULLISH" and c["close"] > prev["close"]:
                progression += 1
            if direction == "BEARISH" and c["close"] < prev["close"]:
                progression += 1

    # =========================
    # SCORE BASE
    # =========================
    score = same * 15 + body * 10 + progression * 10
    score = min(int(score / 2.5), 100)

    # =========================
    # 🔥 BIAS POR PAR
    # =========================
    bias = 1.0

    if "EURUSD" in pair:
        bias = 1.0
    elif "GBP" in pair:
        bias = 1.02
    elif "JPY" in pair:
        bias = 0.98
    elif "OTC" in pair:
        bias = 1.01

    score = min(int(score * bias), 100)

    result["score"] = score

    # =========================
    # SEÑAL FINAL
    # =========================
    if score >= MIN_SCORE_TO_TRADE:
        result["valid"] = True
        result["signal"] = "call" if direction == "BULLISH" else "put"

    return result
