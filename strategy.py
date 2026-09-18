"""Estrategia de rechazo estructural para Binary OTC M1.

Interfaz compatible con bot (1).py vv2:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)

Solo se consideran velas cerradas. La señal se prepara en N y el motor
vv2 la ejecuta en la apertura de N+1.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

MIN_BARS = 35
MAX_CANDLES = 120
ATR_PERIOD = 14
EMA_FAST = 8
EMA_SLOW = 21
LOOKBACK = 60
ZONE_ATR_FACTOR = 0.30
MIN_SCORE = 90
EPS = 1e-10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.rename(columns={"max": "high", "min": "low", "timestamp": "from"}, inplace=True)
    required = ["open", "high", "low", "close"]
    if any(column not in out.columns for column in required):
        return pd.DataFrame()
    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out["from"] = out["from"].astype(int)
        out.sort_values("from", inplace=True)
        out.drop_duplicates("from", keep="last", inplace=True)
    out.dropna(subset=required, inplace=True)
    return out.reset_index(drop=True).tail(MAX_CANDLES).reset_index(drop=True)


def _build_dataframe(candle_1m: Any, previous_m1: Optional[pd.DataFrame], df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is not None:
        return _normalize(df)
    history = _normalize(previous_m1)
    if isinstance(candle_1m, pd.Series):
        current = candle_1m.to_dict()
    elif isinstance(candle_1m, dict):
        current = dict(candle_1m)
    else:
        return history
    current_df = _normalize(pd.DataFrame([current]))
    if current_df.empty:
        return history
    return _normalize(pd.concat([history, current_df], ignore_index=True))


def _true_range(data: pd.DataFrame) -> pd.Series:
    previous_close = data["close"].shift(1)
    return pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ], axis=1,
    ).max(axis=1)


def _metrics(row: pd.Series) -> Dict[str, float]:
    open_price = _safe_float(row.get("open"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))
    candle_range = max(high - low, EPS)
    body = abs(close - open_price)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "range": candle_range,
        "body": body,
        "upper": max(high - max(open_price, close), 0.0),
        "lower": max(min(open_price, close) - low, 0.0),
        "close_position": (close - low) / candle_range,
        "body_ratio": body / candle_range,
    }


def _ema(values: pd.Series, period: int) -> float:
    if values.empty:
        return 0.0
    return _safe_float(values.ewm(span=period, adjust=False).mean().iloc[-1])


def _empty(reason: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "score": 0,
        "entry_quality": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason,
        "analysis": analysis or {},
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    data = _build_dataframe(candle_1m, previous_m1, df)
    if len(data) < MIN_BARS:
        return _empty(f"Historial insuficiente {len(data)}/{MIN_BARS}")

    data = data.copy()
    data["tr"] = _true_range(data)
    data["atr"] = data["tr"].rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    atr = _safe_float(data.iloc[-1]["atr"], 0.0)
    if atr <= 0.0:
        return _empty("ATR insuficiente")

    candle = _metrics(data.iloc[-1])
    previous = data.iloc[:-1]
    support = _safe_float(previous["low"].tail(LOOKBACK).min())
    resistance = _safe_float(previous["high"].tail(LOOKBACK).max())
    tolerance = max(atr * ZONE_ATR_FACTOR, candle["range"] * 0.10)

    near_support = candle["low"] <= support + tolerance and candle["close"] > support
    near_resistance = candle["high"] >= resistance - tolerance and candle["close"] < resistance

    fast = _ema(previous["close"].tail(30), EMA_FAST)
    slow = _ema(previous["close"].tail(30), EMA_SLOW)
    bullish_context = fast >= slow
    bearish_context = fast <= slow

    bullish = (
        near_support
        and candle["lower"] >= max(candle["body"] * 1.40, atr * 0.20)
        and candle["close"] > candle["open"]
        and candle["close_position"] >= 0.62
        and candle["body_ratio"] <= 0.65
    )
    bearish = (
        near_resistance
        and candle["upper"] >= max(candle["body"] * 1.40, atr * 0.20)
        and candle["close"] < candle["open"]
        and (candle["high"] - candle["close"]) / candle["range"] >= 0.62
        and candle["body_ratio"] <= 0.65
    )

    analysis = {
        "pair": pair,
        "pattern": "support_resistance_rejection",
        "execution_mode": "next_candle",
        "expiration_minutes": 1,
        "atr": atr,
        "last_swing_high": resistance,
        "last_swing_low": support,
        "support": support,
        "resistance": resistance,
        "bullish_context": bullish_context,
        "bearish_context": bearish_context,
        "candle": candle,
        "force": True,
    }

    if bullish:
        score = 76
        reasons = ["bullish_support_rejection"]
        if candle["lower"] >= candle["body"] * 2.0:
            score += 7
            reasons.append("long_lower_wick")
        if bullish_context:
            score += 5
            reasons.append("ema_context")
        if candle["close"] > support + tolerance * 0.25:
            score += 5
            reasons.append("close_above_support")
        if candle["body_ratio"] <= 0.45:
            score += 7
            reasons.append("controlled_body")
        if score >= MIN_SCORE:
            return {
                "signal": "call",
                "direction": "bullish",
                "score": min(score, 100),
                "entry_quality": min(score, 100),
                "entry_type": "force",
                "blocked": False,
                "reason": ",".join(reasons) + " | N cerrada / ejecución N+1",
                "signal_price": candle["close"],
                "candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
                "analysis": analysis,
            }

    if bearish:
        score = 76
        reasons = ["bearish_resistance_rejection"]
        if candle["upper"] >= candle["body"] * 2.0:
            score += 7
            reasons.append("long_upper_wick")
        if bearish_context:
            score += 5
            reasons.append("ema_context")
        if candle["close"] < resistance - tolerance * 0.25:
            score += 5
            reasons.append("close_below_resistance")
        if candle["body_ratio"] <= 0.45:
            score += 7
            reasons.append("controlled_body")
        if score >= MIN_SCORE:
            return {
                "signal": "put",
                "direction": "bearish",
                "score": min(score, 100),
                "entry_quality": min(score, 100),
                "entry_type": "force",
                "blocked": False,
                "reason": ",".join(reasons) + " | N cerrada / ejecución N+1",
                "signal_price": candle["close"],
                "candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
                "analysis": analysis,
            }

    return _empty("no_valid_rejection_or_score_below_90", analysis)


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado: rechazo soporte/resistencia, score mínimo 90, ejecución N+1")
