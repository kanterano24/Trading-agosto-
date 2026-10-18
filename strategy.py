"""Estrategia M1 de presión y reversión.

Analiza presión bajista/alcista en velas cerradas y busca una vela de reversión
que confirme el cambio. CALL tras presión bajista; PUT tras presión alcista.
La señal se ejecuta en la vela M1 siguiente. No garantiza resultados.
"""


def _finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def normalize_candles(raw):
    """Normaliza velas IQ Option, elimina inválidas/duplicadas y ordena por tiempo."""
    out = {}
    for candle in raw or []:
        try:
            ts = int(float(candle.get("from", candle.get("at", candle.get("timestamp", 0)))))
            op = float(candle["open"])
            cl = float(candle["close"])
            low = float(candle.get("min", candle.get("low", min(op, cl))))
            high = float(candle.get("max", candle.get("high", max(op, cl))))
            if ts <= 0 or not all(_finite(v) for v in (op, cl, low, high)):
                continue
            if high < max(op, cl) or low > min(op, cl) or low > high:
                continue
            rng = high - low
            body = abs(cl - op)
            out[ts] = {
                "timestamp": ts, "open": op, "close": cl,
                "min": low, "max": high, "low": low, "high": high,
                "range": rng, "body": body,
                "lower_wick": min(op, cl) - low,
                "upper_wick": high - max(op, cl),
                "color": "green" if cl > op else "red" if cl < op else "doji",
            }
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            continue
    return [out[k] for k in sorted(out)]


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


def analyze_market(raw, current=None, pair=None):
    """Evalúa presión de 3 velas y reversión confirmada en la última cerrada.

    La entrada se programa en la vela actual (la siguiente a la reversión),
    con ventana de ejecución en segundos 30-35 del reloj del broker.
    Devuelve puntuación 0-100 para que bot.py escoja el mejor de seis pares.
    """
    candles = normalize_candles(raw)
    recent = candles[-8:]
    code = {"green": "V", "red": "R", "doji": "D"}
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0, "put_score": 0,
        "score": 0, "sequence": [c["color"] for c in recent],
        "sequence_text": "-".join(code[c["color"]] for c in recent),
        "entry_timing": "NEXT_M1_CANDLE_SECOND_30", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
        "reason": "Sin señal: faltan velas para medir presión y reversión.",
    }
    if len(candles) < 4:
        return base

    # Tres velas anteriores deben mostrar presión en una dirección.
    pressure = candles[-4:-1]
    reversal = candles[-1]
    if any(c["color"] == "doji" for c in pressure):
        base["reason"] = "Sin señal: presión poco clara por velas doji."
        return base

    reds = sum(c["color"] == "red" for c in pressure)
    greens = sum(c["color"] == "green" for c in pressure)
    down_pressure = reds >= 2 and pressure[-1]["close"] < pressure[0]["close"]
    up_pressure = greens >= 2 and pressure[-1]["close"] > pressure[0]["close"]
    if not (down_pressure or up_pressure):
        base["reason"] = "Sin señal: las tres velas previas no muestran presión direccional suficiente."
        return base

    if reversal["range"] <= 0 or reversal["body"] <= 0:
        base["reason"] = "Sin señal: vela de reversión sin rango/cuerpo suficiente."
        return base

    # La reversión debe tener cuerpo y cerrar más allá del cierre previo; la mecha
    # en contra de la nueva dirección debe ser moderada, para evitar dojis.
    prior = candles[-2]
    if down_pressure and reversal["color"] == "green":
        close_confirmation = reversal["close"] > prior["close"]
        body_ratio = reversal["body"] / reversal["range"]
        wick_ratio = reversal["upper_wick"] / reversal["range"]
        if close_confirmation and body_ratio >= 0.30 and wick_ratio <= 0.45:
            pressure_strength = min(25, 10 + (reds * 5))
            reversal_strength = min(50, int(25 + body_ratio * 35))
            wick_strength = min(25, int((1 - wick_ratio) * 25))
            score = max(1, min(100, pressure_strength + reversal_strength + wick_strength))
            base.update({
                "signal": "CALL", "bias": "CALL", "trend": "REVERSIÓN_ALCISTA",
                "call_score": score, "put_score": 0, "score": score, "impulse": True,
                "pressure": "BAJISTA", "pressure_candles": 3,
                "reversal_timestamp": reversal["timestamp"],
                "entry_candle_timestamp": reversal["timestamp"] + 60,
                "reason": (f"Presión bajista detectada; vela verde de reversión confirmada. "
                           f"Puntuación {score}/100; CALL en la siguiente vela M1."),
            })
            return base

    if up_pressure and reversal["color"] == "red":
        close_confirmation = reversal["close"] < prior["close"]
        body_ratio = reversal["body"] / reversal["range"]
        wick_ratio = reversal["lower_wick"] / reversal["range"]
        if close_confirmation and body_ratio >= 0.30 and wick_ratio <= 0.45:
            pressure_strength = min(25, 10 + (greens * 5))
            reversal_strength = min(50, int(25 + body_ratio * 35))
            wick_strength = min(25, int((1 - wick_ratio) * 25))
            score = max(1, min(100, pressure_strength + reversal_strength + wick_strength))
            base.update({
                "signal": "PUT", "bias": "PUT", "trend": "REVERSIÓN_BAJISTA",
                "call_score": 0, "put_score": score, "score": score, "impulse": True,
                "pressure": "ALCISTA", "pressure_candles": 3,
                "reversal_timestamp": reversal["timestamp"],
                "entry_candle_timestamp": reversal["timestamp"] + 60,
                "reason": (f"Presión alcista detectada; vela roja de reversión confirmada. "
                           f"Puntuación {score}/100; PUT en la siguiente vela M1."),
            })
            return base

    base["reason"] = "Presión detectada, pero la vela cerrada no confirmó una reversión suficientemente fuerte."
    return base


def classify_candles(raw):
    return analyze_market(raw).get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
