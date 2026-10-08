"""Estrategia M1 basada únicamente en una secuencia exacta de 4 velas.

CALL:
    VERDE - ROJA - ROJA - VERDE

PUT:
    ROJA - VERDE - VERDE - ROJA

La cuarta vela es la vela actual en formación. El bot solo permite la
entrada en el segundo 59 de esa cuarta vela.
Sin indicadores, sin S/R y sin rechazo.
"""


def normalize_candles(raw):
    out = []
    for c in raw or []:
        try:
            ts = int(c.get("from", c.get("at", c.get("timestamp", 0))))
            o = float(c["open"])
            cl = float(c["close"])
            lo = float(c.get("min", c.get("low", min(o, cl))))
            hi = float(c.get("max", c.get("high", max(o, cl))))
            if ts > 0 and hi >= max(o, cl) and lo <= min(o, cl):
                body = abs(cl - o)
                rng = max(hi - lo, 0.0)
                lower_wick = min(o, cl) - lo
                upper_wick = hi - max(o, cl)
                color = "green" if cl > o else "red" if cl < o else "doji"
                out.append({
                    "timestamp": ts, "open": o, "close": cl,
                    "min": lo, "max": hi, "high": hi, "low": lo,
                    "body": body, "range": rng,
                    "lower_wick": lower_wick, "upper_wick": upper_wick,
                    "color": color,
                })
        except (KeyError, TypeError, ValueError):
            continue

    dedup = {c["timestamp"]: c for c in sorted(out, key=lambda x: x["timestamp"])}
    return list(dedup.values())


def describe_history(raw):
    c = normalize_candles(raw)
    g = sum(x["color"] == "green" for x in c)
    r = sum(x["color"] == "red" for x in c)
    return {"count": len(c), "greens": g, "reds": r, "dojis": len(c) - g - r}


def format_candle(i, c):
    x = normalize_candles([c])
    if not x:
        return f"{i}: inválida"
    a = x[0]
    color = "V" if a["color"] == "green" else "R" if a["color"] == "red" else "D"
    return (
        f"{i}: {color} O={a['open']} C={a['close']} "
        f"H={a['high']} L={a['low']} Rango={a['range']} "
        f"Cuerpo={a['body']} MechaInf={a['lower_wick']} "
        f"MechaSup={a['upper_wick']}"
    )


def analyze_market(raw):
    c = normalize_candles(raw)
    if len(c) < 4:
        return {
            "signal": "NO SIGNAL", "bias": "NEUTRAL",
            "call_score": 0, "put_score": 0,
            "reason": f"historial insuficiente {len(c)}/4",
        }

    sequence = [x["color"] for x in c[-4:]]

    if sequence == ["green", "red", "red", "green"]:
        signal = "CALL"
        reason = "CALL | secuencia VERDE-ROJA-ROJA-VERDE | entrada en segundo 59"
    elif sequence == ["red", "green", "green", "red"]:
        signal = "PUT"
        reason = "PUT | secuencia ROJA-VERDE-VERDE-ROJA | entrada en segundo 59"
    else:
        signal = "NO SIGNAL"
        reason = "sin secuencia de 4 velas confirmada"

    return {
        "signal": signal,
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": 1 if signal == "CALL" else 0,
        "put_score": 1 if signal == "PUT" else 0,
        "reason": reason,
    }
