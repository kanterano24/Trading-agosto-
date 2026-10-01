from __future__ import annotations

"""Estrategia de simulacion M1 basada solo en precio.

No usa indicadores, S/R ni rechazo.

Flujo:
1) Se toman las 10 velas M1 cerradas mas recientes.
2) Se calcula anatomia de cada vela.
3) Se busca una vela de impulso en la ultima posicion.
4) Se genera una senal HIPOTETICA para la siguiente M1.
5) El bot espera a que cierre la siguiente M1 y registra el resultado real.

Las entradas reales estan fuera de este modulo y permanecen desactivadas.
"""

from typing import Any, Dict, List, Optional
import pandas as pd

M1 = 60
WINDOW = 10
MIN_BODY_RATIO = 0.70
CLOSE_EDGE_RATIO = 0.25


def normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    d = df.copy().rename(columns={"max": "high", "min": "low"})
    required = ["from", "open", "high", "low", "close"]
    if any(c not in d.columns for c in required):
        return pd.DataFrame()

    for c in required:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    return (
        d.dropna(subset=required)
        .drop_duplicates("from")
        .sort_values("from")
        .reset_index(drop=True)
    )


def candle_data(row: pd.Series) -> Dict[str, Any]:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    body = abs(c - o)
    rng = max(h - l, 0.0)
    lower = max(0.0, min(o, c) - l)
    upper = max(0.0, h - max(o, c))
    body_ratio = (body / rng * 100.0) if rng > 0 else 0.0

    if c > o:
        color = "VERDE"
    elif c < o:
        color = "ROJA"
    else:
        color = "DOJI"

    close_from_high = ((h - c) / rng * 100.0) if rng > 0 else 0.0
    close_from_low = ((c - l) / rng * 100.0) if rng > 0 else 0.0

    return {
        "timestamp": int(row["from"]),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "lower_wick": lower,
        "upper_wick": upper,
        "body": body,
        "range": rng,
        "body_ratio": body_ratio,
        "close_from_high_pct": close_from_high,
        "close_from_low_pct": close_from_low,
        "color": color,
    }


def last_10_closed(df: pd.DataFrame) -> List[Dict[str, Any]]:
    d = normalize(df)
    if len(d) < WINDOW:
        return []
    return [candle_data(row) for _, row in d.iloc[-WINDOW:].iterrows()]


def sequence(candles: List[Dict[str, Any]]) -> str:
    return " ".join(
        "V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D"
        for c in candles
    )


def _last_context(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    colors = [c["color"] for c in candles]
    last3 = candles[-3:]
    last5 = candles[-5:]

    green = sum(c["color"] == "VERDE" for c in candles)
    red = sum(c["color"] == "ROJA" for c in candles)
    green3 = sum(c["color"] == "VERDE" for c in last3)
    red3 = sum(c["color"] == "ROJA" for c in last3)
    green5 = sum(c["color"] == "VERDE" for c in last5)
    red5 = sum(c["color"] == "ROJA" for c in last5)

    return {
        "green_10": green,
        "red_10": red,
        "green_3": green3,
        "red_3": red3,
        "green_5": green5,
        "red_5": red5,
        "last3": " ".join("V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in last3),
        "last5": " ".join("V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in last5),
    }


def analyze_market(df: Optional[pd.DataFrame] = None, **_: Any) -> Dict[str, Any]:
    candles = last_10_closed(df if df is not None else pd.DataFrame())
    base = {
        "signal": None,
        "blocked": True,
        "reason": "menos de 10 velas cerradas",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "window": {
            "ready": len(candles) == WINDOW,
            "count": len(candles),
            "candles": candles,
            "sequence": sequence(candles),
        },
    }

    if len(candles) != WINDOW:
        return base

    context = _last_context(candles)
    last = candles[-1]
    previous = candles[-2]

    direction = None
    reasons: List[str] = []
    score = 0

    if last["color"] == "VERDE":
        direction = "CALL"
        reasons.append("ultima vela verde")
        score += 1
    elif last["color"] == "ROJA":
        direction = "PUT"
        reasons.append("ultima vela roja")
        score += 1
    else:
        reasons.append("ultima vela doji")

    if direction is None:
        base.update({"reason": "; ".join(reasons), "context": context, "score": 0})
        return base

    if last["body_ratio"] >= MIN_BODY_RATIO * 100:
        score += 2
        reasons.append(f"Body/R {last['body_ratio']:.2f}% >= 70%")
    else:
        reasons.append(f"Body/R {last['body_ratio']:.2f}% < 70%")

    if direction == "CALL":
        close_near_edge = last["close_from_high_pct"] <= CLOSE_EDGE_RATIO * 100
        if close_near_edge:
            score += 2
            reasons.append("cierre cerca del maximo")
        if last["close"] > previous["close"]:
            score += 1
            reasons.append("cierre > cierre anterior")
        else:
            reasons.append("cierre <= cierre anterior")
        if context["green_3"] >= 2:
            score += 1
            reasons.append("balance alcista en ultimas 3")
        if context["green_5"] >= 3:
            score += 1
            reasons.append("balance alcista en ultimas 5")
        if last["body_ratio"] > previous["body_ratio"]:
            score += 1
            reasons.append("ultima vela mas dominante que la anterior")
    else:
        close_near_edge = last["close_from_low_pct"] <= CLOSE_EDGE_RATIO * 100
        if close_near_edge:
            score += 2
            reasons.append("cierre cerca del minimo")
        if last["close"] < previous["close"]:
            score += 1
            reasons.append("cierre < cierre anterior")
        else:
            reasons.append("cierre >= cierre anterior")
        if context["red_3"] >= 2:
            score += 1
            reasons.append("balance bajista en ultimas 3")
        if context["red_5"] >= 3:
            score += 1
            reasons.append("balance bajista en ultimas 5")
        if last["body_ratio"] > previous["body_ratio"]:
            score += 1
            reasons.append("ultima vela mas dominante que la anterior")

    # Umbral conservador para simulacion. No ejecuta dinero real.
    if score >= 7:
        signal = direction
        reason = " | ".join(reasons)
        blocked = False
    else:
        signal = None
        blocked = True
        reason = f"sin señal: score {score}/9 < 7 | " + " | ".join(reasons)

    base.update(
        {
            "signal": signal,
            "blocked": blocked,
            "reason": reason,
            "score": score,
            "max_score": 9,
            "context": context,
            "last_candle": last,
            "previous_candle": previous,
        }
    )
    return base


def compare_window_to_next(previous_10: List[Dict[str, Any]], next_candle: Dict[str, Any]) -> Dict[str, Any]:
    if len(previous_10) != WINDOW:
        return {"ready": False}

    return {
        "ready": True,
        "sequence": sequence(previous_10),
        "next_color": next_candle.get("color"),
        "next_open": next_candle.get("open"),
        "next_close": next_candle.get("close"),
        "next_body": next_candle.get("body"),
        "next_body_ratio": next_candle.get("body_ratio"),
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    result = analyze_market(df)
    return result.get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
