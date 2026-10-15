"""Estrategia M5 de impulso a favor de la tendencia, solo acción del precio.

Confirma usando 10 velas M5 cerradas y evalúa la entrada durante la undécima vela.
Exige dirección consistente e impulso final; además, la vela de entrada debe
confirmar en tiempo real la misma dirección antes de permitir una orden.
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


def analyze_market(raw, current=None, pair=None):
    """Analiza 10 velas M5 cerradas y prepara una única entrada en la undécima.

    Usa solo acción del precio: dirección neta, cambios entre cierres, impulso
    de la décima vela, mechas y posible agotamiento. No usa indicadores, S/R
    ni rechazo como filtros independientes.
    """
    candles = normalize_candles(raw)
    color_code = {"green": "V", "red": "R", "doji": "D"}
    recent = candles[-10:]
    sequence = [c["color"] for c in recent]
    sequence_text = "-".join(color_code[c] for c in sequence)
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "sequence": sequence, "sequence_text": sequence_text,
        "entry_timing": "ELEVENTH_M5_AFTER_TEN_CLOSED", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
    }
    if len(candles) < 10:
        base["reason"] = f"Historial insuficiente: {len(candles)}/10 velas cerradas"
        return base

    closes = [c["close"] for c in recent]
    changes = [closes[i] - closes[i - 1] for i in range(1, 10)]
    up_count = sum(delta > 0 for delta in changes)
    down_count = sum(delta < 0 for delta in changes)
    flat_count = sum(delta == 0 for delta in changes)
    net_move = closes[-1] - closes[0]
    trend_up = net_move > 0 and up_count >= 6
    trend_down = net_move < 0 and down_count >= 6

    last = recent[-1]
    candle_range = last["range"]
    body_ratio = last["body"] / candle_range if candle_range > 0 else 0.0
    close_position = ((last["close"] - last["low"]) / candle_range) if candle_range > 0 else 0.5
    impulse_up = last["color"] == "green" and body_ratio >= 0.60 and close_position >= 0.75
    impulse_down = last["color"] == "red" and body_ratio >= 0.60 and close_position <= 0.25

    previous_ranges = sorted(c["range"] for c in recent[:-1] if c["range"] > 0)
    median_previous_range = previous_ranges[len(previous_ranges) // 2] if previous_ranges else 0.0
    previous = recent[-2]
    prev_range = previous["range"]
    prev_body_ratio = previous["body"] / prev_range if prev_range > 0 else 0.0
    same_direction_before = (
        (last["color"] == "green" and previous["color"] == "green") or
        (last["color"] == "red" and previous["color"] == "red")
    )
    exhausted = (
        median_previous_range > 0
        and candle_range >= 2.5 * median_previous_range
        and body_ratio >= 0.70
        and same_direction_before
        and prev_body_ratio >= 0.55
    )

    original_signal = "NO SIGNAL"
    if trend_up:
        base["trend"] = "ALCISTA"
        if impulse_up:
            original_signal = "CALL"
    elif trend_down:
        base["trend"] = "BAJISTA"
        if impulse_down:
            original_signal = "PUT"

    base["impulse"] = impulse_up or impulse_down
    base["impulse_timestamp"] = last["timestamp"]
    base["entry_candle_timestamp"] = last["timestamp"] + 300
    base["up_closes"] = up_count
    base["down_closes"] = down_count
    base["flat_closes"] = flat_count
    base["body_ratio"] = body_ratio
    base["close_position"] = close_position

    if original_signal == "NO SIGNAL":
        if not (trend_up or trend_down):
            base["reason"] = (
                "Sin señal: las 10 velas no muestran dirección suficientemente consistente; "
                f"cierres ascendentes {up_count}/9, descendentes {down_count}/9, "
                f"sin cambio {flat_count}/9, variación neta={net_move:.8g}."
            )
        else:
            base["reason"] = (
                f"Sin señal: tendencia {base['trend'].lower()}, pero la décima vela "
                f"no confirma impulso suficiente (cuerpo={body_ratio:.1%}, "
                f"posición de cierre={close_position:.1%})."
            )
        return base

    if exhausted:
        base["reason"] = (
            "Entrada bloqueada: posible agotamiento; la décima vela tiene un rango "
            f"{candle_range / median_previous_range:.2f}x la mediana de las nueve previas "
            "y la vela anterior ya impulsaba en la misma dirección."
        )
        return base

    signal = original_signal
    reason = (
        f"Señal {signal}: contexto de 10 velas con tendencia {base['trend'].lower()}, "
        f"{max(up_count, down_count)}/9 cambios de cierre a favor e impulso en la décima "
        f"(cuerpo={body_ratio:.1%}, cierre en {close_position:.1%} del rango)."
    )

    current_candles = normalize_candles([current]) if current is not None else []
    if not current_candles:
        base["reason"] = reason + " Entrada pendiente: no hay datos válidos de la vela actual."
        return base

    # Mejora de entrada: no basta con que las 10 velas cerradas sugieran dirección.
    # La undécima vela en formación debe mostrar confirmación real en esa misma dirección.
    cur = current_candles[-1]
    ranges = sorted(c["range"] for c in recent if c["range"] > 0)
    median_range = ranges[len(ranges) // 2] if ranges else 0.0
    cur_ratio = cur["body"] / cur["range"] if cur["range"] > 0 else 0.0
    cur_position = ((cur["close"] - cur["low"]) / cur["range"]) if cur["range"] > 0 else 0.5
    min_current_range = 0.20 * median_range
    enough_live_movement = (
        median_range > 0
        and cur["range"] >= min_current_range
        and cur_ratio >= 0.55
    )
    aligned_call = (
        cur["color"] == "green"
        and cur_position >= 0.70
        and enough_live_movement
    )
    aligned_put = (
        cur["color"] == "red"
        and cur_position <= 0.30
        and enough_live_movement
    )
    aligned = aligned_call if signal == "CALL" else aligned_put

    base["entry_candle_color"] = cur["color"]
    base["entry_candle_body_ratio"] = cur_ratio
    base["entry_candle_close_position"] = cur_position
    base["entry_candle_range"] = cur["range"]
    base["entry_minimum_range"] = min_current_range

    if not aligned:
        direction_label = "alcista/verde" if signal == "CALL" else "bajista/roja"
        base["reason"] = (
            f"Entrada no confirmada: las 10 velas cerradas sugieren {signal}, pero la "
            f"undécima aún no confirma una vela {direction_label} con fuerza. "
            f"Color actual={cur['color']}, cuerpo={cur_ratio:.1%}, "
            f"posición de cierre={cur_position:.1%}, rango actual={cur['range']:.8g}, "
            f"rango mínimo={min_current_range:.8g}. Se espera sin enviar orden."
        )
        return base

    base["signal"] = signal
    base["bias"] = signal
    base["call_score"] = int(signal == "CALL")
    base["put_score"] = int(signal == "PUT")
    base["reason"] = reason
    return base
