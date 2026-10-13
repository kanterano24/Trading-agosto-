"""Estrategia M1 de impulso a favor de la tendencia, solo acción del precio.

Confirma la dirección usando 5 velas M1 cerradas: los 4 cambios entre cierres
deben ser ascendentes o descendentes, y la quinta vela debe ser un impulso
en esa misma dirección. La orden se intenta en la sexta vela M1.
No usa indicadores técnicos, S/R ni rechazo como filtro independiente.
"""


def _finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def normalize_candles(raw):
    """Normaliza velas IQ Option, elimina inválidas/duplicadas y ordena por tiempo."""
    out = []
    for candle in raw or []:
        try:
            ts = int(candle.get("from", candle.get("at", candle.get("timestamp", 0))))
            op = float(candle["open"])
            cl = float(candle["close"])
            low = float(candle.get("min", candle.get("low", min(op, cl))))
            high = float(candle.get("max", candle.get("high", max(op, cl))))
            if ts <= 0 or not all(_finite(v) for v in (op, cl, low, high)):
                continue
            if high < max(op, cl) or low > min(op, cl) or low > high:
                continue
            candle_range = high - low
            body = abs(cl - op)
            out.append({
                "timestamp": ts, "open": op, "close": cl,
                "min": low, "max": high, "low": low, "high": high,
                "range": candle_range, "body": body,
                "lower_wick": min(op, cl) - low,
                "upper_wick": high - max(op, cl),
                "color": "green" if cl > op else "red" if cl < op else "doji",
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


def format_candle(index, candle):
    normalized = normalize_candles([candle])
    if not normalized:
        return f"{index}: inválida"
    c = normalized[0]
    color = {"green": "V", "red": "R", "doji": "D"}[c["color"]]
    return (f"{index}: {color} O={c['open']} C={c['close']} H={c['high']} L={c['low']} "
            f"Rango={c['range']} Cuerpo={c['body']} "
            f"MechaInf={c['lower_wick']} MechaSup={c['upper_wick']}")


def analyze_market(raw):
    """Confirma con 5 velas cerradas y entra durante la sexta vela M1.

    Los 4 cambios entre los cierres de las 5 velas deben apuntar en la misma
    dirección. La quinta vela debe ser un impulso en esa dirección: cuerpo
    >= 60% de su rango y cierre en el 25% extremo correspondiente.
    No utiliza indicadores, S/R ni rechazo como filtro independiente.
    """
    candles = normalize_candles(raw)
    color_code = {"green": "V", "red": "R", "doji": "D"}
    sequence = [c["color"] for c in candles[-5:]]
    sequence_text = "-".join(color_code[c] for c in sequence)
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "sequence": sequence, "sequence_text": sequence_text,
        "entry_timing": "SIXTH_M1_AFTER_FIVE_CLOSED", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
    }
    if len(candles) < 5:
        base["reason"] = f"Historial insuficiente: {len(candles)}/5 velas cerradas"
        return base

    recent = candles[-5:]
    closes = [c["close"] for c in recent]
    rises = all(closes[i] > closes[i - 1] for i in range(1, 5))
    falls = all(closes[i] < closes[i - 1] for i in range(1, 5))
    last = recent[-1]
    candle_range = last["range"]
    body_ratio = (last["body"] / candle_range) if candle_range > 0 else 0.0
    close_position = ((last["close"] - last["low"]) / candle_range) if candle_range > 0 else 0.5
    impulse_up = (
        last["color"] == "green" and body_ratio >= 0.60 and close_position >= 0.75
    )
    impulse_down = (
        last["color"] == "red" and body_ratio >= 0.60 and close_position <= 0.25
    )

    signal = "NO SIGNAL"
    if rises and impulse_up:
        signal = "CALL"
        base["trend"] = "ALCISTA"
        base["reason"] = (
            f"CALL: tendencia alcista confirmada por 5 velas cerradas y 4 cierres ascendentes; "
            f"impulso verde con cuerpo {body_ratio:.1%} del rango y cierre cerca del máximo."
        )
    elif falls and impulse_down:
        signal = "PUT"
        base["trend"] = "BAJISTA"
        base["reason"] = (
            f"PUT: tendencia bajista confirmada por 5 velas cerradas y 4 cierres descendentes; "
            f"impulso rojo con cuerpo {body_ratio:.1%} del rango y cierre cerca del mínimo."
        )
    else:
        if rises:
            base["trend"] = "ALCISTA"
        elif falls:
            base["trend"] = "BAJISTA"
        if not (rises or falls):
            base["reason"] = "Sin señal: las 5 velas cerradas no confirman una dirección con 4 cambios de cierre consecutivos."
        elif not (impulse_up or impulse_down):
            base["reason"] = (
                f"Sin señal: tendencia {base['trend'].lower()}, pero la última vela no cumple "
                f"el criterio de impulso (cuerpo={body_ratio:.1%}, posición de cierre={close_position:.1%})."
            )
        else:
            base["reason"] = "Sin señal: el impulso no coincide con la dirección de la tendencia."

    base["signal"] = signal
    base["bias"] = signal if signal in ("CALL", "PUT") else base["trend"]
    base["call_score"] = int(signal == "CALL")
    base["put_score"] = int(signal == "PUT")
    base["impulse"] = impulse_up or impulse_down
    base["impulse_timestamp"] = last["timestamp"]
    base["entry_candle_timestamp"] = last["timestamp"] + 60
    return base
