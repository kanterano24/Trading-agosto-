from __future__ import annotations
"""QUANT MODE - analisis descriptivo de velas M1.

Esta etapa es SOLO recoleccion de datos.
No hay indicadores, S/R, rechazo ni ejecucion de operaciones.
"""
from typing import Any, Dict, List, Optional
import pandas as pd

M1 = 60
WINDOW = 10


def candle_color(open_: float, close: float) -> str:
    if close > open_:
        return "VERDE"
    if close < open_:
        return "ROJA"
    return "DOJI"


def candle_data(row: pd.Series) -> Dict[str, Any]:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    body = abs(c - o)
    rng = max(h - l, 0.0)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    return {
        "timestamp": int(row["from"]),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "color": candle_color(o, c),
        "body": body,
        "range": rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "body_pct": body / rng * 100.0 if rng else 0.0,
        "upper_wick_pct": upper / rng * 100.0 if rng else 0.0,
        "lower_wick_pct": lower / rng * 100.0 if rng else 0.0,
    }


def normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    d = df.copy().rename(columns={"max": "high", "min": "low"})
    required = ["from", "open", "high", "low", "close"]
    if any(col not in d.columns for col in required):
        return pd.DataFrame()

    for col in required:
        d[col] = pd.to_numeric(d[col], errors="coerce")

    return (
        d.dropna(subset=required)
        .drop_duplicates("from")
        .sort_values("from")
        .reset_index(drop=True)
    )


def last_10_closed(df: pd.DataFrame) -> List[Dict[str, Any]]:
    d = normalize(df)
    if len(d) < WINDOW:
        return []
    return [candle_data(row) for _, row in d.iloc[-WINDOW:].iterrows()]


def summarize_window(df: pd.DataFrame) -> Dict[str, Any]:
    candles = last_10_closed(df)
    return {
        "ready": len(candles) == WINDOW,
        "count": len(candles),
        "candles": candles,
        "sequence": " ".join(
            "G" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D"
            for c in candles
        ),
    }


def compare_window_to_next(
    previous_10: List[Dict[str, Any]],
    next_candle: Dict[str, Any],
) -> Dict[str, Any]:
    """Compara una ventana anterior con la siguiente vela real."""
    if len(previous_10) != WINDOW:
        return {"ready": False}

    return {
        "ready": True,
        "sequence": " ".join(
            "G" if c.get("color") == "VERDE" else "R" if c.get("color") == "ROJA" else "D"
            for c in previous_10
        ),
        "next_color": next_candle.get("color"),
        "next_open": next_candle.get("open"),
        "next_high": next_candle.get("high"),
        "next_low": next_candle.get("low"),
        "next_close": next_candle.get("close"),
        "next_lower_wick": next_candle.get("lower_wick"),
        "next_upper_wick": next_candle.get("upper_wick"),
    }


def analyze_market(df: Optional[pd.DataFrame] = None, **_: Any) -> Dict[str, Any]:
    return {
        "signal": None,
        "blocked": True,
        "reason": "modo recoleccion: operaciones desactivadas",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "window": summarize_window(df if df is not None else pd.DataFrame()),
    }


def get_signal(df: pd.DataFrame):
    return None


def signal(df: pd.DataFrame):
    return None
