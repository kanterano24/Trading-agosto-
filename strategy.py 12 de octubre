"""strategy.py — Clasificación de velas M1 y generación de señales por acción del precio.
Usa exclusivamente velas cerradas OHLC: sin indicadores técnicos, S/R ni filtro de rechazo.
"""

from datetime import datetime, timezone
from statistics import median

TIMEFRAME_SECONDS = 60
ACTIONABLE_LABELS = {
    "FUERZA", "IMPULSO", "CONTINUIDAD", "RUPTURA ALCISTA", "RUPTURA BAJISTA",
    "REVERSIÓN (ENVOLVENTE)", "POSIBLE REVERSIÓN",
}


def normalize_candles(raw):
    """Normaliza velas de IQ Option, elimina inválidas/duplicadas y ordena por tiempo."""
    out = {}
    for item in raw or []:
        try:
            ts = int(float(item.get("from", item.get("timestamp", item.get("at", 0)))))
            o = float(item["open"])
            h = float(item.get("max", item.get("high")))
            lo = float(item.get("min", item.get("low")))
            c = float(item["close"])
            if ts <= 0 or not all(map(lambda x: x == x and abs(x) != float("inf"), (o, h, lo, c))):
                continue
            if h < max(o, lo, c) or lo > min(o, h, c):
                continue
            out[ts] = {"timestamp": ts, "open": o, "high": h, "low": lo, "close": c}
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            continue
    return [out[k] for k in sorted(out)]


def candle_anatomy(candle):
    o, h, lo, c = (float(candle[k]) for k in ("open", "high", "low", "close"))
    span = max(0.0, h - lo)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - lo)
    color = "VERDE" if c > o else "ROJA" if c < o else "DOJI"
    return {
        **candle, "color": color, "range": span, "body": body,
        "body_pct": body / span * 100 if span else 0.0,
        "upper_wick": upper, "lower_wick": lower,
        "upper_wick_pct": upper / span * 100 if span else 0.0,
        "lower_wick_pct": lower / span * 100 if span else 0.0,
        "close_position_pct": (c - lo) / span * 100 if span else 50.0,
    }


def classify_candles(raw):
    """Clasifica cada vela; las categorías descriptivas pueden coexistir."""
    candles = normalize_candles(raw)
    result = []
    ranges = [c["high"] - c["low"] for c in candles]
    for i, raw_c in enumerate(candles):
        x = candle_anatomy(raw_c)
        prev = candle_anatomy(candles[i - 1]) if i else None
        labels = []
        body_pct, close_pos, rng = x["body_pct"], x["close_position_pct"], x["range"]
        avg_range = median(ranges[max(0, i - 10):i]) if i else 0.0

        if rng == 0 or body_pct <= 10:
            labels.append("DOJI")
        elif body_pct <= 25:
            labels.append("INDECISIÓN")
        elif body_pct <= 35 and max(x["upper_wick_pct"], x["lower_wick_pct"]) >= 45:
            labels.append("DESCANSO")

        if body_pct >= 60 and (close_pos >= 80 or close_pos <= 20):
            labels.append("FUERZA")
        if body_pct >= 55 and (
            (close_pos >= 70 and x["color"] == "VERDE") or
            (close_pos <= 30 and x["color"] == "ROJA")
        ):
            labels.append("IMPULSO")

        if prev:
            prev_body_top, prev_body_bottom = max(prev["open"], prev["close"]), min(prev["open"], prev["close"])
            cur_body_top, cur_body_bottom = max(x["open"], x["close"]), min(x["open"], x["close"])
            if x["high"] <= prev["high"] and x["low"] >= prev["low"]:
                labels.append("INSIDE BAR")
            if x["high"] > prev["high"] and x["low"] < prev["low"]:
                labels.append("OUTSIDE BAR")
            if x["color"] == prev["color"] and x["color"] != "DOJI" and body_pct >= 30:
                labels.append("CONTINUIDAD")

            bullish_engulf = (
                prev["color"] == "ROJA" and x["color"] == "VERDE"
                and cur_body_top >= prev_body_top and cur_body_bottom <= prev_body_bottom
            )
            bearish_engulf = (
                prev["color"] == "VERDE" and x["color"] == "ROJA"
                and cur_body_top >= prev_body_top and cur_body_bottom <= prev_body_bottom
            )
            if bullish_engulf or bearish_engulf:
                labels.append("REVERSIÓN (ENVOLVENTE)")
            elif prev["color"] != x["color"] and x["color"] != "DOJI" and body_pct >= 40:
                labels.append("POSIBLE REVERSIÓN")

            if x["close"] > prev["high"]:
                labels.append("RUPTURA ALCISTA")
            elif x["close"] < prev["low"]:
                labels.append("RUPTURA BAJISTA")

            # El rechazo se informa como descripción, nunca como filtro de entrada.
            if x["upper_wick"] >= max(x["body"] * 1.5, rng * 0.35) and close_pos < 65:
                labels.append("RECHAZO SUPERIOR")
            if x["lower_wick"] >= max(x["body"] * 1.5, rng * 0.35) and close_pos > 35:
                labels.append("RECHAZO INFERIOR")

            if avg_range > 0 and rng >= avg_range * 1.5 and body_pct >= 45:
                labels.append("EXPANSIÓN DE RANGO")
            if avg_range > 0 and rng <= avg_range * 0.55:
                labels.append("RANGO COMPRIMIDO")

        if not labels:
            labels.append("VELA NORMAL")
        x["labels"] = list(dict.fromkeys(labels))
        x["index"] = i + 1
        result.append(x)
    return result


def analyze_market(raw):
    """Evalúa las últimas velas cerradas y devuelve CALL, PUT o NO SIGNAL.

    Para abrir una operación la última vela debe ser direccional y tener al menos
    una etiqueta accionable. La dirección se determina por el color/cierre de la
    vela; no se usa rechazo como filtro independiente.
    """
    items = classify_candles(raw)
    if not items:
        return {"signal": "NO SIGNAL", "reason": "No hay velas cerradas válidas", "candles": []}

    latest = items[-1]
    labels = set(latest["labels"])
    actionable = labels.intersection(ACTIONABLE_LABELS)
    if not actionable:
        return {
            "signal": "NO SIGNAL",
            "reason": "La última vela no tiene una clasificación direccional de entrada.",
            "latest": latest, "candles": items,
        }
    if latest["color"] == "VERDE":
        signal = "call"
    elif latest["color"] == "ROJA":
        signal = "put"
    else:
        signal = "NO SIGNAL"

    if signal == "NO SIGNAL":
        reason = "La última vela es doji/indecisa; no se opera."
    else:
        reason = "Clasificación direccional: " + ", ".join(sorted(actionable))
    return {
        "signal": signal, "reason": reason, "latest": latest,
        "sequence": [x["color"] for x in items], "candles": items,
    }


def describe_history(raw):
    items = classify_candles(raw)
    greens = sum(x["color"] == "VERDE" for x in items)
    reds = sum(x["color"] == "ROJA" for x in items)
    dojis = sum(x["color"] == "DOJI" for x in items)
    label_counts = {}
    for candle in items:
        for label in candle["labels"]:
            label_counts[label] = label_counts.get(label, 0) + 1
    return {
        "candles": items, "count": len(items), "greens": greens, "reds": reds,
        "dojis": dojis,
        "sequence": " ".join("V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D" for x in items),
        "highest": max((x["high"] for x in items), default=None),
        "lowest": min((x["low"] for x in items), default=None),
        "net_change": items[-1]["close"] - items[0]["open"] if items else 0.0,
        "label_counts": label_counts,
    }


def format_candle(index, candle):
    x = candle if "labels" in candle and "body_pct" in candle else candle_anatomy(candle)
    dt = datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc)
    labels = ", ".join(x.get("labels", ["SIN CLASIFICAR"]))
    return (
        f'{index:04d} | {dt:%Y-%m-%d %H:%M:%S} UTC | {x["color"]}\n'
        f'O={x["open"]:.8f} H={x["high"]:.8f} L={x["low"]:.8f} C={x["close"]:.8f}\n'
        f'Rango={x["range"]:.8f} | Cuerpo={x["body"]:.8f} ({x["body_pct"]:.1f}%)\n'
        f'Mecha sup={x["upper_wick"]:.8f} ({x["upper_wick_pct"]:.1f}%) | '
        f'Mecha inf={x["lower_wick"]:.8f} ({x["lower_wick_pct"]:.1f}%)\n'
        f'Tipos: {labels}'
    )
