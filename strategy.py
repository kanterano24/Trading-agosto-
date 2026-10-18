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
    """Compara presión, agotamiento y ubicación del precio para elegir reversiones mejores.

    Usa las tres últimas velas M1 cerradas como velas previas a la entrada.
    Una señal solo se habilita para la apertura de la vela siguiente.
    Incluye OHLC de las tres velas en la señal; el bot solo las enviará a Telegram
    si esa señal termina siendo la orden ejecutada.
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

    pressure = candles[-3:]
    # Datos de las tres velas que preceden a la potencial entrada.
    candle_snapshot = [dict(c) for c in pressure]
    base["candles_before_entry"] = candle_snapshot
    if any(c["color"] == "doji" for c in pressure):
        base["reason"] = "Sin señal: contexto de presión poco claro por velas doji."
        return base

    reds = sum(c["color"] == "red" for c in pressure)
    greens = sum(c["color"] == "green" for c in pressure)
    first, middle, last = pressure
    if last["range"] <= 0 or last["body"] <= 0:
        base["reason"] = "Sin señal: la vela de presión no tiene rango/cuerpo suficiente."
        return base

    body_ratio = last["body"] / last["range"]
    lower_ratio = last["lower_wick"] / last["range"]
    upper_ratio = last["upper_wick"] / last["range"]
    down_pressure = reds >= 2 and last["color"] == "red" and last["close"] < first["close"]
    up_pressure = greens >= 2 and last["color"] == "green" and last["close"] > first["close"]

    # Puntuación de ubicación: premia que la vela de presión alcance un extremo
    # reciente, en vez de entrar tras un movimiento ya extendido en mitad del rango.
    location_window = candles[-8:-3] + pressure
    if not location_window:
        location_window = pressure
    recent_low = min(c["low"] for c in location_window)
    recent_high = max(c["high"] for c in location_window)
    total_range = max(recent_high - recent_low, 1e-12)
    low_location = max(0.0, min(1.0, 1.0 - (last["low"] - recent_low) / total_range))
    high_location = max(0.0, min(1.0, 1.0 - (recent_high - last["high"]) / total_range))

    if down_pressure:
        # Para CALL: rechazo inferior real, y el cierre no debe quedar pegado al mínimo.
        close_rejection = (last["close"] - last["low"]) / last["range"]
        exhaustion = lower_ratio >= 0.20 or body_ratio <= 0.50
        if not exhaustion or close_rejection < 0.20:
            base.update({"pressure": "BAJISTA", "reason":
                "Presión bajista sin rechazo inferior suficientemente claro; se evita una entrada tardía."})
            return base
        pressure_score = 25 * (reds / 3.0)
        exhaustion_score = 35 * min(1.0, lower_ratio / 0.45) * (1.0 if body_ratio <= 0.65 else 0.75)
        rejection_score = 20 * min(1.0, close_rejection / 0.60)
        location_score = 20 * low_location
        score = max(1, min(100, round(pressure_score + exhaustion_score + rejection_score + location_score)))
        base.update({
            "signal": "CALL", "bias": "CALL", "trend": "REVERSIÓN_ALCISTA",
            "call_score": score, "put_score": 0, "score": score, "impulse": True,
            "pressure": "BAJISTA", "pressure_candles": 3,
            "pressure_timestamp": last["timestamp"],
            "entry_candle_timestamp": last["timestamp"] + 60,
            "location_score": round(location_score),
            "exhaustion_score": round(exhaustion_score),
            "rejection_score": round(rejection_score),
            "reason": (f"CALL: presión bajista con rechazo inferior y ubicación evaluados; "
                       f"presión={pressure_score:.0f}, agotamiento={exhaustion_score:.0f}, "
                       f"rechazo={rejection_score:.0f}, zona={location_score:.0f}; score={score}/100."),
        })
        return base

    if up_pressure:
        # Para PUT: rechazo superior real, y el cierre no debe quedar pegado al máximo.
        close_rejection = (last["high"] - last["close"]) / last["range"]
        exhaustion = upper_ratio >= 0.20 or body_ratio <= 0.50
        if not exhaustion or close_rejection < 0.20:
            base.update({"pressure": "ALCISTA", "reason":
                "Presión alcista sin rechazo superior suficientemente claro; se evita una entrada tardía."})
            return base
        pressure_score = 25 * (greens / 3.0)
        exhaustion_score = 35 * min(1.0, upper_ratio / 0.45) * (1.0 if body_ratio <= 0.65 else 0.75)
        rejection_score = 20 * min(1.0, close_rejection / 0.60)
        location_score = 20 * high_location
        score = max(1, min(100, round(pressure_score + exhaustion_score + rejection_score + location_score)))
        base.update({
            "signal": "PUT", "bias": "PUT", "trend": "REVERSIÓN_BAJISTA",
            "call_score": 0, "put_score": score, "score": score, "impulse": True,
            "pressure": "ALCISTA", "pressure_candles": 3,
            "pressure_timestamp": last["timestamp"],
            "entry_candle_timestamp": last["timestamp"] + 60,
            "location_score": round(location_score),
            "exhaustion_score": round(exhaustion_score),
            "rejection_score": round(rejection_score),
            "reason": (f"PUT: presión alcista con rechazo superior y ubicación evaluados; "
                       f"presión={pressure_score:.0f}, agotamiento={exhaustion_score:.0f}, "
                       f"rechazo={rejection_score:.0f}, zona={location_score:.0f}; score={score}/100."),
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
