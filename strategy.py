from __future__ import annotations

"""Estrategia M1 basada únicamente en el patrón visual definido por el usuario.

PUT:
1) último máximo/resistencia;
2) precio se aleja del nivel;
3) regresa al mismo nivel y lo toca/rechaza;
4) la vela de rechazo cierra debajo de la resistencia;
5) la siguiente vela cierra roja;
6) entrada PUT, expiración 1 minuto.

CALL: exactamente al contrario.
"""

from typing import Any, Dict, Optional
import pandas as pd

MIN_BARS = 25
LOOKBACK = 24
TOUCH_TOLERANCE = 0.0010
MIN_AWAY_TOLERANCE = 0.0015
WICK_RATIO = 0.50

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
}


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "analysis": {},
    }


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
        d = d.sort_values("from")
    return d.dropna(subset=required).reset_index(drop=True)


def _candle(row: pd.Series) -> Dict[str, float]:
    o = float(row["open"]); h = float(row["high"])
    l = float(row["low"]); c = float(row["close"])
    rng = max(h - l, 1e-12)
    return {
        "open": o, "high": h, "low": l, "close": c,
        "range": rng,
        "body": abs(c - o),
        "upper_wick": h - max(o, c),
        "lower_wick": min(o, c) - l,
    }


def _near(price: float, level: float) -> bool:
    return abs(price - level) <= max(abs(level) * TOUCH_TOLERANCE, 1e-12)


def _last_swing_high(data: pd.DataFrame, end: int) -> Optional[tuple[int, float]]:
    # Solo utiliza velas ya cerradas dentro del historial disponible.
    start = max(1, end - LOOKBACK)
    for i in range(end - 1, start - 1, -1):
        h = float(data.iloc[i]["high"])
        if h >= float(data.iloc[i - 1]["high"]) and h >= float(data.iloc[i + 1]["high"]):
            return i, h
    return None


def _last_swing_low(data: pd.DataFrame, end: int) -> Optional[tuple[int, float]]:
    start = max(1, end - LOOKBACK)
    for i in range(end - 1, start - 1, -1):
        l = float(data.iloc[i]["low"])
        if l <= float(data.iloc[i - 1]["low"]) and l <= float(data.iloc[i + 1]["low"]):
            return i, l
    return None


def _put_pattern(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    # data[-1] = vela de confirmación. data[-2] = vela de rechazo.
    confirm_idx = len(data) - 1
    rejection_idx = confirm_idx - 1
    if rejection_idx < 2:
        return None

    swing = _last_swing_high(data, rejection_idx)
    if swing is None:
        return None
    swing_idx, resistance = swing
    if swing_idx >= rejection_idx:
        return None

    rejection = _candle(data.iloc[rejection_idx])
    confirmation = _candle(data.iloc[confirm_idx])

    # Debe existir alejamiento real entre el primer toque y el regreso.
    between = data.iloc[swing_idx + 1:rejection_idx]
    if between.empty:
        return None
    away_low = float(between["low"].min())
    min_away = max(abs(resistance) * MIN_AWAY_TOLERANCE, 1e-12)
    if resistance - away_low < min_away:
        return None

    # Segundo toque/rechazo: la mecha llega al nivel y el cierre queda debajo.
    touched = rejection["high"] >= resistance * (1 - TOUCH_TOLERANCE)
    closed_below = rejection["close"] < resistance
    wick_reject = rejection["upper_wick"] >= max(rejection["body"] * WICK_RATIO, 1e-12)
    if not (touched and closed_below and wick_reject):
        return None

    # La siguiente vela debe ser roja.
    confirmation_red = confirmation["close"] < confirmation["open"]
    if not confirmation_red:
        return None

    return {
        "signal": "put",
        "level": resistance,
        "level_type": "resistance",
        "first_touch_index": swing_idx,
        "rejection_index": rejection_idx,
        "confirmation_index": confirm_idx,
        "reason": "PUT | último máximo/resistencia | recorrido | segundo toque y rechazo | cierre debajo | siguiente vela roja",
    }


def _call_pattern(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    confirm_idx = len(data) - 1
    rejection_idx = confirm_idx - 1
    if rejection_idx < 2:
        return None

    swing = _last_swing_low(data, rejection_idx)
    if swing is None:
        return None
    swing_idx, support = swing
    if swing_idx >= rejection_idx:
        return None

    rejection = _candle(data.iloc[rejection_idx])
    confirmation = _candle(data.iloc[confirm_idx])

    between = data.iloc[swing_idx + 1:rejection_idx]
    if between.empty:
        return None
    away_high = float(between["high"].max())
    min_away = max(abs(support) * MIN_AWAY_TOLERANCE, 1e-12)
    if away_high - support < min_away:
        return None

    touched = rejection["low"] <= support * (1 + TOUCH_TOLERANCE)
    closed_above = rejection["close"] > support
    wick_reject = rejection["lower_wick"] >= max(rejection["body"] * WICK_RATIO, 1e-12)
    if not (touched and closed_above and wick_reject):
        return None

    confirmation_green = confirmation["close"] > confirmation["open"]
    if not confirmation_green:
        return None

    return {
        "signal": "call",
        "level": support,
        "level_type": "support",
        "first_touch_index": swing_idx,
        "rejection_index": rejection_idx,
        "confirmation_index": confirm_idx,
        "reason": "CALL | último mínimo/soporte | recorrido | segundo toque y rechazo | cierre encima | siguiente vela verde",
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}")

    # La función recibe únicamente velas cerradas desde bot.py.
    put = _put_pattern(data)
    call = _call_pattern(data)

    # Nunca se inventa una preferencia si ambos aparecieran simultáneamente.
    if put and call:
        return _empty("dos patrones simultáneos; entrada descartada")

    pattern = put or call
    if not pattern:
        return _empty("sin patrón confirmado")

    ts = int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None
    return {
        "signal": pattern["signal"],
        "direction": "bullish" if pattern["signal"] == "call" else "bearish",
        "trend": "bullish" if pattern["signal"] == "call" else "bearish",
        "higher_trend": "range",
        "reason": pattern["reason"],
        "score": 0,
        "blocked": False,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "candle_timestamp": ts,
        "analysis": pattern,
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
