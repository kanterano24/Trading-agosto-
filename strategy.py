"""
strategy.py
Análisis descriptivo de velas M1 cerradas. No genera órdenes ni señales.
"""
from typing import Any, Dict, List
import pandas as pd

TIMEFRAME_SECONDS = 60
MIN_CANDLES = 100


def normalize_candles(candles: List[dict]) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        try:
            ts = int(float(c.get("from", c.get("timestamp", c.get("at")))))
            o = float(c["open"])
            h = float(c.get("max", c.get("high")))
            l = float(c.get("min", c.get("low")))
            close = float(c["close"])
            if ts > 0 and h >= max(o, l, close) and l <= min(o, h, close):
                rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": close})
        except (TypeError, ValueError, KeyError):
            continue
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
    df = pd.DataFrame(rows).drop_duplicates("timestamp", keep="last")
    return df.sort_values("timestamp").reset_index(drop=True)


def candle_anatomy(row: Any) -> Dict[str, Any]:
    o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
    span = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "timestamp": int(row["timestamp"]),
        "open": o, "high": h, "low": l, "close": c,
        "color": "VERDE" if c > o else "ROJA" if c < o else "DOJI",
        "body": body, "range": h-l,
        "upper_wick": h-max(o,c),
        "lower_wick": min(o,c)-l,
        "body_ratio_pct": body/span*100,
        "close_position_pct": (c-l)/span*100,
    }


def describe_history(candles: List[dict]) -> Dict[str, Any]:
    df = normalize_candles(candles)
    items = [candle_anatomy(r) for _, r in df.iterrows()]
    return {
        "count": len(items),
        "candles": items,
        "sequence": " ".join("V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D" for x in items),
        "ready": len(items) >= MIN_CANDLES,
    }
