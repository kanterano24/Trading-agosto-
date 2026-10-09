"""Estrategia M1 de acción del precio: reversión, continuidad y entrada.

La estrategia usa únicamente velas cerradas y no usa indicadores, S/R ni rechazo.

PUT:
  1) Vela previa verde: movimiento previo alcista.
  2) Vela de reversión roja: cierra por debajo del mínimo de la vela previa.
  3) Vela de continuidad roja: cierra por debajo del mínimo de la vela de reversión.
  -> Señal PUT para la siguiente vela M1.

CALL (patrón inverso):
  1) Vela previa roja: movimiento previo bajista.
  2) Vela de reversión verde: cierra por encima del máximo de la vela previa.
  3) Vela de continuidad verde: cierra por encima del máximo de la vela de reversión.
  -> Señal CALL para la siguiente vela M1.
"""


def _finite(value):
    return value == value and value not in (float("inf"), float("-inf"))


def normalize_candles(raw):
    """Normaliza velas de IQ Option, ordena por tiempo y quita duplicados."""
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
            out.append({
                "timestamp": ts, "open": op, "close": cl,
                "min": low, "max": high, "low": low, "high": high,
                "range": candle_range, "body": abs(cl - op),
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
    """Detecta reversión + continuidad y señala entrada en la siguiente M1."""
    candles = normalize_candles(raw)
    if len(candles) < 3:
        return {
            "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
            "put_score": 0, "reason": f"Historial insuficiente: {len(candles)}/3 velas cerradas",
            "sequence": [c["color"] for c in candles],
            "sequence_text": "-".join({"green": "V", "red": "R", "doji": "D"}[c["color"]] for c in candles),
            "entry_timing": "NEXT_M1",
        }

    previous, reversal, continuation = candles[-3:]
    sequence = [previous["color"], reversal["color"], continuation["color"]]
    sequence_text = "-".join({"green": "V", "red": "R", "doji": "D"}[color] for color in sequence)

    # PUT: movimiento alcista previo, reversión bajista que rompe su mínimo,
    # seguida por continuidad bajista que rompe el mínimo de la reversión.
    put_setup = (
        previous["color"] == "green"
        and reversal["color"] == "red"
        and reversal["close"] < previous["low"]
        and continuation["color"] == "red"
        and continuation["close"] < reversal["low"]
    )

    # CALL: patrón inverso.
    call_setup = (
        previous["color"] == "red"
        and reversal["color"] == "green"
        and reversal["close"] > previous["high"]
        and continuation["color"] == "green"
        and continuation["close"] > reversal["high"]
    )

    if put_setup:
        signal = "PUT"
        reason = ("PUT: vela previa verde; vela roja de reversión cierra bajo el mínimo previo; "
                  "vela roja de continuidad cierra bajo el mínimo de reversión. Entrada PUT en la siguiente M1.")
    elif call_setup:
        signal = "CALL"
        reason = ("CALL: vela previa roja; vela verde de reversión cierra sobre el máximo previo; "
                  "vela verde de continuidad cierra sobre el máximo de reversión. Entrada CALL en la siguiente M1.")
    else:
        signal = "NO SIGNAL"
        reason = ("Sin entrada: se necesitan una vela previa, una vela de reversión que rompa el extremo "
                  "anterior al cierre y una vela de continuidad del mismo color que rompa el extremo "
                  "de la reversión al cierre.")

    return {
        "signal": signal,
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": int(signal == "CALL"),
        "put_score": int(signal == "PUT"),
        "reason": reason,
        "sequence": sequence,
        "sequence_text": sequence_text,
        "entry_timing": "NEXT_M1",
        "reversal_candle_timestamp": reversal["timestamp"],
        "continuation_candle_timestamp": continuation["timestamp"],
        "confirmation_candle_timestamp": continuation["timestamp"],
        "entry_candle_timestamp": continuation["timestamp"] + 60,
    }
