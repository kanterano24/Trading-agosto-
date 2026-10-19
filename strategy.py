"""Estrategia M1: confirma presión con 12 velas cerradas y exige reversión.

CALL: presión bajista previa + rechazo en zona baja + vela verde confirmada.
PUT: presión alcista previa + rechazo en zona alta + vela roja confirmada.
La señal solo sirve para la apertura de la vela M1 siguiente. No garantiza resultados.
"""
import math


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def normalize_candles(raw):
    """Normaliza velas IQ Option, elimina inválidas/duplicadas y ordena por tiempo."""
    out = {}
    for candle in raw or []:
        try:
            ts = int(float(candle.get("from", candle.get("at", candle.get("timestamp", 0)))))
            op, cl = float(candle["open"]), float(candle["close"])
            low = float(candle.get("min", candle.get("low", min(op, cl))))
            high = float(candle.get("max", candle.get("high", max(op, cl))))
            if ts <= 0 or not all(_finite(v) for v in (op, cl, low, high)):
                continue
            if high < max(op, cl) or low > min(op, cl) or low > high:
                continue
            rng, body = high - low, abs(cl - op)
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


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def analyze_market(raw, current=None, pair=None):
    """Analiza 12 velas de presión cerradas + una vela de reversión cerrada.

    Exige dirección sostenida en varias velas, cambio neto suficiente, estructura,
    ubicación en extremos del rango reciente y una vela de reversión con rechazo.
    Devuelve señal solo para la vela M1 inmediatamente posterior a la reversión.
    """
    candles = normalize_candles(raw)
    candles = candles[-13:]  # 12 velas de contexto + la última vela de confirmación
    code = {"green": "V", "red": "R", "doji": "D"}
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "score": 0,
        "sequence": [c["color"] for c in candles],
        "sequence_text": "-".join(code[c["color"]] for c in candles),
        "entry_timing": "NEXT_M1_CANDLE_OPEN", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False, "pressure_candles": 12,
        "reason": "Sin señal: se necesitan al menos 12 velas de presión y una de reversión cerradas.",
    }
    if len(candles) < 13:
        return base

    pressure = candles[:-1]  # 12 velas cerradas anteriores a la vela de reversión
    reversal = candles[-1]
    base["candles_before_entry"] = [dict(c) for c in candles[-3:]]
    base["pressure_candles_data"] = [dict(c) for c in pressure]
    if reversal["color"] == "doji" or reversal["range"] <= 0:
        base["reason"] = "Sin señal: la última vela cerrada no confirma reversión con cuerpo y dirección claros."
        return base

    valid_pressure = [c for c in pressure if c["color"] != "doji" and c["range"] > 0]
    if len(valid_pressure) < 10:
        base["reason"] = "Sin señal: demasiados dojis o velas sin rango en el contexto de presión."
        return base

    reds = sum(c["color"] == "red" for c in pressure)
    greens = sum(c["color"] == "green" for c in pressure)
    avg_range = sum(c["range"] for c in valid_pressure) / len(valid_pressure)
    net_move = pressure[-1]["close"] - pressure[0]["close"]
    move_strength = abs(net_move) / max(avg_range, 1e-12)

    # Estructura simple, basada en extremos de bloques tempranos y recientes.
    early = pressure[:6]
    late = pressure[-6:]
    early_high = max(c["high"] for c in early)
    late_high = max(c["high"] for c in late)
    early_low = min(c["low"] for c in early)
    late_low = min(c["low"] for c in late)
    lower_structure = late_high < early_high and late_low < early_low
    higher_structure = late_high > early_high and late_low > early_low

    # Zona local: no operar en el centro del rango. Se usan solo velas ya cerradas.
    zone = candles[-13:]
    recent_low = min(c["low"] for c in zone)
    recent_high = max(c["high"] for c in zone)
    total_range = max(recent_high - recent_low, 1e-12)
    close_pos = _clamp((reversal["close"] - reversal["low"]) / max(reversal["range"], 1e-12))
    body_ratio = _clamp(reversal["body"] / max(reversal["range"], 1e-12))
    lower_ratio = _clamp(reversal["lower_wick"] / max(reversal["range"], 1e-12))
    upper_ratio = _clamp(reversal["upper_wick"] / max(reversal["range"], 1e-12))
    # La vela debe tocar/acercarse al extremo de la zona y cerrar fuera del centro.
    low_location = _clamp(1.0 - (reversal["low"] - recent_low) / total_range)
    high_location = _clamp(1.0 - (recent_high - reversal["high"]) / total_range)

    # Detectar pérdida de impulso en las últimas velas de la presión.
    last_bodies = [c["body"] for c in pressure[-4:]]
    previous_body_avg = sum(c["body"] for c in pressure[-8:-4]) / 4.0
    recent_body_avg = sum(last_bodies) / 4.0
    momentum_fading = recent_body_avg < previous_body_avg * 0.90

    # Umbral de presión: mayoría clara en 12 velas, recorrido neto y apoyo estructural.
    pressure_down = (
        reds >= 7 and net_move < 0 and move_strength >= 0.45
        and (lower_structure or pressure[-1]["close"] < pressure[0]["close"] - 0.35 * avg_range)
    )
    pressure_up = (
        greens >= 7 and net_move > 0 and move_strength >= 0.45
        and (higher_structure or pressure[-1]["close"] > pressure[0]["close"] + 0.35 * avg_range)
    )

    if pressure_down:
        # La presión bajista debe estar perdiendo fuerza y revertir con rechazo inferior.
        if (reversal["color"] != "green" or lower_ratio < 0.16 or close_pos < 0.68
                or low_location < 0.70 or body_ratio < 0.38):
            base.update({"pressure": "BAJISTA", "trend": "PRESIÓN_BAJISTA",
                         "reason": "Presión bajista detectada en 12 velas, pero falta CALL confirmado: se exige rechazo inferior, cierre fuerte y zona baja."})
            return base
        if not momentum_fading and reversal["body"] < 0.60 * avg_range:
            base.update({"pressure": "BAJISTA", "trend": "PRESIÓN_BAJISTA",
                         "reason": "Presión bajista detectada, pero no hay agotamiento suficiente ni una reversión fuerte."})
            return base

        pressure_score = 30 * _clamp((reds - 6) / 6) * _clamp(move_strength / 1.5)
        exhaustion_score = 15 * (1.0 if momentum_fading else _clamp(reversal["body"] / max(avg_range, 1e-12)))
        rejection_score = 20 * _clamp(lower_ratio / 0.30) * _clamp((close_pos - 0.50) / 0.45)
        location_score = 20 * _clamp((low_location - 0.50) / 0.50)
        structure_score = 15 if lower_structure else 8
        score = int(round(_clamp((pressure_score + exhaustion_score + rejection_score + location_score + structure_score), 0, 100)))
        base.update({
            "signal": "CALL", "bias": "CALL", "trend": "REVERSIÓN_ALCISTA",
            "call_score": score, "put_score": 0, "score": score, "impulse": True,
            "pressure": "BAJISTA", "pressure_candles": 12,
            "pressure_directional_candles": reds, "net_pressure_move": net_move,
            "momentum_fading": momentum_fading,
            "pressure_timestamp": reversal["timestamp"],
            "entry_candle_timestamp": reversal["timestamp"] + 60,
            "location_score": int(round(location_score)),
            "exhaustion_score": int(round(exhaustion_score)),
            "rejection_score": int(round(rejection_score)),
            "structure_score": structure_score,
            "reason": (f"CALL: {reds}/12 velas rojas y presión bajista confirmada; "
                       f"reversión verde, rechazo inferior y zona baja. "
                       f"Presión={pressure_score:.1f}/30, agotamiento={exhaustion_score:.1f}/15, "
                       f"rechazo={rejection_score:.1f}/20, zona={location_score:.1f}/20, "
                       f"estructura={structure_score}/15; total={score}/100."),
        })
        return base

    if pressure_up:
        if (reversal["color"] != "red" or upper_ratio < 0.16 or close_pos > 0.32
                or high_location < 0.70 or body_ratio < 0.38):
            base.update({"pressure": "ALCISTA", "trend": "PRESIÓN_ALCISTA",
                         "reason": "Presión alcista detectada en 12 velas, pero falta PUT confirmado: se exige rechazo superior, cierre débil y zona alta."})
            return base
        if not momentum_fading and reversal["body"] < 0.60 * avg_range:
            base.update({"pressure": "ALCISTA", "trend": "PRESIÓN_ALCISTA",
                         "reason": "Presión alcista detectada, pero no hay agotamiento suficiente ni una reversión fuerte."})
            return base

        pressure_score = 30 * _clamp((greens - 6) / 6) * _clamp(move_strength / 1.5)
        exhaustion_score = 15 * (1.0 if momentum_fading else _clamp(reversal["body"] / max(avg_range, 1e-12)))
        rejection_score = 20 * _clamp(upper_ratio / 0.30) * _clamp((0.50 - close_pos) / 0.45)
        location_score = 20 * _clamp((high_location - 0.50) / 0.50)
        structure_score = 15 if higher_structure else 8
        score = int(round(_clamp((pressure_score + exhaustion_score + rejection_score + location_score + structure_score), 0, 100)))
        base.update({
            "signal": "PUT", "bias": "PUT", "trend": "REVERSIÓN_BAJISTA",
            "call_score": 0, "put_score": score, "score": score, "impulse": True,
            "pressure": "ALCISTA", "pressure_candles": 12,
            "pressure_directional_candles": greens, "net_pressure_move": net_move,
            "momentum_fading": momentum_fading,
            "pressure_timestamp": reversal["timestamp"],
            "entry_candle_timestamp": reversal["timestamp"] + 60,
            "location_score": int(round(location_score)),
            "exhaustion_score": int(round(exhaustion_score)),
            "rejection_score": int(round(rejection_score)),
            "structure_score": structure_score,
            "reason": (f"PUT: {greens}/12 velas verdes y presión alcista confirmada; "
                       f"reversión roja, rechazo superior y zona alta. "
                       f"Presión={pressure_score:.1f}/30, agotamiento={exhaustion_score:.1f}/15, "
                       f"rechazo={rejection_score:.1f}/20, zona={location_score:.1f}/20, "
                       f"estructura={structure_score}/15; total={score}/100."),
        })
        return base

    base["reason"] = (
        f"Sin señal: no hay presión suficientemente consistente en 12 velas "
        f"(rojas={reds}, verdes={greens}, movimiento neto={net_move:.6g}, "
        f"fuerza={move_strength:.2f} rangos medios)."
    )
    base["pressure"] = "BAJISTA" if net_move < 0 else "ALCISTA" if net_move > 0 else "NEUTRAL"
    return base


def classify_candles(raw):
    return analyze_market(raw).get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
