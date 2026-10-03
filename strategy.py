"""Funciones compatibles de análisis descriptivo de velas M1. Sin órdenes."""
from datetime import datetime, timezone

TIMEFRAME_SECONDS = 60
MIN_CANDLES = 200


def normalize_candles(candles):
    rows, seen = [], set()
    for c in candles or []:
        try:
            ts = int(float(c.get("from", c.get("timestamp", c.get("at", 0)))))
            o = float(c["open"])
            h = float(c.get("max", c.get("high")))
            l = float(c.get("min", c.get("low")))
            close = float(c["close"])
            if ts <= 0 or h < max(o, l, close) or l > min(o, h, close) or ts in seen:
                continue
            seen.add(ts)
            rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": close})
        except (TypeError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda x: x["timestamp"])


def candle_anatomy(c):
    o, h, l, close = c["open"], c["high"], c["low"], c["close"]
    span = max(h - l, 0.0)
    body = abs(close - o)
    return {
        **c,
        "color": "VERDE" if close > o else "ROJA" if close < o else "DOJI",
        "body": body,
        "range": span,
        "upper_wick": max(0.0, h - max(o, close)),
        "lower_wick": max(0.0, min(o, close) - l),
        "body_ratio_pct": body / span * 100 if span else 0.0,
        "close_position_pct": (close - l) / span * 100 if span else 50.0,
    }


def describe_history(candles):
    items = [candle_anatomy(c) for c in normalize_candles(candles)]
    greens = sum(x["color"] == "VERDE" for x in items)
    reds = sum(x["color"] == "ROJA" for x in items)
    dojis = len(items) - greens - reds
    return {
        "count": len(items), "candles": items,
        "sequence": " ".join("V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D" for x in items),
        "greens": greens, "reds": reds, "dojis": dojis,
        "net_change": items[-1]["close"] - items[0]["open"] if items else 0.0,
        "highest": max((x["high"] for x in items), default=None),
        "lowest": min((x["low"] for x in items), default=None),
        "ready": len(items) >= MIN_CANDLES,
    }


def format_candle(index, candle):
    x = candle_anatomy(candle)
    dt = datetime.fromtimestamp(x["timestamp"], tz=timezone.utc)
    return (f'{index:03d} | {dt:%Y-%m-%d %H:%M:%S} | {x["color"]} | '
            f'{x["open"]:.6f} | {x["high"]:.6f} | {x["low"]:.6f} | '
            f'{x["close"]:.6f} | {x["body_ratio_pct"]:.1f}% | '
            f'{x["upper_wick"]:.6f} | {x["lower_wick"]:.6f}')
