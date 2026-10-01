from __future__ import annotations
"""Estrategia M1 de SIMULACION.

Solo precio y anatomia de las ultimas 10 velas.
Sin indicadores, S/R ni rechazo.
Nunca envia orden real.
"""
from typing import Any, Dict, Optional
import pandas as pd

M1 = 60
WINDOW = 10
MIN_SCORE = 7


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
        d = d.dropna(subset=["from"]).sort_values("from")
        d = d.drop_duplicates("from")
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


def _score(candles: list[Dict[str, Any]], direction: str) -> tuple[int, list[str]]:
    last = candles[-1]
    prev = candles[-2]
    wanted = "VERDE" if direction == "CALL" else "ROJA"
    score = 0
    reasons: list[str] = []

    if last["color"] != wanted:
        return 0, ["ultima vela no coincide con la direccion"]
    score += 1
    reasons.append("ultima vela a favor")

    if last["body_ratio"] >= 0.70:
        score += 2; reasons.append(f"Body/R {last['body_ratio']*100:.1f}% >= 70%")
    elif last["body_ratio"] >= 0.55:
        score += 1; reasons.append(f"Body/R {last['body_ratio']*100:.1f}% >= 55%")

    if direction == "CALL":
        if last["close_pos"] >= 0.90:
            score += 2; reasons.append("cierre muy cerca del maximo")
        elif last["close_pos"] >= 0.75:
            score += 1; reasons.append("cierre en zona alta")
        if last["close"] > prev["close"]:
            score += 1; reasons.append("cierre mayor que la vela anterior")
    else:
        if last["close_pos"] <= 0.10:
            score += 2; reasons.append("cierre muy cerca del minimo")
        elif last["close_pos"] <= 0.25:
            score += 1; reasons.append("cierre en zona baja")
        if last["close"] < prev["close"]:
            score += 1; reasons.append("cierre menor que la vela anterior")

    recent3 = candles[-3:]
    recent5 = candles[-5:]
    n3 = sum(c["color"] == wanted for c in recent3)
    n5 = sum(c["color"] == wanted for c in recent5)
    if n3 >= 2:
        score += 1; reasons.append(f"{n3}/3 velas recientes a favor")
    if n5 >= 3:
        score += 1; reasons.append(f"{n5}/5 velas recientes a favor")

    if last["body_ratio"] > prev["body_ratio"] and last["body_ratio"] >= 0.55:
        score += 1; reasons.append("cuerpo mas dominante que la vela anterior")

    return min(score, 9), reasons


def analyze_market(df: Optional[pd.DataFrame] = None, **_: Any) -> Dict[str, Any]:
    candles = last_10_closed(df if df is not None else pd.DataFrame())
    window = summarize_window(df if df is not None else pd.DataFrame())
    base = {
        "signal": None, "score": 0, "blocked": True,
        "reason": "historial insuficiente", "window": window,
        "analysis_timeframe": "M1", "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
    }
    if len(candles) < WINDOW:
        return base

    call_score, call_reasons = _score(candles, "CALL")
    put_score, put_reasons = _score(candles, "PUT")

    if call_score >= MIN_SCORE and call_score > put_score:
        return {**base, "signal": "CALL", "score": call_score, "blocked": False,
                "reason": " | ".join(call_reasons)}
    if put_score >= MIN_SCORE and put_score > call_score:
        return {**base, "signal": "PUT", "score": put_score, "blocked": False,
                "reason": " | ".join(put_reasons)}

    return {**base, "reason": f"sin señal: CALL={call_score}/9, PUT={put_score}/9"}


# Compatibilidad con versiones anteriores.
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
