"""
strategy.py — análisis de acción del precio M1 para GBPUSD-OTC.

Diseño:
- Usa exclusivamente velas cerradas OHLC.
- No usa indicadores técnicos ni datos futuros.
- Separa contexto, estructura, niveles, impulso y confirmación.
- Devuelve CALL/PUT solo si coinciden varios filtros; en otro caso WAIT.
- No ejecuta operaciones. La integración de ejecución debe hacerse en bot.py
  y validarse primero en PRACTICE.

Importaciones compatibles con el recolector:
    normalize_candles, candle_anatomy, describe_history, format_candle

API de análisis:
    analyze_market(candles) -> dict
"""

from datetime import datetime, timezone

TIMEFRAME_SECONDS = 60
MIN_CANDLES = 30
DEFAULT_CONTEXT = 200

# Umbrales expresados en proporciones del rango reciente, no en pips fijos.
MIN_BODY_RATIO = 0.45
MIN_CLOSE_LOCATION = 0.68
LEVEL_TOLERANCE_FRACTION = 0.12
SWING_LEFT = 2
SWING_RIGHT = 2


def normalize_candles(candles):
    """Normaliza, ordena y elimina duplicados de registros IQ Option."""
    result, seen = [], set()
    for raw in candles or []:
        try:
            ts = int(float(raw.get("from", raw.get("timestamp", raw.get("at", 0)))))
            o = float(raw["open"])
            h = float(raw.get("max", raw.get("high")))
            low = float(raw.get("min", raw.get("low")))
            c = float(raw["close"])
            if ts <= 0 or h < max(o, low, c) or low > min(o, h, c) or ts in seen:
                continue
            seen.add(ts)
            result.append({
                "timestamp": ts, "open": o, "high": h,
                "low": low, "close": c
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(result, key=lambda x: x["timestamp"])


def candle_anatomy(candle):
    """Calcula anatomía de una vela sin alterar el OHLC."""
    o, h, low, c = (float(candle[k]) for k in ("open", "high", "low", "close"))
    span = max(h - low, 0.0)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - low)
    color = "VERDE" if c > o else "ROJA" if c < o else "DOJI"
    return {
        **candle,
        "color": color,
        "range": span,
        "body": body,
        "body_pct": body / span * 100 if span else 0.0,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_wick_pct": upper / span * 100 if span else 0.0,
        "lower_wick_pct": lower / span * 100 if span else 0.0,
        "close_position_pct": (c - low) / span * 100 if span else 50.0,
    }


def describe_history(candles):
    items = [candle_anatomy(c) for c in normalize_candles(candles)]
    greens = sum(x["color"] == "VERDE" for x in items)
    reds = sum(x["color"] == "ROJA" for x in items)
    dojis = len(items) - greens - reds
    return {
        "candles": items,
        "count": len(items),
        "greens": greens,
        "reds": reds,
        "dojis": dojis,
        "sequence": " ".join(
            "V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D"
            for x in items
        ),
        "highest": max((x["high"] for x in items), default=None),
        "lowest": min((x["low"] for x in items), default=None),
        "net_change": items[-1]["close"] - items[0]["open"] if items else 0.0,
    }


def format_candle(index, candle):
    """Formato detallado, compatible con el bot recolector."""
    x = candle_anatomy(candle)
    dt = datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc)
    return (
        f'{index:03d} | {dt:%Y-%m-%d %H:%M:%S} UTC | {x["color"]}\n'
        f'Open: {x["open"]:.6f} | High: {x["high"]:.6f} | '
        f'Low: {x["low"]:.6f} | Close: {x["close"]:.6f}\n'
        f'Rango: {x["range"]:.6f} | Cuerpo: {x["body"]:.6f} '
        f'({x["body_pct"]:.1f}%)\n'
        f'Mecha superior: {x["upper_wick"]:.6f} ({x["upper_wick_pct"]:.1f}%) | '
        f'Mecha inferior: {x["lower_wick"]:.6f} ({x["lower_wick_pct"]:.1f}%)\n'
        f'Posición del cierre en rango: {x["close_position_pct"]:.1f}%'
    )


def _median(values):
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _swing_points(candles):
    """Pivotes confirmados: solo se usan pivotes con velas a ambos lados."""
    highs, lows = [], []
    for i in range(SWING_LEFT, len(candles) - SWING_RIGHT):
        h, low = candles[i]["high"], candles[i]["low"]
        left = candles[i - SWING_LEFT:i]
        right = candles[i + 1:i + 1 + SWING_RIGHT]
        if all(h > x["high"] for x in left + right):
            highs.append((i, h))
        if all(low < x["low"] for x in left + right):
            lows.append((i, low))
    return highs, lows


def _structure_bias(candles):
    highs, lows = _swing_points(candles[-80:])
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL", "No hay suficientes pivotes confirmados"
    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]
    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]
    if higher_high and higher_low:
        return "ALCISTA", "Máximo y mínimo de oscilación ascendentes"
    if lower_high and lower_low:
        return "BAJISTA", "Máximo y mínimo de oscilación descendentes"
    return "NEUTRAL", "Estructura mixta o lateral"


def _recent_levels(candles, window=40):
    sample = candles[-window:]
    return max(x["high"] for x in sample), min(x["low"] for x in sample)


def _classify_last_candle(candles):
    x = candle_anatomy(candles[-1])
    prior_ranges = [c["high"] - c["low"] for c in candles[-11:-1]]
    typical_range = _median(prior_ranges)
    expanded = typical_range > 0 and x["range"] >= 1.25 * typical_range
    if x["body_pct"] >= 65 and expanded and x["close_position_pct"] >= 75:
        return "IMPULSO_ALCISTA"
    if x["body_pct"] >= 65 and expanded and x["close_position_pct"] <= 25:
        return "IMPULSO_BAJISTA"
    if x["body_pct"] <= 30 and (x["upper_wick_pct"] >= 35 or x["lower_wick_pct"] >= 35):
        return "RECHAZO"
    if x["body_pct"] <= 35:
        return "INDECISION"
    return "VELA_MIXTA"


def analyze_market(raw_candles):
    """
    Analiza velas cerradas recientes y devuelve una evaluación conservadora.

    La dirección solo se considera candidata cuando hay estructura definida,
    confirmación de vela y ruptura del extremo de la vela previa. La señal es
    una hipótesis para validación, no una predicción garantizada.
    """
    candles = normalize_candles(raw_candles)
    if len(candles) < MIN_CANDLES:
        return {
            "signal": "WAIT",
            "reason": f"Se requieren al menos {MIN_CANDLES} velas cerradas; recibidas {len(candles)}",
            "candles_used": len(candles),
            "bias": "NEUTRAL",
            "setup": None,
            "confidence_score": 0,
        }

    # Contexto reciente: prioriza la información actual sin descartar la historia recibida.
    context = candles[-min(DEFAULT_CONTEXT, len(candles)):]
    bias, structure_reason = _structure_bias(context)
    last = candle_anatomy(context[-1])
    prev = context[-2]
    resistance, support = _recent_levels(context, 40)
    typical_range = _median([c["high"] - c["low"] for c in context[-11:-1]])
    tolerance = max(typical_range * LEVEL_TOLERANCE_FRACTION, 1e-10)
    candle_type = _classify_last_candle(context)

    # Ruptura de la vela anterior: el cierre debe confirmar, no basta una mecha.
    bullish_break = last["close"] > prev["high"] and last["color"] == "VERDE"
    bearish_break = last["close"] < prev["low"] and last["color"] == "ROJA"

    # Rechazo cerca de niveles: mecha relevante y cierre alejado del extremo rechazado.
    near_support = abs(last["low"] - support) <= tolerance or last["low"] <= support + tolerance
    near_resistance = abs(last["high"] - resistance) <= tolerance or last["high"] >= resistance - tolerance
    bullish_rejection = (
        near_support and last["lower_wick"] >= max(last["body"] * 0.8, tolerance)
        and last["close_position_pct"] >= 60
    )
    bearish_rejection = (
        near_resistance and last["upper_wick"] >= max(last["body"] * 0.8, tolerance)
        and last["close_position_pct"] <= 40
    )

    # Puntaje transparente: no se interpreta como probabilidad de ganar.
    call_score = 0
    put_score = 0
    call_reasons, put_reasons = [], []

    if bias == "ALCISTA":
        call_score += 2
        call_reasons.append("estructura M1 alcista")
    elif bias == "BAJISTA":
        put_score += 2
        put_reasons.append("estructura M1 bajista")

    if bullish_break:
        call_score += 2
        call_reasons.append("cierre verde rompe el máximo de la vela previa")
    if bearish_break:
        put_score += 2
        put_reasons.append("cierre rojo rompe el mínimo de la vela previa")

    if bullish_rejection:
        call_score += 1
        call_reasons.append("rechazo comprador cerca del soporte reciente")
    if bearish_rejection:
        put_score += 1
        put_reasons.append("rechazo vendedor cerca de la resistencia reciente")

    if last["body_pct"] >= 50 and last["close_position_pct"] >= 70 and last["color"] == "VERDE":
        call_score += 1
        call_reasons.append("cuerpo verde amplio y cierre en zona alta")
    if last["body_pct"] >= 50 and last["close_position_pct"] <= 30 and last["color"] == "ROJA":
        put_score += 1
        put_reasons.append("cuerpo rojo amplio y cierre en zona baja")

    # Evita operar contra estructura y evita señales sin ruptura/rechazo.
    signal, reasons = "WAIT", []
    if bias == "ALCISTA" and call_score >= 4 and (bullish_break or bullish_rejection):
        signal, reasons = "CALL", call_reasons
    elif bias == "BAJISTA" and put_score >= 4 and (bearish_break or bearish_rejection):
        signal, reasons = "PUT", put_reasons
    else:
        if bias == "NEUTRAL":
            reasons.append("estructura sin dirección clara")
        if not (bullish_break or bearish_break or bullish_rejection or bearish_rejection):
            reasons.append("falta ruptura confirmada o rechazo válido")
        if signal == "WAIT" and not reasons:
            reasons.append("confirmaciones insuficientes o contradictorias")

    return {
        "signal": signal,
        "reason": "; ".join(reasons),
        "candles_used": len(context),
        "bias": bias,
        "structure_reason": structure_reason,
        "last_candle_type": candle_type,
        "last_candle": last,
        "support": support,
        "resistance": resistance,
        "typical_range_10": typical_range,
        "call_score": call_score,
        "put_score": put_score,
        "confidence_score": max(call_score, put_score),
        "bullish_break": bullish_break,
        "bearish_break": bearish_break,
        "bullish_rejection": bullish_rejection,
        "bearish_rejection": bearish_rejection,
        "setup": signal if signal != "WAIT" else None,
        "note": "Puntaje de filtros, no probabilidad estadística ni garantía de resultado.",
    }
