"""Estrategia M1 de acción del precio: ruptura fuerte + continuación + confirmación.

PUT (4 velas CERRADAS, en orden cronológico):
  1) Roja.
  2) Verde fuerte: cierra por encima del máximo de la roja anterior.
  3) Verde fuerte: cierra por encima del máximo de la verde anterior.
  4) Roja de confirmación.
  La entrada PUT se intenta en la vela M1 siguiente, en el segundo 59.

CALL (inverso):
  1) Verde.
  2) Roja fuerte: cierra por debajo del mínimo de la verde anterior.
  3) Roja fuerte: cierra por debajo del mínimo de la roja anterior.
  4) Verde de confirmación.
  La entrada CALL se intenta en la vela M1 siguiente, en el segundo 59.

No utiliza indicadores, soportes/resistencias ni reglas de rechazo.
"""

# Proporción mínima del rango total que debe ocupar el cuerpo para considerar
# una vela "fuerte". 0.50 significa que el cuerpo es al menos el 50 % del rango.
MIN_BODY_RANGE_RATIO = 0.50


def normalize_candles(raw):
    """Normaliza velas de IQ Option y elimina timestamps duplicados."""
    out = []
    for candle in raw or []:
        try:
            ts = int(candle.get("from", candle.get("at", candle.get("timestamp", 0))))
            open_price = float(candle["open"])
            close_price = float(candle["close"])
            low = float(candle.get("min", candle.get("low", min(open_price, close_price))))
            high = float(candle.get("max", candle.get("high", max(open_price, close_price))))

            values = (open_price, close_price, low, high)
            if ts <= 0 or not all(map(_is_finite, values)):
                continue
            if high < max(open_price, close_price) or low > min(open_price, close_price):
                continue

            body = abs(close_price - open_price)
            candle_range = max(0.0, high - low)
            color = "green" if close_price > open_price else "red" if close_price < open_price else "doji"
            out.append({
                "timestamp": ts,
                "open": open_price,
                "close": close_price,
                "min": low,
                "max": high,
                "low": low,
                "high": high,
                "body": body,
                "range": candle_range,
                "lower_wick": min(open_price, close_price) - low,
                "upper_wick": high - max(open_price, close_price),
                "color": color,
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    deduplicated = {c["timestamp"]: c for c in sorted(out, key=lambda item: item["timestamp"])}
    return list(deduplicated.values())


def _is_finite(value):
    """Comprueba números finitos sin depender de librerías externas."""
    return value == value and value not in (float("inf"), float("-inf"))


def describe_history(raw):
    candles = normalize_candles(raw)
    greens = sum(c["color"] == "green" for c in candles)
    reds = sum(c["color"] == "red" for c in candles)
    return {
        "count": len(candles),
        "greens": greens,
        "reds": reds,
        "dojis": len(candles) - greens - reds,
    }


def format_candle(index, candle):
    normalized = normalize_candles([candle])
    if not normalized:
        return f"{index}: inválida"
    c = normalized[0]
    color = {"green": "V", "red": "R", "doji": "D"}[c["color"]]
    return (
        f"{index}: {color} O={c['open']} C={c['close']} H={c['high']} L={c['low']} "
        f"Rango={c['range']} Cuerpo={c['body']} "
        f"MechaInf={c['lower_wick']} MechaSup={c['upper_wick']}"
    )


def _strong(candle):
    """Una vela tiene fuerza si su cuerpo ocupa al menos el 50 % de su rango."""
    candle_range = candle["range"]
    if candle_range <= 0:
        return False
    return candle["body"] / candle_range >= MIN_BODY_RANGE_RATIO


def analyze_market(raw):
    """Evalúa las últimas cuatro velas cerradas; la entrada es en la M1 siguiente.

    PUT: R, G fuerte que rompe el máximo de R, G fuerte que rompe el máximo
    de la G previa, R confirmatoria.
    CALL: G, R fuerte que rompe el mínimo de G, R fuerte que rompe el mínimo
    de la R previa, G confirmatoria.
    """
    candles = normalize_candles(raw)
    if len(candles) < 4:
        sequence = [c["color"] for c in candles]
        return {
            "signal": "NO SIGNAL",
            "bias": "NEUTRAL",
            "call_score": 0,
            "put_score": 0,
            "reason": f"Historial insuficiente: {len(candles)}/4 velas cerradas",
            "sequence": sequence,
        }

    c1, c2, c3, c4 = candles[-4:]
    sequence = [c["color"] for c in (c1, c2, c3, c4)]
    sequence_labels = ["V" if x == "green" else "R" if x == "red" else "D" for x in sequence]

    # PUT: impulso alcista de dos velas fuertes que supera máximos previos,
    # seguido de una vela roja de confirmación.
    put_setup = (
        c1["color"] == "red"
        and c2["color"] == "green"
        and _strong(c2)
        and c2["close"] > c1["high"]
        and c3["color"] == "green"
        and _strong(c3)
        and c3["close"] > c2["high"]
        and c4["color"] == "red"
    )

    # CALL: patrón exactamente inverso.
    call_setup = (
        c1["color"] == "green"
        and c2["color"] == "red"
        and _strong(c2)
        and c2["close"] < c1["low"]
        and c3["color"] == "red"
        and _strong(c3)
        and c3["close"] < c2["low"]
        and c4["color"] == "green"
    )

    if put_setup:
        signal = "PUT"
        reason = (
            "PUT confirmado: roja inicial + verde fuerte rompe máximo anterior + "
            "segunda verde fuerte rompe máximo de la verde previa + roja confirmatoria. "
            "Entrada en la siguiente M1 (segundo 59)."
        )
    elif call_setup:
        signal = "CALL"
        reason = (
            "CALL confirmado: verde inicial + roja fuerte rompe mínimo anterior + "
            "segunda roja fuerte rompe mínimo de la roja previa + verde confirmatoria. "
            "Entrada en la siguiente M1 (segundo 59)."
        )
    else:
        signal = "NO SIGNAL"
        reason = (
            "No cumple la secuencia completa: se exigen dos velas de ruptura con "
            f"cuerpo >= {MIN_BODY_RANGE_RATIO:.0%} del rango y cierre más allá del "
            "máximo/mínimo previo, más la vela contraria de confirmación."
        )

    return {
        "signal": signal,
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": int(signal == "CALL"),
        "put_score": int(signal == "PUT"),
        "reason": reason,
        "sequence": sequence,
        "sequence_text": "-".join(sequence_labels),
        "strong_body_ratio_min": MIN_BODY_RANGE_RATIO,
    }
