from __future__ import annotations

import pandas as pd


# Compatibilidad con bot.py
M1 = 60
WINDOW = 3

# Umbral de fuerza: el cuerpo debe ocupar al menos el 60% del rango.
IMPULSE_BODY_RATIO = 0.60


def _prepare(data):
    """Valida y normaliza las columnas OHLC recibidas por bot.py."""
    if data is None or not isinstance(data, pd.DataFrame) or data.empty:
        return None

    df = data.copy()

    # Algunos streams pueden entregar max/min en vez de high/low.
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
    """
    Estrategia Momentum M1: vela de fuerza + ruptura de la vela anterior.

    CALL:
      - Vela actual alcista.
      - Cuerpo >= 60% de su rango.
      - Máximo actual > máximo de la vela anterior.

    PUT:
      - Vela actual bajista.
      - Cuerpo >= 60% de su rango.
      - Mínimo actual < mínimo de la vela anterior.

    La función devuelve las claves que consume bot.py:
    signal, force_candle, price_action_confirmed, reason y analysis.
    """
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
    if force_candle and direction == "call" and broke_high:
        signal = "call"
    elif force_candle and direction == "put" and broke_low:
        signal = "put"

    confirmed = signal in ("call", "put")

    if signal == "call":
        reason = (
            f"CALL | vela alcista fuerte ({body_ratio:.1%} de cuerpo) "
            "y ruptura del máximo anterior"
        )
    elif signal == "put":
        reason = (
            f"PUT | vela bajista fuerte ({body_ratio:.1%} de cuerpo) "
            "y ruptura del mínimo anterior"
        )
    else:
        reason = "Sin ruptura direccional con vela de fuerza"

    return {
        "signal": signal,
        "prediction": signal.upper() if signal else "NO SIGNAL",
        "force_candle": force_candle,
        "price_action_confirmed": confirmed,
        "reason": reason,
        "analysis": {
            "pair": pair,
            "mode": mode,
            "timeframe": "M1",
            "direction": direction,
            "body_ratio": body_ratio,
            "range": candle_range,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "broke_previous_high": broke_high,
            "broke_previous_low": broke_low,
            "impulse_body_threshold": IMPULSE_BODY_RATIO,
        },
    }


# Alias por compatibilidad con módulos que utilicen el nombre analyze.
def analyze(data, pair=None, mode="M1_M1"):
    return analyze_market(data, pair=pair, mode=mode)
