"""
strategy.py

Estrategia CONTINUIDAD FILTRADA + SECUENCIA DE ENTRADA:

CALL:
    1) Vela roja con señal de continuidad bajista.
    2) Una vela sin señal de continuidad.
    3) Vela verde con señal de continuidad alcista.
    4) El bot prepara CALL para la apertura de la siguiente vela (N+1).

PUT:
    1) Vela verde con señal de continuidad alcista.
    2) Una vela sin señal de continuidad.
    3) Vela roja con señal de continuidad bajista.
    4) El bot prepara PUT para la apertura de la siguiente vela (N+1).

La lógica de continuidad replica el script:
    - Periodo promedio de cuerpos: 10
    - Fuerza mínima: 120% del promedio
    - Mecha máxima: 50% del cuerpo
    - Ruptura del máximo/mínimo de la vela anterior

Este módulo solamente analiza; no ejecuta operaciones ni decide la expiración.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import math

import numpy as np
import pandas as pd

# -------------------- Configuración --------------------
MIN_BARS = 35
MAX_CANDLES = 120

FORCE_PERIOD = 10
FORCE_PERCENT = 120.0
WICK_PERCENT = 50.0

ATR_PERIOD = 14
SWING_LOOKBACK = 8
EPS = 1e-10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out.rename(
        columns={"max": "high", "min": "low", "timestamp": "from"},
        inplace=True,
    )

    required = ["open", "high", "low", "close"]
    if any(column not in out.columns for column in required):
        return pd.DataFrame()

    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")

    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out["from"] = out["from"].astype(int)
        out.sort_values("from", inplace=True)
        out.drop_duplicates("from", keep="last", inplace=True)

    out.dropna(subset=required, inplace=True)
    out.reset_index(drop=True, inplace=True)
    return out.tail(MAX_CANDLES).reset_index(drop=True)


def _build_dataframe(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if df is not None:
        return _normalize(df)

    history = _normalize(previous_m1)

    if isinstance(candle_1m, pd.Series):
        current = candle_1m.to_dict()
    elif isinstance(candle_1m, dict):
        current = dict(candle_1m)
    else:
        return history

    current_df = _normalize(pd.DataFrame([current]))
    if current_df.empty:
        return history

    return _normalize(pd.concat([history, current_df], ignore_index=True))


def _true_range(data: pd.DataFrame) -> pd.Series:
    previous_close = data["close"].shift(1)
    return pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _atr(data: pd.DataFrame) -> pd.Series:
    return _true_range(data).rolling(ATR_PERIOD, min_periods=1).mean()


def _candle_metrics(row: pd.Series) -> Dict[str, float]:
    open_price = _safe_float(row.get("open"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))

    candle_range = max(high - low, EPS)
    body = abs(close - open_price)
    upper_wick = max(high - max(close, open_price), 0.0)
    lower_wick = max(min(close, open_price) - low, 0.0)

    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "range": candle_range,
        "body": body,
        "upper": upper_wick,
        "lower": lower_wick,
        "body_ratio": body / candle_range,
        "close_position": (close - low) / candle_range,
    }


def _add_continuity_columns(data: pd.DataFrame) -> pd.DataFrame:
    out = data.copy()
    out["candle_body"] = (out["close"] - out["open"]).abs()
    out["average_body"] = out["candle_body"].rolling(
        FORCE_PERIOD, min_periods=FORCE_PERIOD
    ).mean()
    out["upper_wick"] = out["high"] - out[["close", "open"]].max(axis=1)
    out["lower_wick"] = out[["close", "open"]].min(axis=1) - out["low"]

    out["strong_candle"] = (
        out["average_body"].notna()
        & (out["candle_body"] >= out["average_body"] * FORCE_PERCENT / 100.0)
    )
    out["small_upper_wick"] = (
        out["candle_body"] > EPS
    ) & (out["upper_wick"] <= out["candle_body"] * WICK_PERCENT / 100.0)
    out["small_lower_wick"] = (
        out["candle_body"] > EPS
    ) & (out["lower_wick"] <= out["candle_body"] * WICK_PERCENT / 100.0)

    previous_open = out["open"].shift(1)
    previous_close = out["close"].shift(1)
    previous_high = out["high"].shift(1)
    previous_low = out["low"].shift(1)

    out["buy_continuation"] = (
        (out["close"] > out["open"])
        & (previous_close > previous_open)
        & out["strong_candle"]
        & (out["close"] > previous_high)
        & out["small_upper_wick"]
    )

    out["sell_continuation"] = (
        (out["close"] < out["open"])
        & (previous_close < previous_open)
        & out["strong_candle"]
        & (out["close"] < previous_low)
        & out["small_lower_wick"]
    )

    out["continuation_signal"] = np.select(
        [out["buy_continuation"], out["sell_continuation"]],
        ["call", "put"],
        default=None,
    )
    return out


def _recent_levels(data: pd.DataFrame) -> tuple[float, float]:
    window = data.tail(SWING_LOOKBACK + 1).iloc[:-1]
    if window.empty:
        return np.nan, np.nan
    return float(window["high"].max()), float(window["low"].min())


def _empty_result(reason: str = "Sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "score": 0,
        "entry_quality": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason,
        "analysis": {},
    }


def _pattern_description(signal: str) -> str:
    if signal == "call":
        return "PUT continuidad → vela sin señal → CALL continuidad → entrada CALL en N+1"
    return "CALL continuidad → vela sin señal → PUT continuidad → entrada PUT en N+1"


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    data = _build_dataframe(
        df=df,
        candle_1m=candle_1m,
        previous_m1=previous_m1,
    )

    if len(data) < MIN_BARS:
        return _empty_result(f"Historial insuficiente {len(data)}/{MIN_BARS}")

    ind = _add_continuity_columns(data)
    if len(ind) < 3:
        return _empty_result("Se necesitan 3 velas para validar la secuencia")

    current = ind.iloc[-1]
    middle = ind.iloc[-2]
    first = ind.iloc[-3]

    current_metrics = _candle_metrics(current)
    atr_series = _atr(ind)
    atr = _safe_float(atr_series.iloc[-1], 0.0)
    last_swing_high, last_swing_low = _recent_levels(ind)

    first_signal = first.get("continuation_signal")
    middle_signal = middle.get("continuation_signal")
    current_signal = current.get("continuation_signal")

    # La segunda vela debe estar completamente libre de señal.
    middle_without_signal = pd.isna(middle_signal) or middle_signal is None

    call_sequence = (
        first_signal == "put"
        and middle_without_signal
        and current_signal == "call"
    )
    put_sequence = (
        first_signal == "call"
        and middle_without_signal
        and current_signal == "put"
    )

    signal: Optional[str] = None
    if call_sequence:
        signal = "call"
    elif put_sequence:
        signal = "put"

    base_analysis: Dict[str, Any] = {
        "pair": pair,
        "force": True,
        "structure": "continuidad_filtrada",
        "impulse_phase": "secuencia_confirmada" if signal else "sin_secuencia",
        "execution_mode": "next_candle_sniper",
        "expiration_minutes": 3,
        "atr": atr,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "signal_candle": bool(current_signal),
        "first_candle_signal": first_signal,
        "middle_candle_signal": middle_signal,
        "current_candle_signal": current_signal,
        "middle_without_signal": bool(middle_without_signal),
        "sequence_confirmed": bool(signal),
        "first_candle": _candle_metrics(first),
        "middle_candle": _candle_metrics(middle),
        "candle": current_metrics,
        "force_period": FORCE_PERIOD,
        "force_percent": FORCE_PERCENT,
        "wick_percent": WICK_PERCENT,
    }

    if signal is None:
        result = _empty_result(
            "Sin secuencia: continuidad inicial + vela sin señal + continuidad contraria"
        )
        result["analysis"] = base_analysis
        return result

    reason = (
        f"{_pattern_description(signal)} | "
        "entrada en apertura de la cuarta vela (00)"
    )

    return {
        "signal": signal,
        "direction": "bullish" if signal == "call" else "bearish",
        "score": 100,
        "entry_quality": 100,
        "entry_type": "force",
        "blocked": False,
        "reason": reason,
        "signal_price": current_metrics["close"],
        "candle_timestamp": (
            int(current["from"])
            if "from" in current and pd.notna(current["from"])
            else None
        ),
        "analysis": base_analysis,
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente: CONTINUIDAD FILTRADA + SECUENCIA N+1")
