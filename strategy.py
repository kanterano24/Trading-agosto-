"""Estrategia M1 de acción del precio: ruptura fuerte + confirmación + entrada siguiente.

PUT (4 velas CERRADAS, en orden cronológico):
  1) Roja inicial.
  2) Verde fuerte que cierra por encima del máximo de la roja inicial.
  3) Verde fuerte que cierra por encima del máximo de la verde anterior.
  4) Roja de confirmación bajista: cierra por debajo del mínimo de la tercera vela.
  -> Señal PUT para ejecutar en la vela M1 siguiente.

CALL (patrón inverso):
  1) Verde inicial.
  2) Roja fuerte que cierra por debajo del mínimo de la verde inicial.
  3) Roja fuerte que cierra por debajo del mínimo de la roja anterior.
  4) Verde de confirmación alcista: cierra por encima del máximo de la tercera vela.
  -> Señal CALL para ejecutar en la vela M1 siguiente.

No utiliza indicadores, soportes/resistencias ni reglas de rechazo.
La función analiza únicamente velas cerradas. El bot principal debe programar
la orden en la siguiente vela; esta función no controla el reloj ni envía órdenes.
"""

MIN_BODY_RANGE_RATIO = 0.50


def _is_finite(value):
    """Comprueba que un número sea finito sin dependencias externas."""
    return value == value and value not in (float("inf"), float("-inf"))


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
            if ts <= 0 or not all(_is_finite(v) for v in values):
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
    """True cuando el cuerpo ocupa al menos el 50 % del rango de la vela."""
    candle_range = candle["range"]
    return candle_range > 0 and candle["body"] / candle_range >= MIN_BODY_RANGE_RATIO


def analyze_market(raw):
    """Analiza las últimas cuatro velas cerradas y señala entrada en la siguiente M1."""
    candles = normalize_candles(raw)
    if len(candles) < 4:
        return {
            "signal": "NO SIGNAL",
            "bias": "NEUTRAL",
            "call_score": 0,
            "put_score": 0,
            "reason": f"Historial insuficiente: {len(candles)}/4 velas cerradas",
            "sequence": [c["color"] for c in candles],
            "sequence_text": "-".join("V" if c["color"] == "green" else "R" if c["color"] == "red" else "D" for c in candles),
            "entry_timing": "NEXT_M1",
        }

    c1, c2, c3, c4 = candles[-4:]
    sequence = [c["color"] for c in (c1, c2, c3, c4)]
    sequence_text = "-".join("V" if color == "green" else "R" if color == "red" else "D" for color in sequence)

    # PUT: dos rupturas alcistas fuertes y luego confirmación bajista real.
    put_setup = (
        c1["color"] == "red"
        and c2["color"] == "green" and _strong(c2) and c2["close"] > c1["high"]
        and c3["color"] == "green" and _strong(c3) and c3["close"] > c2["high"]
        and c4["color"] == "red" and c4["close"] < c3["low"]
    )

    # CALL: patrón inverso con confirmación alcista real.
    call_setup = (
        c1["color"] == "green"
        and c2["color"] == "red" and _strong(c2) and c2["close"] < c1["low"]
        and c3["color"] == "red" and _strong(c3) and c3["close"] < c2["low"]
        and c4["color"] == "green" and c4["close"] > c3["high"]
    )

    if put_setup:
        signal = "PUT"
        reason = (
            "PUT confirmado: roja inicial; dos velas verdes fuertes rompen máximos consecutivos; "
            "la cuarta vela roja confirma continuidad bajista al cerrar por debajo del mínimo "
            "de la tercera. Ejecutar en la siguiente vela M1."
        )
    elif call_setup:
        signal = "CALL"
        reason = (
            "CALL confirmado: verde inicial; dos velas rojas fuertes rompen mínimos consecutivos; "
            "la cuarta vela verde confirma continuidad alcista al cerrar por encima del máximo "
            "de la tercera. Ejecutar en la siguiente vela M1."
        )
    else:
        signal = "NO SIGNAL"
        reason = (
            "Sin entrada: se requieren cuatro velas cerradas, dos rupturas con cuerpo >= "
            f"{MIN_BODY_RANGE_RATIO:.0%} del rango y una vela final que confirme con cierre "
            "más allá del extremo de la tercera vela. La entrada, si aparece señal, es en la siguiente M1."
        )

    return {
        "signal": signal,
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": int(signal == "CALL"),
        "put_score": int(signal == "PUT"),
        "reason": reason,
        "sequence": sequence,
        "sequence_text": sequence_text,
        "strong_body_ratio_min": MIN_BODY_RANGE_RATIO,
        "entry_timing": "NEXT_M1",
        "confirmation_candle_timestamp": c4["timestamp"],
        "entry_candle_timestamp": c4["timestamp"] + 60,
    }
