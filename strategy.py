"""Estrategia M1: vela previa + reversión cerrada; entrada en continuidad.

PUT:
  1) Vela previa verde.
  2) Vela de reversión roja que cierra por debajo del mínimo de la previa.
  3) La entrada PUT se intenta en la vela M1 inmediatamente siguiente
     a la reversión (vela de continuidad, aún en formación).

CALL: patrón contrario: previa roja; reversión verde cierra por encima
 del máximo previo; entrada CALL en la siguiente vela M1.

Solo acción del precio. Sin indicadores, S/R ni rechazo.
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
    """Detecta reversión en las últimas dos velas cerradas.

    La señal se ejecuta durante la vela actual, que es la vela de continuidad.
    """
    candles = normalize_candles(raw)
    if len(candles) < 2:
        sequence = [c["color"] for c in candles]
        return {
            "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
            "put_score": 0,
            "reason": f"Historial insuficiente: {len(candles)}/2 velas cerradas",
            "sequence": sequence,
            "sequence_text": "-".join({"green": "V", "red": "R", "doji": "D"}[x] for x in sequence),
            "entry_timing": "CONTINUATION_CANDLE",
            "entry_candle_timestamp": None,
        }

    previous, reversal = candles[-2:]
    sequence = [previous["color"], reversal["color"]]
    color_code = {"green": "V", "red": "R", "doji": "D"}
    sequence_text = "-".join(color_code[x] for x in sequence)

    put_setup = (
        previous["color"] == "green"
        and reversal["color"] == "red"
        and reversal["close"] < previous["low"]
    )
    call_setup = (
        previous["color"] == "red"
        and reversal["color"] == "green"
        and reversal["close"] > previous["high"]
    )

    if put_setup:
        signal = "PUT"
        reason = ("PUT: vela previa verde; reversión roja cerró por debajo del mínimo "
                  "previo. Entrada en la vela M1 siguiente (continuidad).")
    elif call_setup:
        signal = "CALL"
        reason = ("CALL: vela previa roja; reversión verde cerró por encima del máximo "
                  "previo. Entrada en la vela M1 siguiente (continuidad).")
    else:
        signal = "NO SIGNAL"
        reason = ("Sin señal: se requieren dos velas cerradas; verde + reversión roja "
                  "que cierre bajo el mínimo previo para PUT, o el patrón contrario para CALL.")

    return {
        "signal": signal,
        "bias": signal if signal in ("CALL", "PUT") else "NEUTRAL",
        "call_score": int(signal == "CALL"),
        "put_score": int(signal == "PUT"),
        "reason": reason,
        "sequence": sequence,
        "sequence_text": sequence_text,
        "entry_timing": "CONTINUATION_CANDLE",
        "previous_candle_timestamp": previous["timestamp"],
        "reversal_candle_timestamp": reversal["timestamp"],
        "confirmation_candle_timestamp": reversal["timestamp"],
        "continuation_candle_timestamp": reversal["timestamp"] + 60,
        "entry_candle_timestamp": reversal["timestamp"] + 60,
    }
