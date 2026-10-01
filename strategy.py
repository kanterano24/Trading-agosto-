from __future__ import annotations

"""
strategy.py - Reconocedor M1 por precio y anatomia.

Objetivo:
- Unicamente velas M1 cerradas.
- Sin indicadores.
- Sin soporte/resistencia.
- Sin rechazo.
- Sin datos futuros para generar la prediccion.
- Reconoce estados de mercado a partir de anatomia y contexto.
- Devuelve estado, direccion, calidad, evidencia y prediccion para la
  SIGUIENTE vela M1.
- Compatible con el bot actual: M1, WINDOW, analyze_market, get_signal,
  signal y candle_metrics.

IMPORTANTE:
Esta estrategia es un reconocedor experimental de patrones de precio.
Las etiquetas no representan una garantia de direccion futura.
"""

from typing import Any, Dict, List, Optional
import math
import pandas as pd


M1 = 60
WINDOW = 10

# Umbrales de anatomia. Se usan como reglas de reconocimiento, no como
# indicadores de mercado.
STRONG_BODY = 0.70
MEDIUM_BODY = 0.55
SMALL_BODY = 0.25
VERY_SMALL_BODY = 0.18

# Un impulso extremo tiene cuerpo muy dominante y cierre muy cerca del extremo.
EXTREME_BODY = 0.85
EXTREME_CLOSE = 0.88

# Intraminuto: por debajo de esto se considera muestra pobre.
MIN_INTRABAR_SAMPLES = 120
GOOD_INTRABAR_SAMPLES = 240


def candle_color(open_: float, close: float) -> str:
    if close > open_:
        return "VERDE"
    if close < open_:
        return "ROJA"
    return "DOJI"


def candle_metrics(row: pd.Series) -> Dict[str, Any]:
    """Calcula la anatomia de una vela sin usar indicadores."""
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)

    return {
        "timestamp": (
            int(row["from"])
            if "from" in row and pd.notna(row["from"])
            else None
        ),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "body": body,
        "range": rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        "close_pos": (c - l) / rng,
        "color": candle_color(o, c),
    }


def normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    d = df.copy().rename(columns={"max": "high", "min": "low"})

    required = ["open", "high", "low", "close"]
    if any(c not in d.columns for c in required):
        return pd.DataFrame()

    for c in required:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    if "from" in d.columns:
        d["from"] = pd.to_numeric(d["from"], errors="coerce")
        d = d.dropna(subset=["from"])
        d = d.sort_values("from").drop_duplicates("from")
    else:
        d = d.drop_duplicates()

    return d.dropna(subset=required).reset_index(drop=True)


def _enrich(c: Dict[str, Any]) -> Dict[str, Any]:
    """Añade medidas derivadas sin modificar el significado de la vela."""
    out = dict(c)
    rng = max(float(out["range"]), 1e-12)
    body = float(out["body"])

    out["body_ratio"] = body / rng
    out["wick_ratio"] = (
        float(out["upper_wick"]) + float(out["lower_wick"])
    ) / rng

    # Posicion del cierre: 1 = maximo, 0 = minimo.
    out["close_pos"] = (
        float(out["close"]) - float(out["low"])
    ) / rng

    # Resultado del cierre frente a la apertura.
    out["signed_body"] = (
        float(out["close"]) - float(out["open"])
    )

    return out


def candle_data(row: pd.Series) -> Dict[str, Any]:
    return _enrich(candle_metrics(row))


def last_10_closed(df: pd.DataFrame) -> List[Dict[str, Any]]:
    d = normalize(df)
    if len(d) < WINDOW:
        return []
    return [candle_data(r) for _, r in d.iloc[-WINDOW:].iterrows()]


def summarize_window(df: pd.DataFrame) -> Dict[str, Any]:
    candles = last_10_closed(df)
    seq = " ".join(
        "V" if c["color"] == "VERDE"
        else "R" if c["color"] == "ROJA"
        else "D"
        for c in candles
    )

    return {
        "ready": len(candles) == WINDOW,
        "count": len(candles),
        "candles": candles,
        "sequence": seq,
    }


def _direction(c: Dict[str, Any]) -> int:
    if c["color"] == "VERDE":
        return 1
    if c["color"] == "ROJA":
        return -1
    return 0


def _body_strength(c: Dict[str, Any]) -> str:
    r = c["body_ratio"]
    if r >= EXTREME_BODY:
        return "EXTREMO"
    if r >= STRONG_BODY:
        return "FUERTE"
    if r >= MEDIUM_BODY:
        return "MEDIO"
    if r >= SMALL_BODY:
        return "DEBIL"
    return "MUY_DEBIL"


def _relative_range(last: Dict[str, Any], previous: List[Dict[str, Any]]) -> float:
    ranges = [float(x["range"]) for x in previous if float(x["range"]) > 0]
    if not ranges:
        return 1.0
    baseline = sum(ranges) / len(ranges)
    return float(last["range"]) / max(baseline, 1e-12)


def _context(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    last = candles[-1]

    def count_color(items: List[Dict[str, Any]], color: str) -> int:
        return sum(x["color"] == color for x in items)

    last3 = candles[-3:]
    last5 = candles[-5:]
    last10 = candles[-10:]

    green3 = count_color(last3, "VERDE")
    red3 = count_color(last3, "ROJA")
    green5 = count_color(last5, "VERDE")
    red5 = count_color(last5, "ROJA")
    green10 = count_color(last10, "VERDE")
    red10 = count_color(last10, "ROJA")

    # Cambios de cierre y cuerpo.
    close_changes = [
        float(candles[i]["close"]) - float(candles[i - 1]["close"])
        for i in range(1, len(candles))
    ]

    net_change = (
        float(candles[-1]["close"]) - float(candles[0]["open"])
    )

    avg_range = sum(float(x["range"]) for x in last5) / 5.0
    avg_body = sum(float(x["body"]) for x in last5) / 5.0

    # Dominio de la secuencia reciente.
    recent_direction = 0
    if green3 >= 2 and green3 > red3:
        recent_direction = 1
    elif red3 >= 2 and red3 > green3:
        recent_direction = -1
    elif green5 > red5:
        recent_direction = 1
    elif red5 > green5:
        recent_direction = -1

    return {
        "last": last,
        "last3": last3,
        "last5": last5,
        "last10": last10,
        "green3": green3,
        "red3": red3,
        "green5": green5,
        "red5": red5,
        "green10": green10,
        "red10": red10,
        "close_changes": close_changes,
        "net_change": net_change,
        "avg_range_5": avg_range,
        "avg_body_5": avg_body,
        "recent_direction": recent_direction,
        "range_ratio": _relative_range(last, candles[-6:-1]),
    }


def _intrabar_quality(last: Dict[str, Any]) -> Dict[str, Any]:
    """
    El bot actual añade estas claves al cerrar la vela.
    Si no existen, no se inventan.
    """
    samples = int(last.get("sample_count", 0) or 0)
    up = int(last.get("up_steps", 0) or 0)
    down = int(last.get("down_steps", 0) or 0)
    total = up + down

    if samples >= GOOD_INTRABAR_SAMPLES:
        quality = "BUENA"
    elif samples >= MIN_INTRABAR_SAMPLES:
        quality = "MEDIA"
    elif samples > 0:
        quality = "BAJA"
    else:
        quality = "NO_DISPONIBLE"

    if total:
        balance = abs(up - down) / total
    else:
        balance = 0.0

    if total and balance >= 0.20:
        intradir = "ALCISTA" if up > down else "BAJISTA"
    else:
        intradir = "NEUTRAL"

    return {
        "samples": samples,
        "quality": quality,
        "direction": intradir,
        "balance": balance,
    }


def _classify_state(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Reconoce el estado de la vela actual dentro del contexto reciente.

    Orden de prioridad:
    1. PAUSA / TRANSICION
    2. IMPULSO EXTREMO
    3. CONTINUACION FUERTE
    4. RESPUESTA / RECUPERACION
    5. DESACELERACION
    6. IMPULSO normal
    7. RUIDO
    """
    ctx = _context(candles)
    last = ctx["last"]
    prev = candles[-2]

    d = _direction(last)
    pdirection = _direction(prev)

    body = float(last["body_ratio"])
    close_pos = float(last["close_pos"])
    range_ratio = float(ctx["range_ratio"])

    prev_body = float(prev["body_ratio"])
    prev_range = float(prev["range"])

    # Cambio del cuerpo y del rango respecto a la vela anterior.
    body_change = body - prev_body
    range_change = float(last["range"]) - prev_range

    # Vela de indecision: cuerpo pequeño con rango no trivial.
    if body <= VERY_SMALL_BODY:
        if (
            ctx["green3"] == 1
            and ctx["red3"] == 1
        ) or abs(ctx["net_change"]) <= ctx["avg_range_5"] * 0.35:
            return {
                "state": "PAUSA_TRANSICION",
                "direction": "NEUTRAL",
                "quality": "BAJA",
                "score": 0,
                "evidence": [
                    "cuerpo muy pequeño",
                    "rango sin dirección limpia",
                    "cierre sin dominio claro",
                ],
            }

    # Impulso extremo: cuerpo dominante + cierre cerca del extremo.
    if (
        body >= EXTREME_BODY
        and (
            (d == 1 and close_pos >= EXTREME_CLOSE)
            or (d == -1 and close_pos <= 1.0 - EXTREME_CLOSE)
        )
    ):
        direction = "ALCISTA" if d == 1 else "BAJISTA"
        return {
            "state": f"IMPULSO_{direction}_EXTREMO",
            "direction": direction,
            "quality": "ALTA",
            "score": 3,
            "evidence": [
                f"Body/R {body * 100:.2f}%",
                "cierre muy cerca del extremo",
                "cuerpo dominante",
            ],
        }

    # Continuacion fuerte: la vela actual mantiene la dirección reciente
    # después de una vela previa con dirección similar.
    same_recent = (
        (d == 1 and ctx["green3"] >= 2)
        or (d == -1 and ctx["red3"] >= 2)
    )

    if (
        d != 0
        and same_recent
        and body >= STRONG_BODY
        and body >= prev_body * 0.85
        and range_ratio >= 0.90
    ):
        direction = "ALCISTA" if d == 1 else "BAJISTA"
        return {
            "state": f"CONTINUACION_{direction}_FUERTE",
            "direction": direction,
            "quality": "ALTA",
            "score": 3,
            "evidence": [
                "dirección repetida en 3 velas",
                f"Body/R {body * 100:.2f}%",
                "rango mantiene expansión",
            ],
        }

    # Recuperacion: cambio de color contra la vela anterior pero cierre
    # suficientemente dominante en la nueva dirección.
    reversal = d != 0 and pdirection != 0 and d != pdirection

    if reversal and body >= MEDIUM_BODY:
        direction = "ALCISTA" if d == 1 else "BAJISTA"

        if (
            (d == 1 and close_pos >= 0.68)
            or (d == -1 and close_pos <= 0.32)
        ):
            return {
                "state": f"RECUPERACION_{direction}",
                "direction": direction,
                "quality": "MEDIA",
                "score": 2,
                "evidence": [
                    "cambio de dirección respecto a la vela anterior",
                    f"Body/R {body * 100:.2f}%",
                    "cierre acompaña la nueva dirección",
                ],
            }

    # Respuesta fuerte: la vela actual responde a una vela anterior fuerte
    # en sentido contrario. Es diferente de una continuación.
    if reversal and prev_body >= STRONG_BODY and body >= MEDIUM_BODY:
        direction = "ALCISTA" if d == 1 else "BAJISTA"
        return {
            "state": f"RESPUESTA_{direction}_FUERTE",
            "direction": direction,
            "quality": "MEDIA",
            "score": 2,
            "evidence": [
                "vela anterior fuerte en sentido contrario",
                "respuesta de color opuesto",
                f"Body/R actual {body * 100:.2f}%",
            ],
        }

    # Desaceleracion: misma dirección pero cuerpo/rango cae claramente.
    if (
        d != 0
        and pdirection == d
        and body <= SMALL_BODY
        and prev_body >= MEDIUM_BODY
    ):
        direction = "ALCISTA" if d == 1 else "BAJISTA"
        return {
            "state": f"DESACELERACION_{direction}",
            "direction": direction,
            "quality": "BAJA",
            "score": 1,
            "evidence": [
                "misma dirección que la vela anterior",
                "cuerpo reducido",
                "pérdida de dominancia",
            ],
        }

    # Impulso normal.
    if d != 0 and body >= MEDIUM_BODY:
        direction = "ALCISTA" if d == 1 else "BAJISTA"
        return {
            "state": f"IMPULSO_{direction}",
            "direction": direction,
            "quality": "MEDIA",
            "score": 2,
            "evidence": [
                f"Body/R {body * 100:.2f}%",
                "dirección clara",
            ],
        }

    # Si el rango es pequeño y no hay estructura clara, es ruido.
    if range_ratio < 0.70 and body < MEDIUM_BODY:
        return {
            "state": "RUIDO",
            "direction": "NEUTRAL",
            "quality": "BAJA",
            "score": 0,
            "evidence": [
                "rango reducido frente al contexto",
                "cuerpo sin dominancia",
            ],
        }

    return {
        "state": "TRANSICION",
        "direction": "NEUTRAL",
        "quality": "BAJA",
        "score": 0,
        "evidence": [
            "estructura sin dominio suficiente",
            "no existe continuidad clara",
        ],
    }


def _prediction_from_state(
    state: Dict[str, Any],
    candles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Genera la predicción de la SIGUIENTE M1 usando solamente las velas
    disponibles hasta el cierre actual.

    Regla conservadora:
    - estados fuertes de continuación -> misma dirección
    - impulso extremo -> esperar; no perseguir automáticamente
    - recuperación fuerte -> esperar confirmación
    - desaceleración/pausa/transición/ruido -> NO SIGNAL
    """
    name = state["state"]
    direction = state["direction"]

    prediction = "NO SIGNAL"
    quality = state["quality"]
    reasons = list(state["evidence"])

    if name in {
        "CONTINUACION_ALCISTA_FUERTE",
        "IMPULSO_ALCISTA",
    }:
        prediction = "CALL"
    elif name in {
        "CONTINUACION_BAJISTA_FUERTE",
        "IMPULSO_BAJISTA",
    }:
        prediction = "PUT"
    elif name in {
        "RECUPERACION_ALCISTA",
        "RESPUESTA_ALCISTA_FUERTE",
    }:
        # Recuperaciones aisladas no se persiguen.
        prediction = "NO SIGNAL"
        reasons.append("recuperación requiere confirmación")
    elif name in {
        "RECUPERACION_BAJISTA",
        "RESPUESTA_BAJISTA_FUERTE",
    }:
        prediction = "NO SIGNAL"
        reasons.append("respuesta bajista requiere confirmación")
    elif "EXTREMO" in name:
        prediction = "NO SIGNAL"
        reasons.append("impulso extremo: evitar persecución inmediata")
    else:
        prediction = "NO SIGNAL"

    # La intraminuto es confirmación secundaria. Nunca crea por sí sola una
    # señal cuando la anatomía principal no la respalda.
    intrabar = _intrabar_quality(candles[-1])

    if prediction == "CALL" and intrabar["quality"] in {"BUENA", "MEDIA"}:
        if intrabar["direction"] == "BAJISTA":
            prediction = "NO SIGNAL"
            reasons.append("intraminuto contradice CALL")
    elif prediction == "PUT" and intrabar["quality"] in {"BUENA", "MEDIA"}:
        if intrabar["direction"] == "ALCISTA":
            prediction = "NO SIGNAL"
            reasons.append("intraminuto contradice PUT")

    # Calidad final.
    if prediction == "NO SIGNAL":
        final_quality = "BAJA"
    elif state["quality"] == "ALTA":
        final_quality = "ALTA"
    else:
        final_quality = "MEDIA"

    return {
        "prediction": prediction,
        "prediction_quality": final_quality,
        "prediction_direction": (
            "ALCISTA" if prediction == "CALL"
            else "BAJISTA" if prediction == "PUT"
            else "NEUTRAL"
        ),
        "prediction_reason": " | ".join(reasons),
        "intrabar": intrabar,
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    **_: Any,
) -> Dict[str, Any]:
    """
    Punto principal usado por bot.py.

    Importante: la predicción corresponde a la siguiente M1 y se calcula
    antes de recibir esa vela.
    """
    data = df if df is not None else pd.DataFrame()
    candles = last_10_closed(data)
    window = summarize_window(data)

    base: Dict[str, Any] = {
        "signal": None,
        "blocked": True,
        "score": 0,
        "reason": "historial insuficiente",
        "state": "SIN_HISTORIAL",
        "state_direction": "NEUTRAL",
        "state_quality": "BAJA",
        "evidence": [],
        "prediction": "NO SIGNAL",
        "prediction_quality": "BAJA",
        "prediction_direction": "NEUTRAL",
        "prediction_reason": "faltan velas cerradas",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "window": window,
    }

    if len(candles) < WINDOW:
        return base

    state = _classify_state(candles)
    pred = _prediction_from_state(state, candles)

    signal = pred["prediction"]
    is_signal = signal in {"CALL", "PUT"}

    result = {
        **base,
        "signal": signal if is_signal else None,
        "blocked": not is_signal,
        "score": int(state["score"]),
        "reason": pred["prediction_reason"],
        "state": state["state"],
        "state_direction": state["direction"],
        "state_quality": state["quality"],
        "evidence": state["evidence"],
        "prediction": signal,
        "prediction_quality": pred["prediction_quality"],
        "prediction_direction": pred["prediction_direction"],
        "prediction_reason": pred["prediction_reason"],
        "intrabar": pred["intrabar"],
        "window": window,
        "current_candle": candles[-1],
    }

    return result


def compare_window_to_next(
    previous_10: List[Dict[str, Any]],
    next_candle: Dict[str, Any],
) -> Dict[str, Any]:
    """Utilidad de compatibilidad para comparar prediccion y resultado."""
    if len(previous_10) != WINDOW:
        return {"ready": False}

    signal = None
    if previous_10:
        # No se recalcula con next_candle: solo sirve como comparador.
        dummy = pd.DataFrame(
            [
                {
                    "from": c.get("timestamp", i),
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                }
                for i, c in enumerate(previous_10)
            ]
        )
        signal = analyze_market(dummy).get("prediction")

    entry = float(next_candle.get("open", 0.0))
    close = float(next_candle.get("close", 0.0))

    if signal == "CALL":
        outcome = (
            "FAVORABLE" if close > entry
            else "DOJI" if close == entry
            else "CONTRARIA"
        )
    elif signal == "PUT":
        outcome = (
            "FAVORABLE" if close < entry
            else "DOJI" if close == entry
            else "CONTRARIA"
        )
    else:
        outcome = "NO_SIGNAL"

    return {
        "ready": True,
        "sequence": " ".join(
            "V" if c.get("color") == "VERDE"
            else "R" if c.get("color") == "ROJA"
            else "D"
            for c in previous_10
        ),
        "prediction": signal or "NO SIGNAL",
        "next_color": next_candle.get("color"),
        "next_open": next_candle.get("open"),
        "next_close": next_candle.get("close"),
        "next_high": next_candle.get("high"),
        "next_low": next_candle.get("low"),
        "outcome": outcome,
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
