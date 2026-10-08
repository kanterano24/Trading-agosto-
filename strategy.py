"""Estrategia de REVERSIÓN por 3 velas claras + reversión.
Solo precio sobre velas M1 cerradas.
Sin indicadores, sin soporte/resistencia y sin lógica OTC.

Lógica:
- PUT: 3 velas verdes claras consecutivas y después una vela roja de reversión.
- CALL: 3 velas rojas claras consecutivas y después una vela verde de reversión.
- La señal se confirma al cerrar la vela de reversión.
- bot.py ejecuta la operación en la siguiente M1.
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

                color = (
                    "green"
                    if cl > o
                    else "red"
                    if cl < o
                    else "doji"
                )

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

    dedup = {
        c["timestamp"]: c
        for c in sorted(out, key=lambda x: x["timestamp"])
    }

    return list(dedup.values())


def describe_history(raw):
    c = normalize_candles(raw)

    g = sum(x["color"] == "green" for x in c)
    r = sum(x["color"] == "red" for x in c)

    return {
        "count": len(c),
        "greens": g,
        "reds": r,
        "dojis": len(c) - g - r,
    }


def format_candle(i, c):
    x = normalize_candles([c])

    if not x:
        return f"{i}: inválida"

    a = x[0]

    color = (
        "V"
        if a["color"] == "green"
        else "R"
        if a["color"] == "red"
        else "D"
    )

    return (
        f"{i}: {color} "
        f"O={a['open']} C={a['close']} "
        f"H={a['high']} L={a['low']} "
        f"Rango={a['range']} Cuerpo={a['body']} "
        f"MechaInf={a['lower_wick']} MechaSup={a['upper_wick']}"
    )


def _is_clear(c):
    """
    Define una vela clara únicamente por su estructura de precio.

    Una vela es clara cuando:
    - no es doji;
    - su cuerpo representa al menos el 50% de todo su rango.

    No se utilizan indicadores ni niveles externos.
    """
    if c["color"] == "doji":
        return False

    if c["range"] <= 0:
        return False

    return c["body"] / c["range"] >= 0.50


def _three_bullish_candles(a, b, c):
    """
    Tres velas verdes claras con avance de precio.
    """
    return (
        a["color"] == "green"
        and b["color"] == "green"
        and c["color"] == "green"
        and _is_clear(a)
        and _is_clear(b)
        and _is_clear(c)
        and b["close"] > a["close"]
        and c["close"] > b["close"]
    )


def _three_bearish_candles(a, b, c):
    """
    Tres velas rojas claras con avance bajista.
    """
    return (
        a["color"] == "red"
        and b["color"] == "red"
        and c["color"] == "red"
        and _is_clear(a)
        and _is_clear(b)
        and _is_clear(c)
        and b["close"] < a["close"]
        and c["close"] < b["close"]
    )


def _bullish_exhaustion(first, second, third):
    """
    Agotamiento alcista usando únicamente precio.

    La tercera vela todavía es verde, pero pierde fuerza: su cuerpo es
    menor que el de la segunda vela y aparece una mecha superior
    significativa.
    """
    if third["range"] <= 0 or second["body"] <= 0:
        return False

    body_is_smaller = third["body"] < second["body"]
    upper_wick_present = (third["upper_wick"] / third["range"]) >= 0.20

    return body_is_smaller and upper_wick_present


def _bearish_exhaustion(first, second, third):
    """
    Agotamiento bajista usando únicamente precio.

    La tercera vela todavía es roja, pero pierde fuerza: su cuerpo es
    menor que el de la segunda vela y aparece una mecha inferior
    significativa.
    """
    if third["range"] <= 0 or second["body"] <= 0:
        return False

    body_is_smaller = third["body"] < second["body"]
    lower_wick_present = (third["lower_wick"] / third["range"]) >= 0.20

    return body_is_smaller and lower_wick_present


def _real_put_reversal(third, reversal):
    """Reversión bajista real, no un simple retroceso."""
    if not _is_clear(reversal):
        return False

    return (
        reversal["color"] == "red"
        and reversal["close"] < third["open"]
    )


def _real_call_reversal(third, reversal):
    """Reversión alcista real, no un simple retroceso."""
    if not _is_clear(reversal):
        return False

    return (
        reversal["color"] == "green"
        and reversal["close"] > third["open"]
    )


def _reversal_signal(c):
    """
    Patrón completo basado únicamente en precio:

    PUT:  3 verdes claras + agotamiento de la tercera + roja clara
          que cierra por debajo de la apertura de la tercera.

    CALL: 3 rojas claras + agotamiento de la tercera + verde clara
          que cierra por encima de la apertura de la tercera.
    """
    if len(c) < 4:
        return "NEUTRAL"

    first = c[-4]
    second = c[-3]
    third = c[-2]
    reversal = c[-1]

    bullish_impulse = _three_bullish_candles(first, second, third)
    bullish_exhaustion = _bullish_exhaustion(first, second, third)
    put_reversal = _real_put_reversal(third, reversal)

    if bullish_impulse and bullish_exhaustion and put_reversal:
        return "PUT"

    bearish_impulse = _three_bearish_candles(first, second, third)
    bearish_exhaustion = _bearish_exhaustion(first, second, third)
    call_reversal = _real_call_reversal(third, reversal)

    if bearish_impulse and bearish_exhaustion and call_reversal:
        return "CALL"

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

    if signal == "PUT":
        reason = (
            "PUT | 3 velas verdes claras + agotamiento alcista "
            "(tercera pierde cuerpo y muestra mecha superior) + "
            "vela roja clara con reversión real | entrada en la siguiente M1"
        )

    elif signal == "CALL":
        reason = (
            "CALL | 3 velas rojas claras + agotamiento bajista "
            "(tercera pierde cuerpo y muestra mecha inferior) + "
            "vela verde clara con reversión real | entrada en la siguiente M1"
        )

    else:
        reason = (
            "sin patrón confirmado: se requieren 3 velas claras del mismo sentido, "
            "agotamiento de la tercera y una vela contraria clara con reversión real"
        )

    return {
        "signal": signal if signal in ("CALL", "PUT") else "NO SIGNAL",
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": 1 if signal == "CALL" else 0,
        "put_score": 1 if signal == "PUT" else 0,
        "reason": reason,
    }
