"""Estrategia de REVERSIÓN sobre velas M1 cerradas.
Sin indicadores, sin soporte/resistencia y sin lógica OTC.
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
                    "timestamp": ts,
                    "open": o,
                    "close": cl,
                    "min": lo,
                    "max": hi,
                    "high": hi,
                    "low": lo,
                    "body": body,
                    "range": rng,
                    "lower_wick": lower_wick,
                    "upper_wick": upper_wick,
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
        f"{i}: {color} "
        f"O={a['open']} C={a['close']} "
        f"H={a['high']} L={a['low']} "
        f"Rango={a['range']} Cuerpo={a['body']} "
        f"MechaInf={a['lower_wick']} MechaSup={a['upper_wick']}"
    )


def _reversal_signal(c):
    """
    Reversión puramente por precio:
    - CALL: secuencia reciente bajista y la última vela cerrada gira al alza.
    - PUT: secuencia reciente alcista y la última vela cerrada gira a la baja.

    No usa indicadores, S/R ni rechazo.
    """
    if len(c) < 4:
        return "NEUTRAL"

    a, b, last = c[-3], c[-2], c[-1]

    # Movimiento previo: dos velas consecutivas en la misma dirección.
    prior_bearish = a["close"] < a["open"] and b["close"] < b["open"]
    prior_bullish = a["close"] > a["open"] and b["close"] > b["open"]

    # Giro confirmado por la última vela cerrada.
    bullish_turn = (
        last["color"] == "green"
        and last["close"] > b["close"]
        and last["close"] > last["open"]
    )
    bearish_turn = (
        last["color"] == "red"
        and last["close"] < b["close"]
        and last["close"] < last["open"]
    )

    if prior_bearish and bullish_turn:
        return "CALL"
    if prior_bullish and bearish_turn:
        return "PUT"

    return "NEUTRAL"


def analyze_market(raw):
    c = normalize_candles(raw)

    if len(c) < 4:
        return {
            "signal": "NO SIGNAL",
            "bias": "NEUTRAL",
            "call_score": 0,
            "put_score": 0,
            "reason": f"historial insuficiente {len(c)}/4",
        }

    signal = _reversal_signal(c)

    if signal == "CALL":
        reason = "reversión alcista confirmada por vela M1 cerrada después de dos velas bajistas"
    elif signal == "PUT":
        reason = "reversión bajista confirmada por vela M1 cerrada después de dos velas alcistas"
    else:
        reason = "sin patrón de reversión confirmado"

    return {
        "signal": signal if signal in ("CALL", "PUT") else "NO SIGNAL",
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": 1 if signal == "CALL" else 0,
        "put_score": 1 if signal == "PUT" else 0,
        "reason": reason,
    }
