from __future__ import annotations

"""Estrategia exclusiva de velas de fuerza M1. Sin indicadores."""
from typing import Any, Dict, Optional
import pandas as pd

M1 = 60
MIN_BARS = 8
BODY_RATIO_MIN = 0.60
BODY_EXPANSION_MIN = 1.20
MODE_CONFIG = {"M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1}}


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    d = df.copy().rename(columns={"max": "high", "min": "low"})
    req = ["open", "high", "low", "close"]
    if any(c not in d.columns for c in req):
        return pd.DataFrame()
    for c in req:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    if "from" in d.columns:
        d["from"] = pd.to_numeric(d["from"], errors="coerce")
        d = d.sort_values("from").drop_duplicates("from")
    return d.dropna(subset=req).reset_index(drop=True)


def _empty(reason="sin señal") -> Dict[str, Any]:
    return {"signal": None, "reason": reason, "mode": "M1_M1", "analysis_timeframe": "M1", "entry_timeframe": "M1", "expiration": 1, "force_candle": False, "analysis": {}}


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 0.0)
    body = abs(c - o)
    return {"open": o, "high": h, "low": l, "close": c, "range": rng, "body": body, "body_ratio": body / rng if rng else 0.0}


def analyze_market(df=None, pair=None, mode="M1_M1", **kwargs):
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}")

    cur = _metrics(data.iloc[-1])
    if cur["close"] > cur["open"]:
        signal, direction = "call", "bullish"
    elif cur["close"] < cur["open"]:
        signal, direction = "put", "bearish"
    else:
        return _empty("vela sin dirección")

    previous_bodies = [_metrics(row)["body"] for _, row in data.iloc[-6:-1].iterrows()]
    baseline = float(pd.Series(previous_bodies).median()) if previous_bodies else 0.0
    ratio_ok = cur["body_ratio"] >= BODY_RATIO_MIN
    expansion_ok = baseline > 0 and cur["body"] >= baseline * BODY_EXPANSION_MIN
    force = ratio_ok and expansion_ok

    analysis = {"direction": direction, "body": cur["body"], "range": cur["range"], "body_ratio": cur["body_ratio"], "baseline_body": baseline, "body_ratio_ok": ratio_ok, "expansion_ok": expansion_ok}
    if not force:
        return {**_empty("vela M1 sin fuerza suficiente"), "analysis": analysis}

    return {"signal": signal, "reason": f"VELA DE FUERZA M1 | {'VERDE → CALL' if signal == 'call' else 'ROJA → PUT'} | cuerpo/rango={cur['body_ratio']:.2f}", "mode": "M1_M1", "analysis_timeframe": "M1", "entry_timeframe": "M1", "expiration": 1, "force_candle": True, "analysis": analysis}


def get_signal(df=None, **kwargs):
    return analyze_market(df=df, **kwargs).get("signal")


def signal(df=None, **kwargs):
    return get_signal(df, **kwargs)
