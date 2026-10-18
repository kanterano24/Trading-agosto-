"""Estrategia M1: presión previa + reversión cerrada + entrada en la vela siguiente.
No garantiza resultados; probar en demo antes de usar dinero real.
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


def analyze_market(raw, current=None, pair=None):
    """Evalúa tres velas de presión y exige una cuarta vela cerrada de reversión.

    Si las tres velas previas presionan hacia abajo, CALL requiere una vela verde
    de reversión con rechazo inferior y cerca del mínimo reciente. PUT es simétrico.
    La señal solo es válida para la apertura de la vela M1 inmediatamente posterior.
    """
    candles = normalize_candles(raw)
    recent = candles[-10:]
    code = {"green": "V", "red": "R", "doji": "D"}
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "score": 0,
        "sequence": [c["color"] for c in recent],
        "sequence_text": "-".join(code[c["color"]] for c in recent),
        "entry_timing": "NEXT_M1_CANDLE_OPEN", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False,
        "reason": "Sin señal: faltan velas cerradas para evaluar presión y reversión.",
    }
    if len(candles) < 4:
        return base

    pressure = candles[-4:-1]
    reversal = candles[-1]
    # Tres velas cerradas más recientes antes de la entrada, incluida la confirmación.
    base["candles_before_entry"] = [dict(c) for c in candles[-3:]]
    base["pressure_candles_data"] = [dict(c) for c in pressure]
    if any(c["color"] == "doji" for c in pressure) or reversal["color"] == "doji":
        base["reason"] = "Sin señal: doji en presión o reversión; se exige confirmación clara."
        return base
    if any(c["range"] <= 0 for c in pressure + [reversal]):
        base["reason"] = "Sin señal: una vela no tiene rango suficiente."
        return base

    reds = sum(c["color"] == "red" for c in pressure)
    greens = sum(c["color"] == "green" for c in pressure)
    first, _, last_pressure = pressure
    pressure_down = reds >= 2 and last_pressure["close"] < first["close"]
    pressure_up = greens >= 2 and last_pressure["close"] > first["close"]

    zone = candles[-10:]
    recent_low = min(c["low"] for c in zone)
    recent_high = max(c["high"] for c in zone)
    total_range = max(recent_high - recent_low, 1e-12)
    low_location = max(0.0, min(1.0, 1.0 - (reversal["low"] - recent_low) / total_range))
    high_location = max(0.0, min(1.0, 1.0 - (recent_high - reversal["high"]) / total_range))
    body_ratio = reversal["body"] / reversal["range"]
    lower_ratio = reversal["lower_wick"] / reversal["range"]
    upper_ratio = reversal["upper_wick"] / reversal["range"]
    close_pos = (reversal["close"] - reversal["low"]) / reversal["range"]

    if pressure_down:
        # Filtros obligatorios: no basta con que la puntuación sea alta.
        if reversal["color"] != "green" or lower_ratio < 0.20 or close_pos < 0.60 or low_location < 0.55 or body_ratio < 0.20:
            base.update({"pressure": "BAJISTA", "reason":
                "Presión bajista detectada, pero falta reversión alcista confirmada: vela verde, rechazo inferior y ubicación baja."})
            return base
        pscore = 25 * reds / 3.0
        escore = 25 * min(1.0, lower_ratio / 0.40)
        rscore = 25 * min(1.0, max(0.0, (close_pos - 0.50) / 0.40))
        lscore = 25 * min(1.0, low_location / 0.80)
        score = round(pscore + escore + rscore + lscore)
        base.update({
            "signal": "CALL", "bias": "CALL", "trend": "REVERSIÓN_ALCISTA",
            "call_score": score, "put_score": 0, "score": score, "impulse": True,
            "pressure": "BAJISTA", "pressure_candles": 3,
            "pressure_timestamp": reversal["timestamp"],
            "entry_candle_timestamp": reversal["timestamp"] + 60,
            "location_score": round(lscore), "exhaustion_score": round(escore),
            "rejection_score": round(rscore),
            "reason": (f"CALL confirmado: presión bajista previa + vela verde de reversión; "
                       f"presión={pscore:.0f}, agotamiento={escore:.0f}, rechazo={rscore:.0f}, "
                       f"zona={lscore:.0f}; score={score}/100."),
        })
        return base

    if pressure_up:
        if reversal["color"] != "red" or upper_ratio < 0.20 or close_pos > 0.40 or high_location < 0.55 or body_ratio < 0.20:
            base.update({"pressure": "ALCISTA", "reason":
                "Presión alcista detectada, pero falta reversión bajista confirmada: vela roja, rechazo superior y ubicación alta."})
            return base
        pscore = 25 * greens / 3.0
        escore = 25 * min(1.0, upper_ratio / 0.40)
        rscore = 25 * min(1.0, max(0.0, (0.50 - close_pos) / 0.40))
        lscore = 25 * min(1.0, high_location / 0.80)
        score = round(pscore + escore + rscore + lscore)
        base.update({
            "signal": "PUT", "bias": "PUT", "trend": "REVERSIÓN_BAJISTA",
            "call_score": 0, "put_score": score, "score": score, "impulse": True,
            "pressure": "ALCISTA", "pressure_candles": 3,
            "pressure_timestamp": reversal["timestamp"],
            "entry_candle_timestamp": reversal["timestamp"] + 60,
            "location_score": round(lscore), "exhaustion_score": round(escore),
            "rejection_score": round(rscore),
            "reason": (f"PUT confirmado: presión alcista previa + vela roja de reversión; "
                       f"presión={pscore:.0f}, agotamiento={escore:.0f}, rechazo={rscore:.0f}, "
                       f"zona={lscore:.0f}; score={score}/100."),
        })
        return base

    base["reason"] = "Sin señal: las tres velas previas no muestran presión direccional suficiente."
    return base


def classify_candles(raw):
    return analyze_market(raw).get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
