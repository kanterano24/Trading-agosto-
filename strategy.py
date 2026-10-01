from __future__ import annotations
"""Analisis M1 por anatomia y movimiento. Sin indicadores, S/R ni rechazo.
Modo actual: recoleccion de datos; no genera operaciones.
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
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    body = abs(c - o)
    total = max(h - l, 0.0)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    return {
        "timestamp": int(row["from"]), "open": o, "high": h, "low": l, "close": c,
        "color": candle_color(o, c), "body": body, "range": total,
        "upper_wick": upper, "lower_wick": lower,
        "body_pct": (body / total * 100.0) if total else 0.0,
        "upper_wick_pct": (upper / total * 100.0) if total else 0.0,
        "lower_wick_pct": (lower / total * 100.0) if total else 0.0,
    }

def normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    d = df.copy().rename(columns={"max": "high", "min": "low"})
    req = ["from", "open", "high", "low", "close"]
    if any(c not in d.columns for c in req):
        return pd.DataFrame()
    for c in req:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.dropna(subset=req).drop_duplicates("from").sort_values("from").reset_index(drop=True)

def last_10_closed(df: pd.DataFrame) -> List[Dict[str, Any]]:
    d = normalize(df)
    if len(d) < WINDOW:
        return []
    return [candle_data(r) for _, r in d.iloc[-WINDOW:].iterrows()]

def summarize_window(df: pd.DataFrame) -> Dict[str, Any]:
    candles = last_10_closed(df)
    return {
        "ready": len(candles) == WINDOW,
        "count": len(candles),
        "candles": candles,
        "sequence": " ".join("G" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in candles),
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
