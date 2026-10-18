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
    """Detecta presión en velas M1 ya cerradas y programa la reversión
    en la apertura de la vela siguiente. No usa la vela actual para inventar
    una confirmación que todavía no existe.

    Se evalúan las tres últimas velas cerradas como contexto de presión. La
    última de ellas debe coincidir con la dirección dominante y mostrar
    agotamiento/rechazo. Tras presión bajista, CALL; tras presión alcista, PUT.
    """
    candles = normalize_candles(raw)
    recent = candles[-8:]
    code = {"green": "V", "red": "R", "doji": "D"}
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "score": 0,
        "sequence": [c["color"] for c in recent],
        "sequence_text": "-".join(code[c["color"]] for c in recent),
        "entry_timing": "NEXT_M1_CANDLE_OPEN", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
        "reason": "Sin señal: faltan velas cerradas para medir la presión.",
    }
    if len(candles) < 3:
        return base

    # Toda la vela de presión debe estar cerrada antes de evaluarla.
    pressure = candles[-3:]
    if any(c["color"] == "doji" for c in pressure):
        base["reason"] = "Sin señal: contexto de presión poco claro por velas doji."
        return base

    reds = sum(c["color"] == "red" for c in pressure)
    greens = sum(c["color"] == "green" for c in pressure)
    last = pressure[-1]
    if last["range"] <= 0 or last["body"] <= 0:
        base["reason"] = "Sin señal: la vela de presión no tiene rango/cuerpo suficiente."
        return base

    body_ratio = last["body"] / last["range"]
    lower_ratio = last["lower_wick"] / last["range"]
    upper_ratio = last["upper_wick"] / last["range"]

    # La presión debe tener consistencia direccional, pero la vela más reciente
    # debe mostrar agotamiento/rechazo en el extremo para justificar contramovimiento.
    down_pressure = reds >= 2 and last["color"] == "red" and pressure[-1]["close"] < pressure[0]["close"]
    up_pressure = greens >= 2 and last["color"] == "green" and pressure[-1]["close"] > pressure[0]["close"]

    if down_pressure:
        # En presión bajista, mecha inferior/rechazo comprador o cuerpo pequeño
        # relativo al rango sugiere pérdida de fuerza. CALL en la vela siguiente.
        exhaustion = lower_ratio >= 0.18 or body_ratio <= 0.55
        if not exhaustion:
            base["pressure"] = "BAJISTA"
            base["reason"] = "Presión bajista presente, pero la vela cerró con fuerza y sin agotamiento suficiente; se evita perseguirla."
            return base
        pressure_score = min(35, 15 + reds * 7)
        exhaustion_score = min(35, int(lower_ratio * 70 + (1 - body_ratio) * 25))
        consistency = min(30, int(abs(pressure[-1]["close"] - pressure[0]["close"]) / max(pressure[-1]["range"], 1e-12) * 10))
        score = max(1, min(100, pressure_score + exhaustion_score + consistency))
        base.update({
            "signal": "CALL", "bias": "CALL", "trend": "REVERSIÓN_ALCISTA",
            "call_score": score, "put_score": 0, "score": score, "impulse": True,
            "pressure": "BAJISTA", "pressure_candles": 3,
            "pressure_timestamp": last["timestamp"],
            "entry_candle_timestamp": last["timestamp"] + 60,
            "reason": (f"Presión bajista evaluada con la vela completa; agotamiento/rechazo detectado. "
                       f"CALL al abrir la siguiente M1. Puntuación {score}/100."),
        })
        return base

    if up_pressure:
        # En presión alcista, mecha superior/rechazo vendedor o cuerpo pequeño
        # relativo al rango sugiere pérdida de fuerza. PUT en la vela siguiente.
        exhaustion = upper_ratio >= 0.18 or body_ratio <= 0.55
        if not exhaustion:
            base["pressure"] = "ALCISTA"
            base["reason"] = "Presión alcista presente, pero la vela cerró con fuerza y sin agotamiento suficiente; se evita perseguirla."
            return base
        pressure_score = min(35, 15 + greens * 7)
        exhaustion_score = min(35, int(upper_ratio * 70 + (1 - body_ratio) * 25))
        consistency = min(30, int(abs(pressure[-1]["close"] - pressure[0]["close"]) / max(pressure[-1]["range"], 1e-12) * 10))
        score = max(1, min(100, pressure_score + exhaustion_score + consistency))
        base.update({
            "signal": "PUT", "bias": "PUT", "trend": "REVERSIÓN_BAJISTA",
            "call_score": 0, "put_score": score, "score": score, "impulse": True,
            "pressure": "ALCISTA", "pressure_candles": 3,
            "pressure_timestamp": last["timestamp"],
            "entry_candle_timestamp": last["timestamp"] + 60,
            "reason": (f"Presión alcista evaluada con la vela completa; agotamiento/rechazo detectado. "
                       f"PUT al abrir la siguiente M1. Puntuación {score}/100."),
        })
        return base

    base["reason"] = "Sin señal: las tres velas cerradas no muestran presión direccional suficiente."
    return base


def classify_candles(raw):
    return analyze_market(raw).get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
