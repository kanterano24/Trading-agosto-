"""Estrategia basada únicamente en el patrón de velas solicitado.

Patrón CALL:
1. Vela verde
2. Vela roja
3. Vela roja
4. Vela verde
5. Vela verde
6. Entrada CALL en la siguiente vela

Solo se analizan velas M1 cerradas.
No se utilizan indicadores, RSI, estructura, S/R, rechazo ni otros filtros.
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

    return {
        "count": len(c),
        "greens": sum(x["color"] == "green" for x in c),
        "reds": sum(x["color"] == "red" for x in c),
        "dojis": sum(x["color"] == "doji" for x in c),
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


def _is_call_pattern(candles):
    """
    Patrón exacto:

        VERDE → ROJA → ROJA → VERDE → VERDE

    Las cinco velas deben estar cerradas.
    Después de confirmar la quinta vela verde,
    la entrada es CALL en la siguiente vela.
    """
    if len(candles) < 5:
        return False

    pattern = candles[-5:]

    return (
        pattern[0]["color"] == "green"
        and pattern[1]["color"] == "red"
        and pattern[2]["color"] == "red"
        and pattern[3]["color"] == "green"
        and pattern[4]["color"] == "green"
    )


def _pattern_signal(candles):
    if _is_call_pattern(candles):
        return "CALL"

    return "NEUTRAL"


def analyze_market(raw):
    candles = normalize_candles(raw)

    if len(candles) < 5:
        return {
            "signal": "NO SIGNAL",
            "bias": "NEUTRAL",
            "call_score": 0,
            "put_score": 0,
            "reason": (
                f"historial insuficiente: {len(candles)}/5 velas cerradas"
            ),
        }

    signal = _pattern_signal(candles)

    if signal == "CALL":
        reason = (
            "CALL | patrón confirmado: "
            "VERDE → ROJA → ROJA → VERDE → VERDE | "
            "entrada CALL en la siguiente vela"
        )
    else:
        recent = "".join(
            "V" if x["color"] == "green"
            else "R" if x["color"] == "red"
            else "D"
            for x in candles[-5:]
        )

        reason = (
            "sin señal | patrón requerido: V-R-R-V-V | "
            f"patrón actual: {recent}"
        )

    return {
        "signal": "CALL" if signal == "CALL" else "NO SIGNAL",
        "bias": "CALL" if signal == "CALL" else "NEUTRAL",
        "call_score": 1 if signal == "CALL" else 0,
        "put_score": 0,
        "reason": reason,
    }
