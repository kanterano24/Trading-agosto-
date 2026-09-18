"""strategy.py vv2 - Rechazo exclusivo de soporte/resistencia para M1.

API compatible con bot.py vv2:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)

Solo devuelve señales CALL/PUT cuando la última vela cerrada muestra rechazo
confirmado de soporte/resistencia. No genera señales de continuación.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
import math
import pandas as pd

MIN_SCORE = 80
LOOKBACK = 60
ZONE_ATR_FACTOR = 0.30


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=["from", "open", "high", "low", "close"])
    out = df.copy()
    out = out.rename(columns={"max": "high", "min": "low"})
    for col in ["from", "open", "high", "low", "close"]:
        if col not in out.columns:
            return pd.DataFrame(columns=["from", "open", "high", "low", "close"])
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["from", "open", "high", "low", "close"])
    return out.sort_values("from").drop_duplicates("from", keep="last").reset_index(drop=True)


def _atr(data: pd.DataFrame, period: int = 14) -> float:
    if len(data) < 2:
        return 0.0
    prev_close = data["close"].shift(1)
    tr = pd.concat([
        data["high"] - data["low"],
        (data["high"] - prev_close).abs(),
        (data["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    value = tr.dropna().tail(period).mean()
    return _safe_float(value)


def _ema(values: pd.Series, period: int) -> float:
    if values.empty:
        return 0.0
    return _safe_float(values.ewm(span=period, adjust=False).mean().iloc[-1])


def _empty(reason: str, pair: str = "", analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": None,
        "score": 0,
        "entry_quality": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason,
        "signal_price": None,
        "candle_timestamp": None,
        "analysis": {"pair": pair, **(analysis or {})},
    }


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: str = "",
    df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    history = _normalize(previous_m1 if previous_m1 is not None else df)
    current = _normalize(pd.DataFrame([candle_1m]) if isinstance(candle_1m, dict) else candle_1m)
    if current.empty:
        return _empty("invalid_current_candle", pair)
    candle = current.iloc[-1]
    data = pd.concat([history, current], ignore_index=True)
    data = _normalize(data)
    if len(data) < 30:
        return _empty("insufficient_candles", pair)

    row = data.iloc[-1]
    o, h, l, c = map(float, [row["open"], row["high"], row["low"], row["close"]])
    candle_range = max(h - l, 1e-12)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    atr = _atr(data.iloc[:-1])
    if atr <= 0:
        return _empty("invalid_atr", pair)

    lookback = data.iloc[:-1].tail(LOOKBACK)
    support = float(lookback["low"].min())
    resistance = float(lookback["high"].max())
    tolerance = max(atr * ZONE_ATR_FACTOR, candle_range * 0.10)
    near_support = l <= support + tolerance and c > support
    near_resistance = h >= resistance - tolerance and c < resistance

    closes = data.iloc[:-1]["close"].tail(30)
    ema_fast = _ema(closes, 8)
    ema_slow = _ema(closes, 21)
    bullish_context = ema_fast >= ema_slow
    bearish_context = ema_fast <= ema_slow

    bullish_rejection = (
        near_support and lower_wick >= max(body * 1.40, atr * 0.20)
        and c > o and (c - l) / candle_range >= 0.62
        and body / candle_range <= 0.65
    )
    bearish_rejection = (
        near_resistance and upper_wick >= max(body * 1.40, atr * 0.20)
        and c < o and (h - c) / candle_range >= 0.62
        and body / candle_range <= 0.65
    )

    timestamp = int(row["from"]) if pd.notna(row["from"]) else None
    common = {
        "pair": pair, "force": True, "pattern": "support_resistance_rejection",
        "execution_mode": "immediate_closed_candle", "expiration_minutes": 1,
        "atr": atr, "last_swing_high": resistance, "last_swing_low": support,
        "support": support, "resistance": resistance, "ema_fast": ema_fast,
        "ema_slow": ema_slow, "body": body, "upper_wick": upper_wick,
        "lower_wick": lower_wick, "zone": "soporte" if bullish_rejection else "resistencia",
        "level": support if bullish_rejection else resistance,
        "structure": "bullish" if bullish_rejection else "bearish" if bearish_rejection else "mixed",
    }

    if bullish_rejection:
        score = 76
        reasons = ["bullish_support_rejection"]
        if lower_wick >= body * 2.0:
            score += 7; reasons.append("long_lower_wick")
        if bullish_context:
            score += 5; reasons.append("ema_context")
        if c > support + tolerance * 0.25:
            score += 5; reasons.append("close_above_support")
        if score >= MIN_SCORE:
            return {"signal": "call", "direction": "bullish", "score": min(score, 100),
                    "entry_quality": min(score, 100), "entry_type": "force", "blocked": False,
                    "reason": ",".join(reasons), "signal_price": c,
                    "candle_timestamp": timestamp, "analysis": common}

    if bearish_rejection:
        score = 76
        reasons = ["bearish_resistance_rejection"]
        if upper_wick >= body * 2.0:
            score += 7; reasons.append("long_upper_wick")
        if bearish_context:
            score += 5; reasons.append("ema_context")
        if c < resistance - tolerance * 0.25:
            score += 5; reasons.append("close_below_resistance")
        if score >= MIN_SCORE:
            return {"signal": "put", "direction": "bearish", "score": min(score, 100),
                    "entry_quality": min(score, 100), "entry_type": "force", "blocked": False,
                    "reason": ",".join(reasons), "signal_price": c,
                    "candle_timestamp": timestamp, "analysis": common}

    common["force"] = False
    common["zone"] = "ninguna"
    return _empty("no_valid_rejection", pair, common)


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
