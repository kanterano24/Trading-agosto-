from __future__ import annotations

"""SNIPER OTC - estrategia M1 por tendencia + cruce de Choppiness.

REGLA EXACTA DE ENTRADA
-----------------------
1) Todo el analisis se hace con velas M1 CERRADAS.
2) Tendencia alcista M1 + Choppiness 14 cruza HACIA ARRIBA 61.8 -> CALL.
3) Tendencia bajista M1 + Choppiness 14 cruza HACIA ABAJO 38.2 -> PUT.
4) Si la estructura M1 es rango, no se opera.
5) El cruce debe ser real; micro cruces pueden filtrarse con los parametros
   MIN_CI_CROSS_DELTA y MIN_CI_PENETRATION.
6) No se usa M5, EMA, RSI, MACD, Bollinger, ATR, volumen ni
   soporte/resistencia como filtro de entrada.
"""

import math
import os
from typing import Any, Dict, Optional

import pandas as pd

MIN_BARS = 35
CI_PERIOD = 14
OVERBOUGHT = 61.8
OVERSOLD = 38.2

# Evita tratar movimientos minimos del CI como cruces operables.
MIN_CI_CROSS_DELTA = float(os.getenv("MIN_CI_CROSS_DELTA", "0.75"))
MIN_CI_PENETRATION = float(os.getenv("MIN_CI_PENETRATION", "0.35"))

# Estructura M1. Se calcula sobre las velas anteriores a la vela que cruza.
STRUCTURE_BARS = int(os.getenv("STRUCTURE_BARS", "20"))
SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "2"))

MODE_CONFIG = {
    "M1_M1": {
        "analysis_tf": "M1",
        "entry_tf": "M1",
        "expiration": 1,
    }
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
        d = d.sort_values("from").drop_duplicates("from")

    return d.dropna(subset=required).reset_index(drop=True)


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close = data["close"].astype(float)
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    tr_sum = true_range.rolling(period, min_periods=period).sum()
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    price_range = hh - ll

    ci = pd.Series(float("nan"), index=data.index, dtype=float)
    valid = tr_sum.notna() & price_range.notna() & (price_range > 0) & (tr_sum > 0)
    ci.loc[valid] = (
        100.0
        * (tr_sum.loc[valid] / price_range.loc[valid]).map(math.log10)
        / math.log10(period)
    )
    return ci


def _direction(row: pd.Series) -> str:
    op = float(row["open"])
    cl = float(row["close"])
    if cl > op:
        return "bullish"
    if cl < op:
        return "bearish"
    return "neutral"


def _pivot_points(data: pd.DataFrame, lookback: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []

    if len(data) < 2 * lookback + 1:
        return highs, lows

    for i in range(lookback, len(data) - lookback):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        left_h = data.iloc[i - lookback:i]["high"].astype(float)
        right_h = data.iloc[i + 1:i + lookback + 1]["high"].astype(float)
        left_l = data.iloc[i - lookback:i]["low"].astype(float)
        right_l = data.iloc[i + 1:i + lookback + 1]["low"].astype(float)

        if h >= float(left_h.max()) and h >= float(right_h.max()):
            highs.append((i, h))
        if l <= float(left_l.min()) and l <= float(right_l.min()):
            lows.append((i, l))

    return highs, lows


def _m1_trend(data: pd.DataFrame) -> Dict[str, Any]:
    """Determina la estructura M1 exclusivamente con OHLC."""
    if len(data) < 12:
        return {"structure": "range", "reason": "pocas velas M1"}

    # La ultima vela es la que produjo el cruce; no debe crear la tendencia.
    context = data.iloc[:-1].copy()
    context = context.iloc[-STRUCTURE_BARS:].reset_index(drop=True)

    highs, lows = _pivot_points(context, SWING_LOOKBACK)

    if len(highs) >= 2 and len(lows) >= 2:
        h1_i, h1 = highs[-2]
        h2_i, h2 = highs[-1]
        l1_i, l1 = lows[-2]
        l2_i, l2 = lows[-1]

        hh = h2 > h1
        hl = l2 > l1
        lh = h2 < h1
        ll = l2 < l1

        if hh and hl:
            structure = "bullish"
        elif lh and ll:
            structure = "bearish"
        else:
            structure = "range"

        return {
            "structure": structure,
            "method": "M1_HH_HL_LH_LL",
            "higher_high": hh,
            "higher_low": hl,
            "lower_high": lh,
            "lower_low": ll,
            "previous_swing_high": h1,
            "latest_swing_high": h2,
            "previous_swing_low": l1,
            "latest_swing_low": l2,
            "high_indices": [h1_i, h2_i],
            "low_indices": [l1_i, l2_i],
        }

    # Fallback conservador: tres de las ultimas cinco velas y desplazamiento neto.
    recent = context.iloc[-5:]
    dirs = [_direction(row) for _, row in recent.iterrows()]
    bullish_count = dirs.count("bullish")
    bearish_count = dirs.count("bearish")
    net_move = float(recent.iloc[-1]["close"]) - float(recent.iloc[0]["close"])

    if bullish_count >= 3 and net_move > 0:
        structure = "bullish"
    elif bearish_count >= 3 and net_move < 0:
        structure = "bearish"
    else:
        structure = "range"

    return {
        "structure": structure,
        "method": "M1_candles_fallback",
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "net_move": net_move,
    }


def _cross(ci: pd.Series) -> Dict[str, Any]:
    if len(ci) < 2 or pd.isna(ci.iloc[-2]) or pd.isna(ci.iloc[-1]):
        return {"valid": False, "type": None, "reason": "CI insuficiente"}

    prev = float(ci.iloc[-2])
    curr = float(ci.iloc[-1])
    delta = curr - prev

    call_cross = (
        prev <= OVERBOUGHT
        and curr > OVERBOUGHT
        and delta >= MIN_CI_CROSS_DELTA
        and (curr - OVERBOUGHT) >= MIN_CI_PENETRATION
    )
    put_cross = (
        prev >= OVERSOLD
        and curr < OVERSOLD
        and (-delta) >= MIN_CI_CROSS_DELTA
        and (OVERSOLD - curr) >= MIN_CI_PENETRATION
    )

    if call_cross:
        return {
            "valid": True,
            "type": "call",
            "previous": prev,
            "current": curr,
            "delta": delta,
            "level": OVERBOUGHT,
        }

    if put_cross:
        return {
            "valid": True,
            "type": "put",
            "previous": prev,
            "current": curr,
            "delta": delta,
            "level": OVERSOLD,
        }

    return {
        "valid": False,
        "type": None,
        "previous": prev,
        "current": curr,
        "delta": delta,
        "reason": "sin cruce valido",
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    del pair, higher_tf_df, kwargs

    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _empty(f"historial M1 insuficiente {len(data)}/{MIN_BARS}")

    ci = _choppiness_index(data)
    trend = _m1_trend(data)
    cross = _cross(ci)

    base_analysis = {
        "analysis_timeframe": "M1",
        "trend_timeframe": "M1",
        "trend": trend,
        "choppiness_period": CI_PERIOD,
        "overbought": OVERBOUGHT,
        "oversold": OVERSOLD,
        "cross": cross,
        "last_candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None,
    }

    if not cross.get("valid"):
        r = _empty(
            f"M1 {trend['structure']} | sin cruce valido | "
            f"CI {cross.get('previous', float('nan')):.2f}->{cross.get('current', float('nan')):.2f}"
        )
        r["trend"] = trend["structure"]
        r["direction"] = trend["structure"]
        r["analysis"] = base_analysis
        return r

    signal = cross["type"]
    structure = trend["structure"]

    # REGLA PRINCIPAL: cruce a favor de la tendencia M1.
    if signal == "call" and structure != "bullish":
        r = _empty(
            f"CALL bloqueado | tendencia M1={structure} | "
            f"CI {cross['previous']:.2f}->{cross['current']:.2f} cruza 61.8"
        )
        r["trend"] = structure
        r["direction"] = structure
        r["analysis"] = base_analysis
        return r

    if signal == "put" and structure != "bearish":
        r = _empty(
            f"PUT bloqueado | tendencia M1={structure} | "
            f"CI {cross['previous']:.2f}->{cross['current']:.2f} cruza 38.2"
        )
        r["trend"] = structure
        r["direction"] = structure
        r["analysis"] = base_analysis
        return r

    if signal == "call":
        reason = (
            f"CALL | tendencia M1 ALCISTA | CI 14 cruza ARRIBA 61.8 "
            f"({cross['previous']:.2f}->{cross['current']:.2f})"
        )
        score = 100
    else:
        reason = (
            f"PUT | tendencia M1 BAJISTA | CI 14 cruza ABAJO 38.2 "
            f"({cross['previous']:.2f}->{cross['current']:.2f})"
        )
        score = 100

    return {
        "signal": signal,
        "direction": signal,
        "trend": structure,
        "higher_trend": "M1",
        "reason": reason,
        "score": score,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "analysis": base_analysis,
    }


def get_signal(df: Optional[pd.DataFrame], **kwargs: Any) -> Optional[str]:
    return analyze_market(df=df, **kwargs).get("signal")


def signal(df: Optional[pd.DataFrame], **kwargs: Any) -> Optional[str]:
    return get_signal(df, **kwargs)
