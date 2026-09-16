"""Estrategia Bollinger Bands + ATR Trailing Stops para Binary OTC M1.

La señal se calcula sobre la última vela cerrada (N) y el bot ejecuta en N+1.
Parámetros del indicador mostrado por el usuario:
- ATR period = 14
- ATR multiplier = 2
- HighLow = False: la base del trailing stop usa cierres
- Bollinger Bands = periodo 20, desviación 2
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import math
import numpy as np
import pandas as pd

BB_PERIOD = 20
BB_STDDEV = 2.0
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
MIN_BARS = 35
MIN_SCORE = 70


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        if col not in out.columns:
            return pd.DataFrame()
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["open", "high", "low", "close"])
    if "from" in out.columns:
        out = out.sort_values("from")
    return out.reset_index(drop=True)


def _true_range(df: pd.DataFrame) -> pd.Series:
    previous_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - previous_close).abs(),
         (df["low"] - previous_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def _rma(series: pd.Series, period: int) -> pd.Series:
    # Equivalente práctico a ta.rma / Wilder smoothing.
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _atr_trailing_stop(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
    close = df["close"]
    tr = _true_range(df)
    atr = _rma(tr, ATR_PERIOD) * ATR_MULTIPLIER
    high_source = close  # HighLow=False
    low_source = close   # HighLow=False

    stop = pd.Series(np.nan, index=df.index, dtype=float)
    position = pd.Series(0, index=df.index, dtype=int)

    for i in range(len(df)):
        if i == 0 or pd.isna(atr.iloc[i]):
            continue
        loss = float(atr.iloc[i])
        h = float(high_source.iloc[i])
        l = float(low_source.iloc[i])
        prev_stop = stop.iloc[i - 1] if i > 0 else np.nan
        prev_pos = int(position.iloc[i - 1]) if i > 0 else 0
        prev_close = float(close.iloc[i - 1])

        if pd.isna(prev_stop):
            stop.iloc[i] = h - loss if close.iloc[i] >= prev_close else l + loss
            position.iloc[i] = 1 if close.iloc[i] >= prev_close else -1
            continue

        if close.iloc[i] > prev_stop and prev_close > prev_stop:
            next_stop = max(float(prev_stop), h - loss)
        elif close.iloc[i] < prev_stop and prev_close < prev_stop:
            next_stop = min(float(prev_stop), l + loss)
        elif close.iloc[i] > prev_stop:
            next_stop = h - loss
        else:
            next_stop = l + loss

        stop.iloc[i] = next_stop
        if close.iloc[i] > prev_stop:
            position.iloc[i] = 1
        elif close.iloc[i] < prev_stop:
            position.iloc[i] = -1
        else:
            position.iloc[i] = prev_pos or (1 if close.iloc[i] >= next_stop else -1)

    return atr, stop, position


def _bollinger(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
    middle = df["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
    deviation = df["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
    return middle, middle + BB_STDDEV * deviation, middle - BB_STDDEV * deviation


def _candle_metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "range": rng,
        "body": body,
        "body_ratio": body / rng,
        "upper_wick": h - max(o, c),
        "lower_wick": min(o, c) - l,
        "close_position": (c - l) / rng,
        "bullish": c > o,
        "bearish": c < o,
    }


def _signal_for_last_candle(df: pd.DataFrame) -> Dict[str, Any]:
    middle, upper, lower = _bollinger(df)
    atr, trailing, position = _atr_trailing_stop(df)
    i = len(df) - 1
    row = df.iloc[i]
    metrics = _candle_metrics(row)

    if any(pd.isna(x.iloc[i]) for x in (middle, upper, lower, atr, trailing)):
        return {"signal": None, "score": 0, "reason": "Datos insuficientes para Bollinger/ATR.",
                "analysis": {"ready": False}}

    previous_position = int(position.iloc[i - 1]) if i > 0 else 0
    current_position = int(position.iloc[i])
    stop_now = float(trailing.iloc[i])
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    upper_now = float(upper.iloc[i])
    lower_now = float(lower.iloc[i])
    bb_width = max(upper_now - lower_now, 1e-12)

    # Rechazo: la mecha toca/supera la banda y el cierre vuelve hacia dentro.
    lower_rejection = low <= lower_now and close > lower_now and metrics["bullish"]
    upper_rejection = high >= upper_now and close < upper_now and metrics["bearish"]

    # Confirmación del ATR Trailing Stop: cruce de posición o stop situado al lado correcto.
    call_atr = current_position == 1 and (previous_position != 1 or close > stop_now)
    put_atr = current_position == -1 and (previous_position != -1 or close < stop_now)

    # Evita señales con vela excesivamente pequeña o sin rechazo claro.
    valid_body = metrics["body_ratio"] >= 0.20
    call = lower_rejection and call_atr and valid_body
    put = upper_rejection and put_atr and valid_body

    score = 0
    reasons = []
    if lower_rejection or upper_rejection:
        score += 40
        reasons.append("rechazo de banda Bollinger")
    if call_atr or put_atr:
        score += 35
        reasons.append("ATR Trailing Stop confirma dirección")
    if valid_body:
        score += 15
        reasons.append("cuerpo válido")
    if metrics["body_ratio"] >= 0.45:
        score += 10
        reasons.append("vela con decisión")

    signal = "call" if call else "put" if put else None
    direction = "bullish" if signal == "call" else "bearish" if signal == "put" else "neutral"
    reason = "; ".join(reasons) if reasons else "No existe rechazo confirmado con ATR Trailing Stop."

    return {
        "signal": signal,
        "direction": direction,
        "score": min(score, 100) if signal else 0,
        "entry_type": "bollinger_atr" if signal else "none",
        "entry_quality": min(score, 100) if signal else 0,
        "reason": reason,
        "analysis": {
            "ready": True,
            "bollinger_period": BB_PERIOD,
            "bollinger_stddev": BB_STDDEV,
            "atr_period": ATR_PERIOD,
            "atr_multiplier": ATR_MULTIPLIER,
            "high_low": False,
            "atr": float(atr.iloc[i]),
            "atr_trailing_stop": stop_now,
            "atr_position": current_position,
            "previous_atr_position": previous_position,
            "bb_middle": float(middle.iloc[i]),
            "bb_upper": upper_now,
            "bb_lower": lower_now,
            "bb_width": bb_width,
            "lower_rejection": bool(lower_rejection),
            "upper_rejection": bool(upper_rejection),
            "force": bool(signal),
            "pullback": {"valid": True, "previous_candle_confirmed": True, "extreme_confirmed": True},
            "last_swing_high": (None, upper_now),
            "last_swing_low": (None, lower_now),
        },
    }


def analyze_market(candle_1m: Dict[str, Any], previous_m1: Optional[pd.DataFrame] = None,
                   pair: Optional[str] = None, **_: Any) -> Dict[str, Any]:
    previous = _normalize(previous_m1)
    current = pd.DataFrame([candle_1m])
    df = _normalize(pd.concat([previous, current], ignore_index=True))
    if len(df) < MIN_BARS:
        return {"signal": None, "direction": "neutral", "score": 0, "entry_type": "none",
                "entry_quality": 0, "reason": f"Historial insuficiente: {len(df)}/{MIN_BARS} velas.",
                "analysis": {"ready": False, "pair": pair}}
    result = _signal_for_last_candle(df.tail(200).reset_index(drop=True))
    result.setdefault("analysis", {})["pair"] = pair
    result["analysis"]["candle_timestamp"] = candle_1m.get("from")
    return result


def get_signal(*args: Any, **kwargs: Any) -> Optional[str]:
    return analyze_market(*args, **kwargs).get("signal")


def signal(*args: Any, **kwargs: Any) -> Optional[str]:
    return get_signal(*args, **kwargs)
