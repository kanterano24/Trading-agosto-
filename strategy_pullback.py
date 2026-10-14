"""Estrategia M1 de acción del precio: tendencia + retroceso + rechazo + confirmación.

No utiliza indicadores técnicos. La dirección de las últimas 10 velas cerradas
solo establece el sesgo; no dispara una entrada por sí sola. La estrategia
mantiene un estado por par para esperar, sin límite de velas, un retesteo de
una zona estructural, una vela de rechazo cerrada y la ruptura confirmatoria
durante la vela M1 siguiente. Una estructura rota cancela el setup.
"""
from __future__ import annotations

from typing import Any


def _finite(value):
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except (TypeError, ValueError):
        return False


def normalize_candles(raw):
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
            rng = high - low
            body = abs(cl - op)
            out.append({
                "timestamp": ts, "open": op, "close": cl,
                "min": low, "max": high, "low": low, "high": high,
                "range": rng, "body": body,
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
    code = {"green": "V", "red": "R", "doji": "D"}[c["color"]]
    return (f"{index}: {code} O={c['open']} C={c['close']} H={c['high']} L={c['low']} "
            f"Rango={c['range']} Cuerpo={c['body']} "
            f"MechaInf={c['lower_wick']} MechaSup={c['upper_wick']}")


# Estado persistente por instrumento: permite esperar varias velas.
_STATES: dict[str, dict[str, Any]] = {}


def _trend(candles):
    """Devuelve sesgo direccional si los cierres tienen dirección suficiente."""
    recent = candles[-10:]
    if len(recent) < 10:
        return None, 0, 0
    closes = [c["close"] for c in recent]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    up = sum(x > 0 for x in deltas)
    down = sum(x < 0 for x in deltas)
    net = closes[-1] - closes[0]
    if net > 0 and up >= 5 and up > down:
        return "CALL", up, down
    if net < 0 and down >= 5 and down > up:
        return "PUT", up, down
    return None, up, down


def _rejection(c, direction):
    rng = c["range"]
    if rng <= 0:
        return False
    body_ratio = c["body"] / rng
    close_pos = (c["close"] - c["low"]) / rng
    if direction == "PUT":
        # Rechazo bajista: la vela toca la zona, cierra roja y cierra en su tercio inferior.
        return c["color"] == "red" and body_ratio >= 0.18 and close_pos <= 0.42
    # Rechazo alcista: toca la zona, cierra verde y cierra en su tercio superior.
    return c["color"] == "green" and body_ratio >= 0.18 and close_pos >= 0.58


def analyze_market(raw, current=None, pair="DEFAULT"):
    """Analiza velas cerradas y vela actual en formación.

    La señal solo aparece si:
    1) el sesgo de las últimas 10 velas es consistente;
    2) el precio retestea una zona basada en extremos recientes;
    3) una vela cerrada rechaza la zona;
    4) la vela actual confirma rompiendo el extremo de esa vela de rechazo.
    """
    candles = normalize_candles(raw)
    color_code = {"green": "V", "red": "R", "doji": "D"}
    recent = candles[-10:]
    sequence = [c["color"] for c in recent]
    base = {
        "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0, "put_score": 0,
        "sequence": sequence, "sequence_text": "-".join(color_code[x] for x in sequence),
        "entry_timing": "RETEST_REJECTION_CONFIRMATION", "entry_candle_timestamp": None,
        "trend": "NEUTRAL", "impulse": False, "setup_state": "WAITING",
    }
    if len(candles) < 10:
        base["reason"] = f"Historial insuficiente: {len(candles)}/10 velas cerradas."
        return base

    direction, up_count, down_count = _trend(candles)
    base["up_closes"] = up_count
    base["down_closes"] = down_count
    base["trend"] = "ALCISTA" if direction == "CALL" else "BAJISTA" if direction == "PUT" else "NEUTRAL"
    if direction is None:
        existing = _STATES.get(str(pair))
        # Una pausa o retroceso corto no borra un setup válido. Solo una tendencia
        # claramente opuesta cancela el sesgo anterior.
        if existing and not (
            (existing.get("direction") == "CALL" and down_count >= 7) or
            (existing.get("direction") == "PUT" and up_count >= 7)
        ):
            direction = existing["direction"]
        else:
            if existing:
                _STATES.pop(str(pair), None)
            base["reason"] = (
                f"Sin entrada: contexto sin dirección clara ({up_count}/9 cierres ascendentes, "
                f"{down_count}/9 descendentes). Se sigue recopilando información."
            )
            return base

    # Zona de referencia: extremos de las velas anteriores a las dos últimas,
    # evitando que el último impulso por sí solo desplace la zona.
    history_for_zone = candles[-12:-2] if len(candles) >= 12 else candles[:-2]
    if len(history_for_zone) < 3:
        history_for_zone = candles[-10:-1]
    if not history_for_zone:
        base["reason"] = "Sin referencia estructural suficiente."
        return base

    if direction == "PUT":
        level = max(c["high"] for c in history_for_zone)
        opposite_level = min(c["low"] for c in candles[-10:])
    else:
        level = min(c["low"] for c in history_for_zone)
        opposite_level = max(c["high"] for c in candles[-10:])

    # Ancho adaptativo derivado del rango mediano de velas cerradas; no es indicador.
    ranges = sorted(c["range"] for c in candles[-10:] if c["range"] > 0)
    median_range = ranges[len(ranges)//2] if ranges else 0.0
    zone_width = max(median_range * 0.22, abs(level) * 1e-7)

    state = _STATES.get(str(pair))
    latest_closed = candles[-1]
    if state and state.get("direction") != direction:
        state = None
    if state is None:
        state = {
            "direction": direction, "level": level, "zone_width": zone_width,
            "last_closed_timestamp": None, "rejection": None, "bars_waited": 0,
            "invalidated": False,
        }
        _STATES[str(pair)] = state
    else:
        # Mantener la zona original mientras se espera; no perseguir el precio.
        state["bars_waited"] += 1

    # Actualizar el estado una sola vez por vela cerrada nueva.
    if state.get("last_closed_timestamp") != latest_closed["timestamp"]:
        state["last_closed_timestamp"] = latest_closed["timestamp"]
        c = latest_closed
        level = float(state["level"])
        width = float(state["zone_width"])
        if direction == "PUT":
            touched = c["high"] >= level - width
            invalid = c["close"] > level + 2.0 * width
        else:
            touched = c["low"] <= level + width
            invalid = c["close"] < level - 2.0 * width

        if invalid:
            # Nivel atravesado y aceptado al otro lado: descartar y esperar estructura nueva.
            _STATES.pop(str(pair), None)
            base["reason"] = (
                f"Setup cancelado: el precio rompió la zona estructural ({level:.10g}). "
                "Se espera una nueva zona y un nuevo retesteo."
            )
            base["setup_state"] = "INVALIDATED"
            return base

        if touched and _rejection(c, direction):
            state["rejection"] = {
                "timestamp": c["timestamp"], "high": c["high"], "low": c["low"],
                "close": c["close"], "color": c["color"],
            }
            state["bars_waited"] = 0
            state["setup_state"] = "REJECTION_CONFIRMED"
        elif state.get("rejection"):
            # La confirmación solo vale inmediatamente después de la vela de rechazo.
            # Si pasa una vela completa sin confirmar, debe esperar otro retesteo.
            if c["timestamp"] > state["rejection"]["timestamp"]:
                state["rejection"] = None
                state["setup_state"] = "WAITING_RETEST"

    base.update({
        "bias": direction, "call_score": int(direction == "CALL"),
        "put_score": int(direction == "PUT"), "zone_level": state["level"],
        "zone_width": state["zone_width"], "setup_state": state.get("setup_state", "WAITING_RETEST"),
        "bars_waited": state.get("bars_waited", 0),
    })

    if state.get("rejection") is None:
        base["reason"] = (
            f"Sesgo {base['trend'].lower()} detectado, pero NO se entra por tendencia sola. "
            f"Esperando retroceso hasta zona {state['level']:.10g} y rechazo confirmado; "
            "la espera puede durar varias velas."
        )
        return base

    current_candles = normalize_candles([current]) if current is not None else []
    if not current_candles:
        base["reason"] = "Rechazo confirmado; esperando datos de la vela M1 actual para confirmar ruptura."
        return base

    cur = current_candles[-1]
    rejection = state["rejection"]
    if direction == "PUT":
        confirmed = cur["close"] < rejection["low"] and cur["color"] == "red"
    else:
        confirmed = cur["close"] > rejection["high"] and cur["color"] == "green"

    base["entry_candle_timestamp"] = cur["timestamp"]
    base["rejection_candle_timestamp"] = rejection["timestamp"]
    base["confirmation_level"] = rejection["low"] if direction == "PUT" else rejection["high"]
    if not confirmed:
        base["reason"] = (
            f"Zona retesteada y rechazo confirmado; falta confirmación de ruptura "
            f"{'por debajo de' if direction == 'PUT' else 'por encima de'} "
            f"{base['confirmation_level']:.10g}. No se envía orden todavía."
        )
        return base

    base["signal"] = direction
    base["setup_state"] = "ENTRY_CONFIRMED"
    base["reason"] = (
        f"Señal {direction}: sesgo {base['trend'].lower()} + retesteo de zona "
        f"{state['level']:.10g} + rechazo en vela cerrada + ruptura confirmatoria "
        f"del extremo {base['confirmation_level']:.10g}. Entrada en la vela actual."
    )
    # Consumir setup para no repetir la misma entrada.
    state["rejection"] = None
    state["setup_state"] = "TRADE_SIGNAL_USED"
    return base
