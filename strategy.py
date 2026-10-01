from __future__ import annotations

"""Estrategia CI(14) para M1 -> M1.

Reglas EXACTAS:
    CALL: el CI cruza hacia ARRIBA 61.8 y la vela que produce el cruce es ROJA.
    PUT : el CI cruza hacia ABAJO 38.2 y la vela que produce el cruce es VERDE.

La señal se confirma solamente con velas M1 CERRADAS.
El bot debe ejecutar la señal en la apertura de la siguiente vela M1.
No se utilizan EMA, RSI, MACD, ATR, tendencia, soporte/resistencia,
rupturas de precio ni alternancia obligatoria de direcciones.
"""

from typing import Any, Dict, Optional
import math
import pandas as pd

M1 = 60
MIN_BARS = 30
CI_PERIOD = 14
OVERBOUGHT = 61.8
OVERSOLD = 38.2

MODE_CONFIG = {
    "M1_M1": {
        "analysis_tf": "M1",
        "entry_tf": "M1",
        "expiration": 1,
    }
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
        d = d.dropna(subset=["from"])
        d = d.sort_values("from").drop_duplicates("from")

    d = d.dropna(subset=required).reset_index(drop=True)
    return d


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "reason": reason,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "expiration": 1,
        "analysis": {},
    }


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    """Calcula Choppiness Index estándar."""
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close = data["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    tr_sum = tr.rolling(period, min_periods=period).sum()
    highest = high.rolling(period, min_periods=period).max()
    lowest = low.rolling(period, min_periods=period).min()
    price_range = highest - lowest

    with pd.option_context("mode.use_inf_as_na", True):
        ratio = tr_sum / price_range
        ci = 100.0 * ratio.apply(lambda x: math.log10(x) if pd.notna(x) and x > 0 else float("nan")) / math.log10(period)

    return ci.replace([float("inf"), float("-inf")], pd.NA).astype("float64")


def analyze_market(df=None, pair=None, mode="M1_M1", **kwargs):
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}")

    # IMPORTANTE: data debe contener solamente velas CERRADAS.
    ci = _choppiness_index(data, CI_PERIOD)
    data = data.copy()
    data["ci"] = ci

    prev = data.iloc[-2]
    cur = data.iloc[-1]

    prev_ci = float(prev["ci"]) if pd.notna(prev["ci"]) else float("nan")
    cur_ci = float(cur["ci"]) if pd.notna(cur["ci"]) else float("nan")

    if not math.isfinite(prev_ci) or not math.isfinite(cur_ci):
        return _empty("CI insuficiente para confirmar el cruce")

    open_price = float(cur["open"])
    close_price = float(cur["close"])
    if close_price > open_price:
        candle_color = "VERDE"
    elif close_price < open_price:
        candle_color = "ROJA"
    else:
        candle_color = "DOJI"

    cross_up = prev_ci <= OVERBOUGHT and cur_ci > OVERBOUGHT
    cross_down = prev_ci >= OVERSOLD and cur_ci < OVERSOLD

    analysis = {
        "ci_period": CI_PERIOD,
        "previous_ci": prev_ci,
        "current_ci": cur_ci,
        "overbought": OVERBOUGHT,
        "oversold": OVERSOLD,
        "cross_up_61_8": bool(cross_up),
        "cross_down_38_2": bool(cross_down),
        "candle_color": candle_color,
        "candle_open": open_price,
        "candle_close": close_price,
        "signal_candle_from": int(cur["from"]) if "from" in cur.index and pd.notna(cur["from"]) else None,
    }

    # CALL: CI cruza arriba de 61.8 + vela ROJA.
    if cross_up and candle_color == "ROJA":
        return {
            "signal": "call",
            "reason": (
                f"CALL | CI cruza ARRIBA 61.8 "
                f"({prev_ci:.2f}->{cur_ci:.2f}) | vela ROJA | siguiente M1"
            ),
            "mode": "M1_M1",
            "analysis_timeframe": "M1",
            "entry_timeframe": "M1",
            "expiration": 1,
            "analysis": analysis,
        }

    # PUT: CI cruza abajo de 38.2 + vela VERDE.
    if cross_down and candle_color == "VERDE":
        return {
            "signal": "put",
            "reason": (
                f"PUT | CI cruza ABAJO 38.2 "
                f"({prev_ci:.2f}->{cur_ci:.2f}) | vela VERDE | siguiente M1"
            ),
            "mode": "M1_M1",
            "analysis_timeframe": "M1",
            "entry_timeframe": "M1",
            "expiration": 1,
            "analysis": analysis,
        }

    if cross_up:
        return {**_empty(f"cruce CI ARRIBA 61.8 pero vela {candle_color}, no CALL"), "analysis": analysis}

    if cross_down:
        return {**_empty(f"cruce CI ABAJO 38.2 pero vela {candle_color}, no PUT"), "analysis": analysis}

    return {**_empty("sin cruce válido CI 61.8/38.2"), "analysis": analysis}


def get_signal(df=None, **kwargs):
    return analyze_market(df=df, **kwargs).get("signal")


def signal(df=None, **kwargs):
    return get_signal(df, **kwargs)
