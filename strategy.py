from __future__ import annotations

from typing import Any, Dict, Optional
import pandas as pd

MIN_CONTINUITY_BODY_RATIO = 0.40
MIN_SCORE_TO_TRADE = 75


def safe_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    required = {"open", "close", "high", "low"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    df = df.copy()
    for c in required:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def resistance_filter(df: pd.DataFrame, direction: str) -> bool:
    if df is None or len(df) < 20:
        return True

    recent = df.tail(20)

    resistance = recent["high"].max()
    support = recent["low"].min()
    price = recent["close"].iloc[-1]

    if direction == "BULLISH" and price >= resistance * 0.999:
        return False

    if direction == "BEARISH" and price <= support * 1.001:
        return False

    return True


def analyze_market(candle_1m, previous_m1=None, pair: str = None):

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "reason": ""
    }

    if not pair:
        return result

    hist = safe_dataframe(previous_m1)

    if len(hist) < 6:
        return result

    direction = "BULLISH" if hist["close"].iloc[-1] > hist["close"].iloc[0] else "BEARISH"
    result["direction"] = direction

    candles = hist.tail(6)

    same = 0
    progression = 0

    for i in range(len(candles)):
        c = candles.iloc[i]
        is_bull = c["close"] > c["open"]

        if (direction == "BULLISH" and is_bull) or (direction == "BEARISH" and not is_bull):
            same += 1

        if i > 0:
            prev = candles.iloc[i - 1]
            if direction == "BULLISH" and c["close"] > prev["close"]:
                progression += 1
            if direction == "BEARISH" and c["close"] < prev["close"]:
                progression += 1

    score = same * 15 + progression * 10
    score = min(int(score / 2.5), 100)

    # 🔥 filtro resistencia
    if not resistance_filter(hist, direction):
        return result

    result["score"] = score

    if score >= MIN_SCORE_TO_TRADE:
        result["valid"] = True
        result["signal"] = "call" if direction == "BULLISH" else "put"

    return result
