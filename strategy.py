from __future__ import annotations

from typing import Any, Dict, Optional
import pandas as pd
import time

# ============================================================
# CONFIG
# ============================================================

MIN_CONTINUITY_BODY_RATIO = 0.40
MIN_SCORE_TO_TRADE = 70

# ============================================================
# ⛔ CONTROL DE REPETICIÓN (COOLDOWN)
# ============================================================

last_trade_time = {}

def can_trade(pair: str, current_time: float, cooldown=240):
    last_time = last_trade_time.get(pair)

    if last_time is None:
        return True

    return (current_time - last_time) >= cooldown


def register_trade(pair: str, current_time: float):
    last_trade_time[pair] = current_time


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

    if "from" in df.columns:
        df.sort_values("from", inplace=True)

    df.reset_index(drop=True, inplace=True)
    return df


def _get_ohlc(candle):
    if candle is None:
        return None

    o = _to_float(candle.get("open"))
    c = _to_float(candle.get("close"))
    h = _to_float(candle.get("high"))
    l = _to_float(candle.get("low"))

    if None in (o, c, h, l):
        return None

    return o, h, l, c


def get_candle_data(candle):
    ohlc = _get_ohlc(candle)
    if ohlc is None:
        return None

    o, h, l, c = ohlc
    rng = h - l
    body = abs(c - o)

    return {
        "open": o,
        "close": c,
        "high": h,
        "low": l,
        "range": rng,
        "body": body,
        "body_ratio": body / rng if rng else 0,
        "upper_wick_ratio": (h - max(o, c)) / rng if rng else 0,
        "lower_wick_ratio": (min(o, c) - l) / rng if rng else 0,
    }

# ============================================================
# 🧠 3 VELAS EXTERIORES
# ============================================================

def analyze_last_3_candles(candles_5s):
    if not candles_5s or len(candles_5s) < 3:
        return {"valid": False}

    last3 = candles_5s[-3:]
    data = [get_candle_data(c) for c in last3]
    data = [d for d in data if d]

    if len(data) < 3:
        return {"valid": False}

    bullish = 0
    bearish = 0
    strong = 0
    progression_up = 0
    progression_down = 0

    for i, c in enumerate(data):
        if c["close"] > c["open"]:
            bullish += 1
        else:
            bearish += 1

        if c["body_ratio"] > 0.5:
            strong += 1

        if i > 0:
            prev = data[i - 1]
            if c["close"] > prev["close"]:
                progression_up += 1
            if c["close"] < prev["close"]:
                progression_down += 1

    if bullish >= 2 and progression_up >= 1:
        direction = "BULLISH"
    elif bearish >= 2 and progression_down >= 1:
        direction = "BEARISH"
    else:
        return {"valid": False}

    if strong < 2:
        return {"valid": False}

    return {
        "valid": True,
        "direction": direction
    }

# ============================================================
# 🔥 MICRO CONTINUIDAD
# ============================================================

def analyze_micro_continuity(candles_5s, direction):
    if not candles_5s or len(candles_5s) < 6:
        return {"valid": False}

    data = [get_candle_data(c) for c in candles_5s[:6]]
    data = [d for d in data if d]

    if len(data) < 6:
        return {"valid": False}

    same_color = 0
    strong_body = 0
    progression = 0

    for i in range(len(data)):
        c = data[i]
        is_bull = c["close"] > c["open"]

        if (direction == "BULLISH" and is_bull) or (direction == "BEARISH" and not is_bull):
            same_color += 1

        if c["body_ratio"] > MIN_CONTINUITY_BODY_RATIO:
            strong_body += 1

        if i > 0:
            prev = data[i - 1]
            if direction == "BULLISH" and c["close"] > prev["close"]:
                progression += 1
            if direction == "BEARISH" and c["close"] < prev["close"]:
                progression += 1

    return {"valid": same_color >= 4 and strong_body >= 3 and progression >= 3}

# ============================================================
# 🧠 SCORE IA
# ============================================================

def calculate_ai_score(candles_5s, direction):
    data = [get_candle_data(c) for c in candles_5s[:6]]
    data = [d for d in data if d]

    if len(data) < 6:
        return 0

    same_color = 0
    strong_body = 0
    progression = 0
    clean_wicks = 0

    for i in range(len(data)):
        c = data[i]
        is_bull = c["close"] > c["open"]

        if (direction == "BULLISH" and is_bull) or (direction == "BEARISH" and not is_bull):
            same_color += 1

        if c["body_ratio"] > 0.5:
            strong_body += 1

        if direction == "BULLISH" and c["lower_wick_ratio"] < 0.25:
            clean_wicks += 1
        if direction == "BEARISH" and c["upper_wick_ratio"] < 0.25:
            clean_wicks += 1

        if i > 0:
            prev = data[i - 1]
            if direction == "BULLISH" and c["close"] > prev["close"]:
                progression += 1
            if direction == "BEARISH" and c["close"] < prev["close"]:
                progression += 1

    score = (
        same_color * 15 +
        strong_body * 10 +
        progression * 10 +
        clean_wicks * 8
    )

    return min(int(score / 2.5), 100)

# ============================================================
# 🔴 FILTRO WICK
# ============================================================

def wick_filter(candle_data, direction):
    if candle_data is None:
        return False

    if direction == "BULLISH" and candle_data["lower_wick_ratio"] > 0.4:
        return False

    if direction == "BEARISH" and candle_data["upper_wick_ratio"] > 0.4:
        return False

    return True

# ============================================================
# 🧠 ANALISIS PRINCIPAL
# ============================================================

def analyze_market(candle_1m, candles_5s=None, previous_m1=None, pair="EURUSD-OTC"):
    result = {
        "signal": None,
        "valid": False,
        "score": 0,
        "direction": "NEUTRAL",
        "state": "NO_SIGNAL",
        "reason": ""
    }

    current_time = time.time()

    if not can_trade(pair, current_time):
        result["state"] = "COOLDOWN"
        result["reason"] = "esperando 4 min"
        return result

    current = get_candle_data(candle_1m)
    if current is None:
        return result

    hist = safe_dataframe(previous_m1)
    if len(hist) < 6:
        return result

    direction = "BULLISH" if hist["close"].iloc[-1] > hist["close"].iloc[0] else "BEARISH"
    result["direction"] = direction

    last3 = analyze_last_3_candles(candles_5s)
    if not last3["valid"]:
        result["state"] = "NO_LAST3"
        return result

    if last3["direction"] != direction:
        result["state"] = "CONFLICT"
        return result

    micro = analyze_micro_continuity(candles_5s, direction)
    if not micro["valid"]:
        result["state"] = "NO_CONTINUITY"
        return result

    ai_score = calculate_ai_score(candles_5s, direction)
    result["score"] = ai_score

    if ai_score < MIN_SCORE_TO_TRADE:
        result["state"] = "LOW_SCORE"
        return result

    if not wick_filter(current, direction):
        result["state"] = "WICK_REJECT"
        return result

    register_trade(pair, current_time)

    result["signal"] = "call" if direction == "BULLISH" else "put"
    result["valid"] = True
    result["state"] = "SNIPER"
    result["reason"] = f"score={ai_score}"

    return result

# ============================================================
# FIX BOT
# ============================================================

def analyze_live_candle(candle_1m, candles_5s=None, previous_m1=None, pair="EURUSD-OTC"):
    return analyze_market(candle_1m, candles_5s, previous_m1, pair)


def get_signal(candle_1m, candles_5s=None, previous_m1=None, pair="EURUSD-OTC"):
    return analyze_market(candle_1m, candles_5s, previous_m1, pair).get("signal")
