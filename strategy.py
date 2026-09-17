"""
strategy.py

CONTINUIDAD FILTRADA - Binary OTC M1

Patron obligatorio para preparar la entrada en la apertura de la cuarta vela:

CALL:
  Vela 1: roja con señal de continuidad bajista.
  Vela 2: sin señal de continuidad.
  Vela 3: verde con señal de continuidad alcista (vela de señal).
  Vela 4: entrada CALL en la apertura, en 00 (modo sniper del bot).

PUT:
  Vela 1: verde con señal de continuidad alcista.
  Vela 2: sin señal de continuidad.
  Vela 3: roja con señal de continuidad bajista (vela de señal).
  Vela 4: entrada PUT en la apertura, en 00 (modo sniper del bot).

Este modulo solamente analiza; no ejecuta operaciones.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import math

import numpy as np
import pandas as pd


# -------------------- Configuracion --------------------

MIN_BARS = 25
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
        columns={
            "max": "high",
            "min": "low",
            "timestamp": "from",
        },
        inplace=True,
    )

    required = ["open", "high", "low", "close"]

    if any(column not in out.columns for column in required):
        return pd.DataFrame()

    for column in required:
        out[column] = pd.to_numeric(
            out[column],
            errors="coerce",
        )

    if "from" in out.columns:
        out["from"] = pd.to_numeric(
            out["from"],
            errors="coerce",
        )

        out.dropna(
            subset=["from"],
            inplace=True,
        )

        out["from"] = out["from"].astype(int)

        out.sort_values(
            "from",
            inplace=True,
        )

        out.drop_duplicates(
            "from",
            keep="last",
            inplace=True,
        )

    out.dropna(
        subset=required,
        inplace=True,
    )

    out.reset_index(
        drop=True,
        inplace=True,
    )

    return out.tail(
        MAX_CANDLES
    ).reset_index(
        drop=True
    )


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

    current_df = _normalize(
        pd.DataFrame([current])
    )

    if current_df.empty:
        return history

    return _normalize(
        pd.concat(
            [
                history,
                current_df,
            ],
            ignore_index=True,
        )
    )


def _true_range(data: pd.DataFrame) -> pd.Series:
    previous_close = data["close"].shift(1)

    return pd.concat(
        [
            data["high"] - data["low"],
            (
                data["high"] - previous_close
            ).abs(),
            (
                data["low"] - previous_close
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _candle_metrics(
    row: pd.Series,
) -> Dict[str, float]:

    opening = _safe_float(
        row.get("open")
    )

    high = _safe_float(
        row.get("high")
    )

    low = _safe_float(
        row.get("low")
    )

    close = _safe_float(
        row.get("close")
    )

    candle_range = max(
        high - low,
        EPS,
    )

    body = abs(
        close - opening
    )

    return {
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "range": candle_range,
        "body": body,
        "upper": max(
            high - max(opening, close),
            0.0,
        ),
        "lower": max(
            min(opening, close) - low,
            0.0,
        ),
        "body_ratio": body / candle_range,
        "close_position": (
            close - low
        ) / candle_range,
    }


def _continuity_signal(
    data: pd.DataFrame,
    index: int,
) -> Optional[str]:
    """
    Aplica literalmente las condiciones
    del script CONTINUIDAD FILTRADA.
    """

    if index < 1 or index >= len(data):
        return None

    current = _candle_metrics(
        data.iloc[index]
    )

    previous = _candle_metrics(
        data.iloc[index - 1]
    )

    average_start = max(
        0,
        index - FORCE_PERIOD + 1,
    )

    bodies = (
        data.iloc[
            average_start:index + 1
        ]["close"]
        - data.iloc[
            average_start:index + 1
        ]["open"]
    ).abs()

    average_body = _safe_float(
        bodies.mean(),
        0.0,
    )

    if average_body <= 0.0:
        return None

    strong_candle = (
        current["body"]
        >= average_body
        * FORCE_PERCENT
        / 100.0
    )

    small_upper_wick = (
        current["upper"]
        <= current["body"]
        * WICK_PERCENT
        / 100.0
    )

    small_lower_wick = (
        current["lower"]
        <= current["body"]
        * WICK_PERCENT
        / 100.0
    )

    buy_continuation = (
        current["close"] > current["open"]
        and previous["close"] > previous["open"]
        and strong_candle
        and current["close"] > previous["high"]
        and small_upper_wick
    )

    sell_continuation = (
        current["close"] < current["open"]
        and previous["close"] < previous["open"]
        and strong_candle
        and current["close"] < previous["low"]
        and small_lower_wick
    )

    if buy_continuation and not sell_continuation:
        return "call"

    if sell_continuation and not buy_continuation:
        return "put"

    return None


def _atr(
    data: pd.DataFrame,
) -> pd.Series:

    return _true_range(
        data
    ).rolling(
        ATR_PERIOD,
        min_periods=ATR_PERIOD,
    ).mean()


def _recent_levels(
    data: pd.DataFrame,
) -> tuple[float, float]:

    window = (
        data.tail(
            SWING_LOOKBACK + 1
        ).iloc[:-1]
    )

    if window.empty:
        return np.nan, np.nan

    return (
        float(window["high"].max()),
        float(window["low"].min()),
    )


def _empty_result(
    reason: str = "Sin señal",
) -> Dict[str, Any]:

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
        return _empty_result(
            f"Historial insuficiente "
            f"{len(data)}/{MIN_BARS}"
        )

    atr_series = _atr(data)

    current_index = len(data) - 1

    if current_index < 3:
        return _empty_result(
            "No hay suficientes velas "
            "para el patron 1-2-3-4"
        )

    candle_1_index = current_index - 2
    candle_2_index = current_index - 1
    candle_3_index = current_index

    signal_1 = _continuity_signal(
        data,
        candle_1_index,
    )

    signal_2 = _continuity_signal(
        data,
        candle_2_index,
    )

    signal_3 = _continuity_signal(
        data,
        candle_3_index,
    )

    current_atr = _safe_float(
        atr_series.iloc[current_index],
        0.0,
    )

    last_swing_high, last_swing_low = _recent_levels(
        data
    )

    current_metrics = _candle_metrics(
        data.iloc[current_index]
    )

    base_analysis: Dict[str, Any] = {
        "pair": pair,
        "atr": current_atr,
        "atr_stop": np.nan,
        "atr_position": 0,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "candle": current_metrics,
        "signal_candle": False,
        "execution_mode": "fourth_candle_open_00",
        "expiration_minutes": 3,
        "force": False,
        "pattern": {
            "candle_1": signal_1,
            "candle_2": signal_2,
            "candle_3": signal_3,
            "middle_candle_without_signal": (
                signal_2 is None
            ),
        },
    }

    # CALL:
    # Vela 1 PUT
    # Vela 2 sin señal
    # Vela 3 CALL

    call_pattern = (
        signal_1 == "put"
        and signal_2 is None
        and signal_3 == "call"
    )

    # PUT:
    # Vela 1 CALL
    # Vela 2 sin señal
    # Vela 3 PUT

    put_pattern = (
        signal_1 == "call"
        and signal_2 is None
        and signal_3 == "put"
    )

    if not (
        call_pattern
        or put_pattern
    ):

        result = _empty_result(
            "Sin patron valido: "
            "vela 1 contraria + "
            "vela 2 sin señal + "
            "vela 3 confirmada"
        )

        result["analysis"] = base_analysis

        return result

    signal = (
        "call"
        if call_pattern
        else "put"
    )

    direction = (
        "bullish"
        if signal == "call"
        else "bearish"
    )

    base_analysis["signal_candle"] = True
    base_analysis["force"] = True
    base_analysis["pattern"]["valid"] = True

    candle_timestamp = data.iloc[
        current_index
    ].get("from")

    result = {
        "signal": signal,
        "direction": direction,
        "score": 90,
        "entry_quality": 90,
        "entry_type": "force",
        "blocked": False,
        "reason": (
            f"{signal.upper()} | "
            "patron 1-2-3 confirmado | "
            "vela intermedia sin señal | "
            "ejecutar en apertura de vela 4 (00)"
        ),
        "signal_price": current_metrics["close"],
        "candle_timestamp": (
            int(candle_timestamp)
            if pd.notna(candle_timestamp)
            else None
        ),
        "analysis": base_analysis,
    }

    return result


def get_signal(
    df: pd.DataFrame,
) -> Optional[str]:

    return analyze_market(
        df=df
    ).get("signal")


def signal(
    df: pd.DataFrame,
) -> Optional[str]:

    return get_signal(df)


if __name__ == "__main__":
    print(
        "strategy.py cargado correctamente: "
        "CONTINUIDAD FILTRADA 1-2-3-4"
    )
