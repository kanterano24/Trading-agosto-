from __future__ import annotations

"""Estrategia M1 de vela de fuerza + ruptura de precio anterior.

No utiliza indicadores. Toda la señal se obtiene exclusivamente de la acción
 del precio de la vela M1 actual y de las velas M1 anteriores.
"""
from typing import Any, Dict, Optional
import pandas as pd

M1 = 60
MIN_BARS = 8
BODY_RATIO_MIN = 0.60
BODY_EXPANSION_MIN = 1.20
LOOKBACK_BODIES = 5
MODE_CONFIG = {"M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1}}


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
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
    return d.dropna(subset=required).reset_index(drop=True)


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "reason": reason,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "expiration": 1,
        "force_candle": False,
        "price_action_confirmed": False,
        "analysis": {},
    }


def _metrics(row: pd.Series) -> Dict[str, float]:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    rng = max(h - l, 0.0)
    body = abs(c - o)
    upper_wick = max(h - max(o, c), 0.0)
    lower_wick = max(min(o, c) - l, 0.0)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "body_ratio": body / rng if rng else 0.0,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
    }


def analyze_market(df=None, pair=None, mode="M1_M1", **kwargs):
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}")

    # La última fila debe ser la VELA M1 ACTUAL, todavía abierta.
    cur = _metrics(data.iloc[-1])
    prev = _metrics(data.iloc[-2])

    if cur["close"] > cur["open"]:
        signal = "call"
        direction = "alcista"
    elif cur["close"] < cur["open"]:
        signal = "put"
        direction = "bajista"
    else:
        return _empty("vela actual sin dirección")

    # Tamaño de referencia: mediana del cuerpo de las 5 velas anteriores.
    previous = data.iloc[-(LOOKBACK_BODIES + 1):-1]
    previous_bodies = [_metrics(row)["body"] for _, row in previous.iterrows()]
    baseline = float(pd.Series(previous_bodies).median()) if previous_bodies else 0.0

    body_ratio_ok = cur["body_ratio"] >= BODY_RATIO_MIN
    expansion_ok = baseline > 0 and cur["body"] >= baseline * BODY_EXPANSION_MIN
    force_candle = body_ratio_ok and expansion_ok

    # Acción del precio: la vela de fuerza debe romper el extremo de la vela anterior
    # y mantener el precio del lado de la ruptura. No se usa ningún indicador.
    if signal == "call":
        broke_previous = cur["high"] > prev["high"]
        confirmed_break = cur["close"] > prev["high"]
    else:
        broke_previous = cur["low"] < prev["low"]
        confirmed_break = cur["close"] < prev["low"]

    # Evita llamar "fuerza" a una vela que tiene cuerpo pequeño respecto a sus mechas.
    wick_ok = (
        cur["upper_wick"] <= cur["body"] * 0.75
        if signal == "call"
        else cur["lower_wick"] <= cur["body"] * 0.75
    )

    analysis = {
        "direction": direction,
        "body": cur["body"],
        "range": cur["range"],
        "body_ratio": cur["body_ratio"],
        "baseline_body": baseline,
        "body_ratio_ok": body_ratio_ok,
        "expansion_ok": expansion_ok,
        "force_candle": force_candle,
        "previous_high": prev["high"],
        "previous_low": prev["low"],
        "broke_previous": broke_previous,
        "confirmed_break": confirmed_break,
        "wick_ok": wick_ok,
    }

    if not force_candle:
        return {**_empty("vela M1 sin fuerza suficiente"), "analysis": analysis}

    if not confirmed_break:
        if broke_previous:
            reason = "vela de fuerza M1 rompió el extremo, pero aún no confirmó el cierre del precio sobre la ruptura"
        else:
            reason = "vela de fuerza M1 sin ruptura confirmada del extremo anterior"
        return {**_empty(reason), "force_candle": True, "analysis": analysis}

    if not wick_ok:
        return {**_empty("vela de fuerza con mecha contraria demasiado grande"), "force_candle": True, "analysis": analysis}

    color = "VERDE" if signal == "call" else "ROJA"
    level = prev["high"] if signal == "call" else prev["low"]
    reason = (
        f"VELA DE FUERZA M1 | {color} → {signal.upper()} | "
        f"ruptura confirmada del {'máximo' if signal == 'call' else 'mínimo'} anterior "
        f"({level:.8f}) | cuerpo/rango={cur['body_ratio']:.2f}"
    )

    return {
        "signal": signal,
        "reason": reason,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "expiration": 1,
        "force_candle": True,
        "price_action_confirmed": True,
        "analysis": analysis,
    }


def get_signal(df=None, **kwargs):
    return analyze_market(df=df, **kwargs).get("signal")


def signal(df=None, **kwargs):
    return get_signal(df, **kwargs)
