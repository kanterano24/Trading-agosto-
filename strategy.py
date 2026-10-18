"""Estrategia M1 de ruptura + pullback/retest de soporte/resistencia.

Busca niveles horizontales recientes, ruptura con fuerza, retroceso débil hacia
el nivel roto y rechazo a favor de la ruptura. Evita perseguir velas extendidas.
La orden solo se habilita en la vela M1 siguiente y durante sus primeros segundos,
si el precio en formación confirma la dirección. No garantiza resultados.
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


def _median(values):
    values = sorted(v for v in values if v > 0)
    if not values:
        return 0.0
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def _near(a, b, tolerance):
    return abs(a - b) <= tolerance


def _levels(candles, end_index, tolerance):
    """Obtiene niveles horizontales a partir de máximos/mínimos pivote previos."""
    highs, lows = [], []
    # Usar pivotes confirmados, nunca velas posteriores al candidato de ruptura.
    for i in range(1, max(1, end_index - 1)):
        left, cur, right = candles[i - 1], candles[i], candles[i + 1]
        if cur["high"] >= left["high"] and cur["high"] >= right["high"]:
            highs.append(cur["high"])
        if cur["low"] <= left["low"] and cur["low"] <= right["low"]:
            lows.append(cur["low"])
    # Agrupa niveles cercanos para tratar la zona como área, no como precio exacto.
    def cluster(values):
        groups = []
        for value in sorted(values):
            if groups and abs(value - sum(groups[-1]) / len(groups[-1])) <= tolerance:
                groups[-1].append(value)
            else:
                groups.append([value])
        return [sum(g) / len(g) for g in groups if len(g) >= 1]
    return cluster(highs), cluster(lows)


def analyze_market(raw, current=None, pair=None):
    """Devuelve CALL/PUT solo si detecta ruptura, retest/rechazo y confirmación.

    La flecha del ejemplo corresponde al retest del nivel roto: no entrar en la
    vela grande de ruptura ni mientras el precio sigue cayendo hacia el nivel.
    Esperar que la vela cerrada toque la zona y rechace, y que la vela actual
    empiece a confirmar la continuación.
    """
    candles = normalize_candles(raw)
    recent = candles[-10:]
    sequence = [c["color"] for c in recent]
    code = {"green": "V", "red": "R", "doji": "D"}
    sequence_text = "-".join(code[c] for c in sequence)
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "sequence": sequence, "sequence_text": sequence_text,
        "entry_timing": "M1_RETEST_NEXT_CANDLE", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
    }
    if len(recent) < 7:
        base["reason"] = f"Historial insuficiente para S/R y retest: {len(recent)}/7 velas"
        return base

    ranges = [c["range"] for c in recent if c["range"] > 0]
    med_range = _median(ranges)
    if med_range <= 0:
        base["reason"] = "Sin señal: rango de velas insuficiente para medir fuerza y zonas."
        return base
    # Tolerancia adaptada a volatilidad. El nivel es una zona, no una línea exacta.
    tol = max(med_range * 0.22, 1e-12)
    base["entry_candle_timestamp"] = recent[-1]["timestamp"] + 60
    base["zone_tolerance"] = tol

    # El último cierre debe ser la vela de rechazo del retest. Buscamos una ruptura
    # reciente 2-5 velas antes, seguida de un retroceso y rechazo en el nivel.
    last = recent[-1]
    prior = recent[-2]
    candidates = []
    for breakout_i in range(2, len(recent) - 2):
        breakout = recent[breakout_i]
        pre_ranges = [c["range"] for c in recent[max(0, breakout_i - 4):breakout_i] if c["range"] > 0]
        local_med = _median(pre_ranges) or med_range
        brange = breakout["range"]
        bbody_ratio = breakout["body"] / brange if brange else 0
        # Niveles formados antes de la ruptura; no se usan datos futuros para definirlos.
        highs, lows = _levels(recent[:breakout_i + 1], breakout_i, tol)
        for level in highs:
            strong_break = (breakout["close"] > level + tol * 0.15 and
                            breakout["color"] == "green" and bbody_ratio >= 0.48 and
                            brange >= 0.65 * local_med)
            if strong_break:
                candidates.append((breakout_i, "CALL", level, breakout, bbody_ratio))
        for level in lows:
            strong_break = (breakout["close"] < level - tol * 0.15 and
                            breakout["color"] == "red" and bbody_ratio >= 0.48 and
                            brange >= 0.65 * local_med)
            if strong_break:
                candidates.append((breakout_i, "PUT", level, breakout, bbody_ratio))

    valid = []
    for breakout_i, direction, level, breakout, breakout_body in candidates:
        bars_after = len(recent) - 1 - breakout_i
        if bars_after < 2 or bars_after > 5:
            continue
        # Retroceso contra la ruptura débil comparado con el cuerpo de ruptura.
        pullback = recent[breakout_i + 1:-1]
        if not pullback:
            continue
        pullback_bodies = [c["body"] for c in pullback if c["color"] != "doji"]
        weak_pullback = (not pullback_bodies or
                         _median(pullback_bodies) <= max(breakout["body"] * 0.78, med_range * 0.55))
        if not weak_pullback:
            continue
        if direction == "CALL":
            touched = last["low"] <= level + tol and last["low"] >= level - 1.8 * tol
            rejected = (last["close"] > level and last["color"] == "green" and
                        (last["lower_wick"] >= last["body"] * 0.35 or last["close"] >= level + tol * 0.15))
            pullback_did_not_break = min(c["low"] for c in recent[breakout_i + 1:]) >= level - 1.8 * tol
            bias = "ALCISTA"
            # No comprar si ya se extendió demasiado lejos del retest.
            distance = (last["close"] - level) / med_range
        else:
            touched = last["high"] >= level - tol and last["high"] <= level + 1.8 * tol
            rejected = (last["close"] < level and last["color"] == "red" and
                        (last["upper_wick"] >= last["body"] * 0.35 or last["close"] <= level - tol * 0.15))
            pullback_did_not_break = max(c["high"] for c in recent[breakout_i + 1:]) <= level + 1.8 * tol
            bias = "BAJISTA"
            distance = (level - last["close"]) / med_range
        if not (touched and rejected and pullback_did_not_break):
            continue
        if distance > 0.85:
            continue
        # Debilidad del retroceso + rechazo en zona + vela actual a favor = confluencia.
        valid.append((bars_after, direction, level, breakout, breakout_body, distance, bias))

    if not valid:
        base["reason"] = (
            "Sin entrada: no hay una ruptura reciente con fuerza seguida de retroceso débil "
            "y vela cerrada de rechazo en la zona S/R. No perseguir el precio; esperar otro retest. "
            f"Tolerancia de zona={tol:.8g}, rango medio M1={med_range:.8g}."
        )
        base["support_resistance"] = "Sin retest válido"
        return base

    # Preferir el retest más reciente y más cercano a la zona.
    valid.sort(key=lambda x: (x[0], x[5]))
    bars_after, signal, level, breakout, breakout_body, distance, bias = valid[0]
    base.update({
        "trend": bias, "support_resistance": f"{level:.8g} (resistencia rota -> soporte)" if signal == "CALL" else f"{level:.8g} (soporte roto -> resistencia)",
        "level": level, "breakout_timestamp": breakout["timestamp"],
        "retest_timestamp": last["timestamp"], "pullback_bars": bars_after - 1,
        "impulse": True, "distance_to_zone_median_ranges": distance,
        "breakout_body_ratio": breakout_body,
    })

    live_list = normalize_candles([current]) if current is not None else []
    if not live_list:
        base["reason"] = f"Setup {signal} detectado en retest {level:.8g}; falta confirmación de la vela actual."
        return base
    cur = live_list[-1]
    cur_range = cur["range"]
    cur_body_ratio = cur["body"] / cur_range if cur_range > 0 else 0.0
    cur_pos = (cur["close"] - cur["low"]) / cur_range if cur_range > 0 else 0.5
    # Confirmación temprana: exigir movimiento real pero no esperar una vela ya extendida.
    enough_move = cur_range >= 0.08 * med_range and cur_body_ratio >= 0.25
    aligned_call = cur["color"] == "green" and cur_pos >= 0.52 and enough_move
    aligned_put = cur["color"] == "red" and cur_pos <= 0.48 and enough_move
    aligned = aligned_call if signal == "CALL" else aligned_put
    base.update({"entry_candle_color": cur["color"], "entry_candle_body_ratio": cur_body_ratio,
                 "entry_candle_close_position": cur_pos, "entry_candle_range": cur_range})
    if not aligned:
        base["reason"] = (
            f"Retest válido en {level:.8g}, pero todavía no entrar: falta confirmación temprana "
            f"de la vela actual a favor de {signal} (color={cur['color']}, cuerpo={cur_body_ratio:.1%})."
        )
        return base

    base.update({"signal": signal, "bias": signal, "call_score": int(signal == "CALL"),
                 "put_score": int(signal == "PUT"), "reason": (
                     f"{signal} habilitado: ruptura con fuerza, retroceso débil y rechazo confirmado "
                     f"en retest de {level:.8g}; distancia al nivel={distance:.2f} rangos medios; "
                     f"vela actual confirma temprano. Entrada solo al inicio de la vela M1 siguiente."
                 )})
    return base


def classify_candles(raw):
    """Compatibilidad con versiones previas del bot."""
    result = analyze_market(raw)
    return result.get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
