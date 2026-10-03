from __future__ import annotations

import pandas as pd

# Estrategia M1 de fuerza + ruptura de la vela previa.
IMPULSE_BODY_RATIO = 0.60


def _prepare(data):
    if data is None or not isinstance(data, pd.DataFrame) or data.empty:
        return None

    df = data.copy()
    if "high" not in df.columns and "max" in df.columns:
        df["high"] = df["max"]
    if "low" not in df.columns and "min" in df.columns:
        df["low"] = df["min"]

    required = ["open", "high", "low", "close"]
    if any(column not in df.columns for column in required):
        return None

    for column in required:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=required).reset_index(drop=True)
    return df if not df.empty else None


def analyze_market(data, pair=None, mode="M1_M1"):
    no_signal = {
        "signal": None,
        "prediction": "NO SIGNAL",
        "force_candle": False,
        "price_action_confirmed": False,
        "reason": "Datos insuficientes o sin señal",
        "analysis": {},
    }

    df = _prepare(data)
    if df is None or len(df) < 2:
        return no_signal

    current = df.iloc[-1]
    previous = df.iloc[-2]

    opening = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])
    previous_high = float(previous["high"])
    previous_low = float(previous["low"])

    candle_range = high - low
    if candle_range <= 0 or previous_high <= previous_low:
        return {
            **no_signal,
            "reason": "Vela actual o anterior sin rango válido",
        }

    body_ratio = abs(close - opening) / candle_range
    direction = "call" if close > opening else "put" if close < opening else None
    force_candle = body_ratio >= IMPULSE_BODY_RATIO

    broke_high = high > previous_high
    broke_low = low < previous_low

    signal = None
    reason = "Sin ruptura direccional con vela de fuerza"

    if force_candle and direction == "call" and broke_high:
        signal = "call"
        reason = (
            f"CALL | vela alcista fuerte ({body_ratio:.2f} del rango) "
            "y ruptura del máximo anterior"
        )
    elif force_candle and direction == "put" and broke_low:
        signal = "put"
        reason = (
            f"PUT | vela bajista fuerte ({body_ratio:.2f} del rango) "
            "y ruptura del mínimo anterior"
        )

    confirmed = signal in ("call", "put")

    return {
        "signal": signal,
        "prediction": signal.upper() if signal else "NO SIGNAL",
        "force_candle": force_candle,
        "price_action_confirmed": confirmed,
        "reason": reason,
        "analysis": {
            "pair": pair,
            "mode": mode,
            "body_ratio": body_ratio,
            "direction": direction,
            "broke_high": broke_high,
            "broke_low": broke_low,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "current_open": opening,
            "current_high": high,
            "current_low": low,
            "current_close": close,
        },
    }


def analyze(data, pair=None, mode="M1_M1"):
    """Alias de compatibilidad para integraciones que llamen analyze()."""
    return analyze_market(data, pair=pair, mode=mode)
