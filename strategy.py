"""Estrategia M1 por secuencia exacta de cuatro velas, sin indicadores.

CALL: VERDE - ROJA - ROJA - VERDE
PUT:  ROJA - VERDE - VERDE - ROJA
La cuarta vela puede estar en formación. El bot debe invocar la señal
únicamente durante el segundo 59 de esa vela.
"""


def normalize_candles(raw):
    """Normaliza velas IQ Option y elimina timestamps duplicados."""
    out = []
    for c in raw or []:
        try:
            ts = int(c.get("from", c.get("at", c.get("timestamp", 0))))
            o = float(c["open"])
            close = float(c["close"])
            low = float(c.get("min", c.get("low", min(o, close))))
            high = float(c.get("max", c.get("high", max(o, close))))
            if ts <= 0 or high < max(o, close) or low > min(o, close):
                continue
            body = abs(close - o)
            candle_range = max(0.0, high - low)
            color = "green" if close > o else "red" if close < o else "doji"
            out.append({
                "timestamp": ts, "open": o, "close": close,
                "min": low, "max": high, "low": low, "high": high,
                "body": body, "range": candle_range,
                "lower_wick": min(o, close) - low,
                "upper_wick": high - max(o, close), "color": color,
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    dedup = {c["timestamp"]: c for c in sorted(out, key=lambda x: x["timestamp"])}
    return list(dedup.values())


def describe_history(raw):
    candles = normalize_candles(raw)
    greens = sum(c["color"] == "green" for c in candles)
    reds = sum(c["color"] == "red" for c in candles)
    return {"count": len(candles), "greens": greens, "reds": reds,
            "dojis": len(candles) - greens - reds}


def format_candle(i, candle):
    candles = normalize_candles([candle])
    if not candles:
        return f"{i}: inválida"
    c = candles[0]
    color = {"green": "V", "red": "R", "doji": "D"}[c["color"]]
    return (f"{i}: {color} O={c['open']} C={c['close']} H={c['high']} L={c['low']} "
            f"Rango={c['range']} Cuerpo={c['body']} "
            f"MechaInf={c['lower_wick']} MechaSup={c['upper_wick']}")


def analyze_market(raw):
    """Analiza las últimas 4 velas recibidas en orden cronológico."""
    candles = normalize_candles(raw)
    if len(candles) < 4:
        return {"signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
                "put_score": 0, "reason": f"historial insuficiente {len(candles)}/4",
                "sequence": [c["color"] for c in candles]}
    sequence = [c["color"] for c in candles[-4:]]
    if sequence == ["green", "red", "red", "green"]:
        signal, reason = "CALL", "secuencia VERDE-ROJA-ROJA-VERDE"
    elif sequence == ["red", "green", "green", "red"]:
        signal, reason = "PUT", "secuencia ROJA-VERDE-VERDE-ROJA"
    else:
        signal, reason = "NO SIGNAL", "sin secuencia exacta de 4 velas"
    return {"signal": signal, "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
            "call_score": int(signal == "CALL"), "put_score": int(signal == "PUT"),
            "reason": reason, "sequence": sequence}
