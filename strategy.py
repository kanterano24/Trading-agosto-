from __future__ import annotations

from collections import Counter
import pandas as pd

# QUANT MODE — análisis y simulación exclusivamente.
# Esta estrategia NO emite señales ejecutables: signal siempre es None.
WINDOW = 104
BODY_RATIO_MIN = 0.0


def _prepare(data):
    if data is None or not isinstance(data, pd.DataFrame) or data.empty:
        return None

    df = data.copy()
    if "high" not in df.columns and "max" in df.columns:
        df["high"] = df["max"]
    if "low" not in df.columns and "min" in df.columns:
        df["low"] = df["min"]

    cols = ["open", "high", "low", "close"]
    if any(c not in df.columns for c in cols):
        return None
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=cols).reset_index(drop=True)
    if df.empty:
        return None
    return df.tail(WINDOW).reset_index(drop=True)


def _candle_features(df):
    out = df.copy()
    out["range"] = out["high"] - out["low"]
    safe_range = out["range"].where(out["range"] > 0)
    out["body"] = (out["close"] - out["open"]).abs()
    out["body_r"] = (out["body"] / safe_range).fillna(0.0)
    out["upper_wick"] = (out["high"] - out[["open", "close"]].max(axis=1)).clip(lower=0)
    out["lower_wick"] = (out[["open", "close"]].min(axis=1) - out["low"]).clip(lower=0)
    out["close_position"] = ((out["close"] - out["low"]) / safe_range).fillna(0.5).clip(0, 1)
    out["direction"] = out["close"].gt(out["open"]).astype(int) - out["close"].lt(out["open"]).astype(int)
    return out


def _sequence_stats(features, n):
    part = features.tail(n)
    if len(part) < n:
        return {"available": False, "bull": 0, "bear": 0, "neutral": 0,
                "net_change": 0.0, "avg_body_r": 0.0, "direction_consistency": 0.0}
    dirs = part["direction"].tolist()
    nonzero = [d for d in dirs if d != 0]
    consistency = abs(sum(nonzero)) / len(nonzero) if nonzero else 0.0
    first_open = float(part.iloc[0]["open"])
    last_close = float(part.iloc[-1]["close"])
    scale = float(part["range"].mean())
    net_change = (last_close - first_open) / scale if scale > 0 else 0.0
    return {
        "available": True,
        "bull": sum(d > 0 for d in dirs),
        "bear": sum(d < 0 for d in dirs),
        "neutral": sum(d == 0 for d in dirs),
        "net_change": net_change,
        "avg_body_r": float(part["body_r"].mean()),
        "direction_consistency": consistency,
    }


def analyze_market(data, pair=None, mode="M1_SIM"):
    """
    Analiza hasta 104 velas cerradas M1 y estima la dirección de la siguiente.
    Devuelve prediction CALL/PUT/NEUTRAL solo como clasificación simulada.
    `signal` permanece siempre en None para impedir que un bot existente
    interprete la predicción como una orden real.
    """
    df = _prepare(data)
    if df is None or len(df) < 10:
        return {
            "signal": None, "prediction": "NEUTRAL",
            "simulation_only": True, "price_action_confirmed": False,
            "reason": "Datos insuficientes: se requieren al menos 10 velas válidas",
            "analysis": {"pair": pair, "mode": mode, "candles": 0 if df is None else len(df)}
        }

    f = _candle_features(df)
    last = f.iloc[-1]
    sequences = {n: _sequence_stats(f, n) for n in (2, 3, 5, 10)}

    # Componentes descriptivos del comportamiento reciente.
    recent3 = f.tail(3)
    recent5 = f.tail(5)
    direction3 = int(recent3["direction"].sum())
    direction5 = int(recent5["direction"].sum())
    avg_body3 = float(recent3["body_r"].mean())
    avg_body5 = float(recent5["body_r"].mean())

    # Impulso: dirección consistente, cambio neto y cuerpos relativamente amplios.
    impulse_score = max(-1.0, min(1.0, direction5 / 5.0)) * avg_body5
    # Continuación: persistencia de la dirección en las últimas 2 y 3 velas.
    continuation_score = (direction3 / 3.0) * avg_body3
    # Recuperación/respuesta: última vela va contra la secuencia corta y recupera
    # parte del rango medio reciente; se registra como contexto, no como orden.
    prior_direction = int(f.iloc[-2]["direction"])
    recovery = bool(last["direction"] != 0 and prior_direction != 0
                    and last["direction"] == -prior_direction)
    # Desaceleración: caída del cuerpo medio reciente frente al bloque previo.
    prior5 = f.iloc[-10:-5]
    prior_avg_body = float(prior5["body_r"].mean()) if len(prior5) == 5 else avg_body5
    deceleration = avg_body3 < prior_avg_body * 0.70 if prior_avg_body > 0 else False

    # Ruido/transición: direcciones muy alternantes y escaso desplazamiento neto.
    last10 = f.tail(10)["direction"].tolist()
    flips = sum(1 for a, b in zip(last10, last10[1:]) if a * b < 0)
    noise = flips >= 6 and abs(sequences[10]["net_change"]) < 1.0

    # Puntaje simple, transparente y determinista basado solo en precio.
    score = 0.55 * impulse_score + 0.45 * continuation_score
    if recovery:
        score *= 0.75
    if deceleration:
        score *= 0.75
    if noise:
        score *= 0.35

    if noise or abs(score) < 0.12:
        prediction = "NEUTRAL"
        confidence = min(1.0, abs(score) / 0.12) if not noise else 0.0
    else:
        prediction = "CALL" if score > 0 else "PUT"
        confidence = min(1.0, abs(score) / 0.65)

    quality_flags = []
    if len(df) < WINDOW:
        quality_flags.append(f"ventana parcial: {len(df)}/{WINDOW} velas")
    if noise:
        quality_flags.append("ruido/transición elevado")
    if deceleration:
        quality_flags.append("desaceleración detectada")
    if recovery:
        quality_flags.append("respuesta contraria reciente")
    quality = "BAJA" if noise or len(df) < 20 else "MEDIA"
    if len(df) >= WINDOW and not noise and not deceleration:
        quality = "ALTA"

    return {
        "signal": None,  # Bloqueo explícito: no enviar órdenes reales.
        "prediction": prediction,
        "simulation_only": True,
        "price_action_confirmed": prediction != "NEUTRAL",
        "force_candle": bool(float(last["body_r"]) >= 0.60),
        "reason": (
            f"SIMULACIÓN | predicción {prediction} | score {score:.3f} | "
            f"confianza heurística {confidence:.2f} | calidad {quality}"
        ),
        "analysis": {
            "pair": pair,
            "mode": mode,
            "window_used": len(df),
            "window_target": WINDOW,
            "last_candle": {
                "body": float(last["body"]),
                "range": float(last["range"]),
                "body_r": float(last["body_r"]),
                "upper_wick": float(last["upper_wick"]),
                "lower_wick": float(last["lower_wick"]),
                "close_position": float(last["close_position"]),
                "direction": int(last["direction"]),
            },
            "sequences": sequences,
            "impulse_score": impulse_score,
            "continuation_score": continuation_score,
            "recovery_or_response": recovery,
            "deceleration": deceleration,
            "transition_or_noise": noise,
            "intraminute_data": "UNAVAILABLE_FROM_OHLC",
            "information_quality": quality,
            "quality_notes": quality_flags,
            "prediction_score": score,
            "heuristic_confidence": confidence,
            "evaluation": "Pendiente: comparar con la siguiente vela cerrada",
        },
    }


def analyze(data, pair=None, mode="M1_SIM"):
    """Alias de compatibilidad; mantiene el modo de simulación."""
    return analyze_market(data, pair=pair, mode=mode)


def evaluate_prediction(prediction, next_candle):
    """
    Evalúa una predicción ya registrada contra la vela siguiente.
    No modifica la predicción ni ejecuta operaciones.
    """
    df = _prepare(next_candle)
    if df is None or df.empty:
        return {"evaluated": False, "result": "NO_DATA"}
    candle = df.iloc[-1]
    actual = "CALL" if candle["close"] > candle["open"] else (
        "PUT" if candle["close"] < candle["open"] else "NEUTRAL"
    )
    predicted = str(prediction).upper()
    return {
        "evaluated": True,
        "prediction": predicted,
        "actual": actual,
        "correct": predicted == actual if predicted != "NEUTRAL" else None,
        "note": "Evaluación direccional de vela; no equivale a rentabilidad de una operación."
    }
