from __future__ import annotations
"""Motor de reconocimiento M1 por precio puro.

- Solo velas M1 cerradas.
- Sin indicadores, S/R ni rechazo.
- No usa informacion futura.
- Reconoce anatomia, contexto, impulsos, respuestas y estados.
- Devuelve CALL/PUT solo cuando existe confluencia suficiente; de lo
  contrario devuelve None y conserva el estado reconocido.
"""
from typing import Any, Dict, Optional
import pandas as pd

M1 = 60
WINDOW = 10
MIN_CONFLUENCE = 7

STATE_NAMES = {
    "IMPULSO_ALCISTA": "IMPULSO_ALCISTA",
    "IMPULSO_BAJISTA": "IMPULSO_BAJISTA",
    "IMPULSO_ALCISTA_EXTREMO": "IMPULSO_ALCISTA_EXTREMO",
    "IMPULSO_BAJISTA_EXTREMO": "IMPULSO_BAJISTA_EXTREMO",
    "CONTINUACION_ALCISTA": "CONTINUACION_ALCISTA",
    "CONTINUACION_BAJISTA": "CONTINUACION_BAJISTA",
    "RECUPERACION_ALCISTA": "RECUPERACION_ALCISTA",
    "RECUPERACION_BAJISTA": "RECUPERACION_BAJISTA",
    "DESACELERACION_ALCISTA": "DESACELERACION_ALCISTA",
    "DESACELERACION_BAJISTA": "DESACELERACION_BAJISTA",
    "PAUSA": "PAUSA",
    "TRANSICION": "TRANSICION",
    "RUIDO": "RUIDO",
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
        d = d.dropna(subset=["from"]).sort_values("from").drop_duplicates("from")
    else:
        d = d.drop_duplicates()
    return d.dropna(subset=required).reset_index(drop=True)


def candle_metrics(row: pd.Series) -> Dict[str, Any]:
    o = float(row["open"]); h = float(row["high"])
    l = float(row["low"]); c = float(row["close"])
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    color = "VERDE" if c > o else "ROJA" if c < o else "DOJI"
    return {
        "timestamp": int(row["from"]) if "from" in row and pd.notna(row["from"]) else None,
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "range": rng,
        "upper_wick": upper, "lower_wick": lower,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        "close_pos": (c - l) / rng,
        "color": color,
        "up_move": float(row.get("up_move", 0.0) or 0.0),
        "down_move": float(row.get("down_move", 0.0) or 0.0),
        "traveled": float(row.get("traveled", 0.0) or 0.0),
        "sample_count": int(row.get("sample_count", 0) or 0),
        "up_steps": int(row.get("up_steps", 0) or 0),
        "down_steps": int(row.get("down_steps", 0) or 0),
    }


def last_10_closed(df: pd.DataFrame) -> list[Dict[str, Any]]:
    d = normalize(df)
    if len(d) < WINDOW:
        return []
    return [candle_metrics(r) for _, r in d.iloc[-WINDOW:].iterrows()]


def summarize_window(df: pd.DataFrame) -> Dict[str, Any]:
    candles = last_10_closed(df)
    seq = " ".join("V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in candles)
    return {"ready": len(candles) == WINDOW, "count": len(candles), "candles": candles, "sequence": seq}


def _direction(c: Dict[str, Any]) -> int:
    if c["color"] == "VERDE": return 1
    if c["color"] == "ROJA": return -1
    return 0


def _strength(c: Dict[str, Any]) -> float:
    """Fuerza de una vela usando cuerpo + cierre + mechas, sin indicadores."""
    br = c["body_ratio"]
    close_edge = max(c["close_pos"], 1.0 - c["close_pos"])
    wick_penalty = min(c["upper_ratio"] + c["lower_ratio"], 1.0)
    return max(0.0, min(1.0, 0.55 * br + 0.30 * close_edge + 0.15 * (1.0 - wick_penalty)))


def _dominant_direction(candles: list[Dict[str, Any]], n: int = 5) -> int:
    recent = candles[-n:]
    weighted = 0.0
    total = 0.0
    for i, c in enumerate(recent, 1):
        w = float(i) * (0.5 + c["body_ratio"])
        weighted += _direction(c) * w
        total += w
    if not total or abs(weighted) < total * 0.12:
        return 0
    return 1 if weighted > 0 else -1


def _intrabar_ok(c: Dict[str, Any], direction: int) -> bool:
    # El stream intraminuto es confirmacion secundaria. Con pocas muestras no pesa.
    if c.get("sample_count", 0) < 120:
        return True
    up = c.get("up_steps", 0); down = c.get("down_steps", 0)
    if up + down < 10:
        return True
    return up >= down if direction > 0 else down >= up


def _recognize_state(candles: list[Dict[str, Any]]) -> tuple[str, int, list[str]]:
    last = candles[-1]
    prev = candles[-2]
    prev2 = candles[-3]
    d = _direction(last)
    pd = _direction(prev)
    p2d = _direction(prev2)
    br = last["body_ratio"]
    strength = _strength(last)
    evidence: list[str] = []

    # Extremos: no son entradas por si mismos. Son eventos que exigen observar respuesta.
    if d != 0 and br >= 0.85 and last["close_pos"] >= 0.88 and d == 1:
        return "IMPULSO_ALCISTA_EXTREMO", d, ["Body/R extremo", "cierre en maximos"]
    if d != 0 and br >= 0.85 and last["close_pos"] <= 0.12 and d == -1:
        return "IMPULSO_BAJISTA_EXTREMO", d, ["Body/R extremo", "cierre en minimos"]

    # Impulso normal.
    if d == 1 and br >= 0.70 and last["close_pos"] >= 0.75:
        if pd == 1 and prev["body_ratio"] >= 0.55:
            return "CONTINUACION_ALCISTA", 1, ["dos cierres alcistas", "cuerpo dominante", "cierre alto"]
        return "IMPULSO_ALCISTA", 1, ["cuerpo dominante", "cierre alto"]
    if d == -1 and br >= 0.70 and last["close_pos"] <= 0.25:
        if pd == -1 and prev["body_ratio"] >= 0.55:
            return "CONTINUACION_BAJISTA", -1, ["dos cierres bajistas", "cuerpo dominante", "cierre bajo"]
        return "IMPULSO_BAJISTA", -1, ["cuerpo dominante", "cierre bajo"]

    # Recuperacion: la vela actual retoma direccion despues de una vela contraria/pausa.
    if d == 1 and pd <= 0 and br >= 0.45 and last["close"] > prev["close"]:
        return "RECUPERACION_ALCISTA", 1, ["retoma alcista", "cierre supera vela previa"]
    if d == -1 and pd >= 0 and br >= 0.45 and last["close"] < prev["close"]:
        return "RECUPERACION_BAJISTA", -1, ["retoma bajista", "cierre pierde vela previa"]

    # Desaceleracion: misma direccion, pero perdida clara de eficiencia.
    if d == pd and d != 0 and br < prev["body_ratio"] * 0.60 and br < 0.45:
        return ("DESACELERACION_ALCISTA" if d == 1 else "DESACELERACION_BAJISTA"), d, ["misma direccion", "caida fuerte de Body/R"]

    # Pausa: cuerpo pequeno o indecision despues de movimiento.
    if br < 0.25 or strength < 0.42:
        return "PAUSA", d, ["cuerpo reducido", "movimiento poco eficiente"]

    # Transicion: direcciones recientes enfrentadas con energia suficiente.
    if d != pd and d != 0 and pd != 0 and br >= 0.30:
        if p2d == pd:
            return "TRANSICION", d, ["cambio de direccion", "estructura reciente enfrentada"]

    if _dominant_direction(candles, 5) == d and d != 0:
        return ("CONTINUACION_ALCISTA" if d == 1 else "CONTINUACION_BAJISTA"), d, ["direccion dominante", "precio mantiene sentido"]

    return "RUIDO", 0, ["sin estructura dominante suficiente"]


def _confluence(candles: list[Dict[str, Any]], state: str, direction: int) -> tuple[int, list[str]]:
    last, prev = candles[-1], candles[-2]
    score = 0
    evidence: list[str] = []
    if direction == 0:
        return 0, ["sin direccion"]

    wanted = "VERDE" if direction > 0 else "ROJA"
    if last["color"] == wanted:
        score += 2; evidence.append("vela a favor")
    if last["body_ratio"] >= 0.70:
        score += 2; evidence.append("Body/R >= 70%")
    elif last["body_ratio"] >= 0.50:
        score += 1; evidence.append("Body/R >= 50%")

    close_good = last["close_pos"] >= 0.80 if direction > 0 else last["close_pos"] <= 0.20
    if close_good:
        score += 2; evidence.append("cierre eficiente")

    if last["close"] > prev["close"] if direction > 0 else last["close"] < prev["close"]:
        score += 1; evidence.append("cierre supera direccion previa")

    recent3 = sum(_direction(c) == direction for c in candles[-3:])
    recent5 = sum(_direction(c) == direction for c in candles[-5:])
    if recent3 >= 2:
        score += 1; evidence.append(f"{recent3}/3 a favor")
    if recent5 >= 3:
        score += 1; evidence.append(f"{recent5}/5 a favor")

    if _intrabar_ok(last, direction):
        score += 1; evidence.append("intraminuto compatible/neutral")

    # Impulso extremo y doble continuacion: reconocer, pero no perseguir.
    if state in {"IMPULSO_ALCISTA_EXTREMO", "IMPULSO_BAJISTA_EXTREMO"}:
        score = min(score, MIN_CONFLUENCE - 1)
        evidence.append("bloqueo por impulso extremo: esperar respuesta")
    elif state in {"CONTINUACION_ALCISTA", "CONTINUACION_BAJISTA"} and prev["body_ratio"] >= 0.70:
        score = min(score, MIN_CONFLUENCE - 1)
        evidence.append("continuacion tras expansion: evitar persecucion")

    return min(score, 10), evidence


def analyze_market(df: Optional[pd.DataFrame] = None, **_: Any) -> Dict[str, Any]:
    d = normalize(df if df is not None else pd.DataFrame())
    candles = last_10_closed(d)
    base: Dict[str, Any] = {
        "signal": None,
        "score": 0,
        "blocked": True,
        "decision": "NO SIGNAL",
        "state": "RUIDO",
        "direction": None,
        "quality": "LOW",
        "reason": "historial insuficiente",
        "evidence": [],
        "window": summarize_window(d),
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
    }
    if len(candles) < WINDOW:
        return base

    state, direction, state_evidence = _recognize_state(candles)
    score, evidence = _confluence(candles, state, direction)
    all_evidence = state_evidence + evidence

    quality = "HIGH" if score >= 8 else "MEDIUM" if score >= 6 else "LOW"
    signal: Optional[str] = None

    # Regla conservadora: estados extremos no entran. Las continuaciones tras
    # expansion inmediata tampoco. Se exige confluencia en estados confirmados.
    eligible = {
        "CONTINUACION_ALCISTA": 1,
        "CONTINUACION_BAJISTA": -1,
        "RECUPERACION_ALCISTA": 1,
        "RECUPERACION_BAJISTA": -1,
    }
    if state in eligible and eligible[state] == direction and score >= MIN_CONFLUENCE:
        signal = "CALL" if direction > 0 else "PUT"

    if signal:
        reason = " | ".join(all_evidence)
        decision = "SIGNAL"
    else:
        reason = " | ".join(all_evidence) if all_evidence else "sin confluencia suficiente"
        decision = "NO SIGNAL"

    return {
        **base,
        "signal": signal,
        "score": score,
        "blocked": signal is None,
        "decision": decision,
        "state": state,
        "direction": "ALCISTA" if direction > 0 else "BAJISTA" if direction < 0 else None,
        "quality": quality,
        "reason": reason,
        "evidence": all_evidence,
        "window": {**summarize_window(d), "candles": candles},
        "call_score": score if direction > 0 else 0,
        "put_score": score if direction < 0 else 0,
    }


def compare_window_to_next(previous_10: list[dict[str, Any]], next_candle: dict[str, Any]) -> Dict[str, Any]:
    if len(previous_10) != WINDOW:
        return {"ready": False}
    return {
        "ready": True,
        "sequence": " ".join("V" if c.get("color") == "VERDE" else "R" if c.get("color") == "ROJA" else "D" for c in previous_10),
        "next_color": next_candle.get("color"),
        "next_open": next_candle.get("open"),
        "next_close": next_candle.get("close"),
        "next_high": next_candle.get("high"),
        "next_low": next_candle.get("low"),
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
