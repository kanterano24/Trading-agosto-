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
    """Patrón de reversión CALL en tres velas M1:
    1) vela roja con mecha inferior > cuerpo y mecha superior pequeña;
    2) vela verde cerrada que confirma el giro alcista;
    3) la vela M1 actual es la vela de entrada, con expiración configurada en bot.py.
    """
    candles = normalize_candles(raw)
    recent = candles[-10:]
    sequence = [c["color"] for c in recent]
    code = {"green": "V", "red": "R", "doji": "D"}
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0,
        "put_score": 0, "sequence": sequence,
        "sequence_text": "-".join(code[c] for c in sequence),
        "entry_timing": "THIRD_M1_CANDLE_SECOND_30",
        "entry_candle_timestamp": None, "trend": "NEUTRAL", "impulse": False,
    }
    if len(candles) < 2:
        base["reason"] = "Sin señal: se necesitan al menos dos velas cerradas para detectar el patrón."
        return base

    rejection = candles[-2]  # primera vela: rechazo bajista por mecha inferior
    confirmation = candles[-1]  # segunda vela: confirmación verde
    base["entry_candle_timestamp"] = confirmation["timestamp"] + 60

    body = rejection["body"]
    lower = rejection["lower_wick"]
    upper = rejection["upper_wick"]
    if rejection["color"] != "red":
        base["reason"] = "Sin señal: la vela de rechazo (primera) no es roja."
        return base
    if body <= 0 or lower <= body:
        base["reason"] = f"Sin señal: mecha inferior ({lower:.8g}) no supera el cuerpo ({body:.8g})."
        return base
    if upper > min(body * 0.35, rejection["range"] * 0.20):
        base["reason"] = f"Sin señal: mecha superior demasiado grande ({upper:.8g})."
        return base
    if confirmation["color"] != "green":
        base["reason"] = "Sin señal: la segunda vela no confirma la reversión con cierre verde."
        return base
    if confirmation["close"] <= rejection["open"]:
        base["reason"] = "Sin señal: la vela verde no recupera el nivel de apertura de la vela roja."
        return base

    # Evita considerar confirmación una vela verde insignificante/doji-like.
    if confirmation["range"] <= 0 or confirmation["body"] < confirmation["range"] * 0.25:
        base["reason"] = "Sin señal: la vela verde confirma con poca fuerza (cuerpo menor al 25% del rango)."
        return base

    base.update({
        "signal": "CALL", "bias": "CALL", "trend": "ALCISTA_POR_REVERSIÓN",
        "call_score": 1, "put_score": 0, "impulse": True,
        "candle_timestamp": rejection["timestamp"],
        "confirmation_timestamp": confirmation["timestamp"],
        "candle_body": body, "lower_wick": lower, "upper_wick": upper,
        "reason": ("CALL habilitado: vela roja con mecha inferior larga, seguida de vela verde "
                   "que confirma la reversión. Entrada en la tercera vela M1."),
    })
    return base

def classify_candles(raw):
    """Compatibilidad con versiones previas del bot."""
    result = analyze_market(raw)
    return result.get("signal", "NO SIGNAL")


def get_signal(df):
    return analyze_market(df).get("signal")


def signal(df):
    return get_signal(df)
