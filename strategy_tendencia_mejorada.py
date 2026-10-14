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


def analyze_market(raw, current=None):
    """Evalúa 5 velas cerradas y la sexta vela en formación.

    La tendencia no exige cuatro cambios de cierre seguidos: exige dirección neta
    y al menos 3 de los 4 cambios de cierre a favor. La quinta vela debe mostrar
    impulso en esa dirección. Después se invierte la señal (regla vigente).
    Se bloquea la entrada si la sexta vela ya muestra un impulso fuerte contrario
    a la dirección que se ejecutaría.
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
    changes = [closes[i] - closes[i - 1] for i in range(1, 5)]
    up_count = sum(delta > 0 for delta in changes)
    down_count = sum(delta < 0 for delta in changes)
    net_move = closes[-1] - closes[0]
    trend_up = net_move > 0 and up_count >= 3
    trend_down = net_move < 0 and down_count >= 3

    last = recent[-1]
    candle_range = last["range"]
    body_ratio = last["body"] / candle_range if candle_range > 0 else 0.0
    close_position = ((last["close"] - last["low"]) / candle_range) if candle_range > 0 else 0.5
    impulse_up = last["color"] == "green" and body_ratio >= 0.60 and close_position >= 0.75
    impulse_down = last["color"] == "red" and body_ratio >= 0.60 and close_position <= 0.25

    original_signal = "NO SIGNAL"
    if trend_up:
        base["trend"] = "ALCISTA"
        if impulse_up:
            original_signal = "CALL"
    elif trend_down:
        base["trend"] = "BAJISTA"
        if impulse_down:
            original_signal = "PUT"

    if original_signal == "NO SIGNAL":
        if not (trend_up or trend_down):
            base["reason"] = (
                f"Sin señal: dirección neta insuficiente o mixta "
                f"(cierres a favor: alcista {up_count}/4, bajista {down_count}/4)."
            )
        else:
            base["reason"] = (
                f"Sin señal: tendencia {base['trend'].lower()} identificada, pero la quinta vela "
                f"no confirma impulso (cuerpo={body_ratio:.1%}, cierre en rango={close_position:.1%})."
            )
        base["impulse"] = impulse_up or impulse_down
        base["impulse_timestamp"] = last["timestamp"]
        base["entry_candle_timestamp"] = last["timestamp"] + 60
        return base

    # Mantener la regla vigente: invertir la dirección confirmada.
    signal = "PUT" if original_signal == "CALL" else "CALL"
    reason = (
        f"Entrada invertida: la confirmación original fue {original_signal}; "
        f"dirección ejecutada {signal}. Tendencia {base['trend'].lower()}, "
        f"{max(up_count, down_count)}/4 cierres a favor e impulso de la quinta vela "
        f"(cuerpo {body_ratio:.1%}, posición de cierre {close_position:.1%})."
    )

    # Filtro de la sexta vela: bloquear solo si ya hay impulso claro contra la
    # dirección ejecutada. Se compara su rango con el rango mediano de las 5 cerradas.
    current_candles = normalize_candles([current]) if current is not None else []
    if current_candles:
        cur = current_candles[-1]
        ranges = sorted(c["range"] for c in recent if c["range"] > 0)
        median_range = ranges[len(ranges) // 2] if ranges else 0.0
        cur_ratio = cur["body"] / cur["range"] if cur["range"] > 0 else 0.0
        cur_position = ((cur["close"] - cur["low"]) / cur["range"]) if cur["range"] > 0 else 0.5
        strong_enough = median_range > 0 and cur["range"] >= 0.35 * median_range and cur_ratio >= 0.55
        bearish_current = cur["color"] == "red" and cur_position <= 0.25
        bullish_current = cur["color"] == "green" and cur_position >= 0.75
        contradicts = (signal == "CALL" and strong_enough and bearish_current) or (
            signal == "PUT" and strong_enough and bullish_current
        )
        if contradicts:
            base["reason"] = (
                f"Entrada bloqueada: la sexta vela muestra impulso contrario a {signal} "
                f"(cuerpo={cur_ratio:.1%}, rango={cur['range']:.8g}, "
                f"rango mediano previo={median_range:.8g})."
            )
            base["impulse"] = True
            base["impulse_timestamp"] = last["timestamp"]
            base["entry_candle_timestamp"] = last["timestamp"] + 60
            return base

    base["signal"] = signal
    base["bias"] = signal
    base["call_score"] = int(signal == "CALL")
    base["put_score"] = int(signal == "PUT")
    base["impulse"] = True
    base["reason"] = reason
    base["impulse_timestamp"] = last["timestamp"]
    base["entry_candle_timestamp"] = last["timestamp"] + 60
    return base
