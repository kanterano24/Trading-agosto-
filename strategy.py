from __future__ import annotations

import pandas as pd

# QUANT MODE M1: ruptura + retesteo + rechazo.
# La señal solo se genera cuando la vela cerrada más reciente confirma
# un retesteo de una ruptura ocurrida en la vela inmediatamente anterior.
IMPULSE_BODY_RATIO = 0.60
RETEST_TOLERANCE_RATIO = 0.20


def _prepare(data):
    """Normaliza y valida el DataFrame de velas."""
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


def _no_signal(reason="Sin confirmación de ruptura + retesteo + rechazo", analysis=None):
    return {
        "signal": None,
        "prediction": "NO SIGNAL",
        "force_candle": False,
        "price_action_confirmed": False,
        "reason": reason,
        "analysis": analysis or {},
    }


def analyze_market(data, pair=None, mode="M1_M1"):
    """
    Analiza velas cerradas M1 y busca esta secuencia:

    CALL:
      1. La vela de ruptura cierra por encima del máximo previo.
      2. La vela siguiente retrocede hasta la zona rota (con tolerancia).
      3. Esa vela rechaza la zona y cierra alcista, por encima del nivel.

    PUT:
      1. La vela de ruptura cierra por debajo del mínimo previo.
      2. La vela siguiente retrocede hasta la zona rota (con tolerancia).
      3. Esa vela rechaza la zona y cierra bajista, por debajo del nivel.

    La función conserva las claves de respuesta de la estrategia anterior.
    Requiere al menos tres velas cerradas: referencia, ruptura y retesteo.
    """
    df = _prepare(data)
    if df is None or len(df) < 3:
        return _no_signal("Se necesitan al menos 3 velas cerradas")

    reference = df.iloc[-3]
    breakout = df.iloc[-2]
    retest = df.iloc[-1]

    ref_high = float(reference["high"])
    ref_low = float(reference["low"])

    bo_open = float(breakout["open"])
    bo_high = float(breakout["high"])
    bo_low = float(breakout["low"])
    bo_close = float(breakout["close"])

    rt_open = float(retest["open"])
    rt_high = float(retest["high"])
    rt_low = float(retest["low"])
    rt_close = float(retest["close"])

    breakout_range = bo_high - bo_low
    retest_range = rt_high - rt_low

    if breakout_range <= 0 or retest_range <= 0 or ref_high <= ref_low:
        return _no_signal("Una de las velas no tiene un rango válido")

    breakout_body_ratio = abs(bo_close - bo_open) / breakout_range
    retest_body_ratio = abs(rt_close - rt_open) / retest_range

    breakout_up = bo_close > ref_high and bo_close > bo_open
    breakout_down = bo_close < ref_low and bo_close < bo_open
    breakout_strong = breakout_body_ratio >= IMPULSE_BODY_RATIO

    # La tolerancia se calcula con el rango de la vela de ruptura.
    tolerance = breakout_range * RETEST_TOLERANCE_RATIO

    # Retesteo CALL: el mínimo vuelve cerca del máximo roto y el cierre
    # recupera/quiebra el nivel con una vela alcista.
    call_retest = (
        breakout_up
        and breakout_strong
        and rt_low <= ref_high + tolerance
        and rt_low >= ref_high - tolerance
        and rt_close > ref_high
        and rt_close > rt_open
    )

    # Retesteo PUT: el máximo vuelve cerca del mínimo roto y el cierre
    # queda por debajo del nivel con una vela bajista.
    put_retest = (
        breakout_down
        and breakout_strong
        and rt_high >= ref_low - tolerance
        and rt_high <= ref_low + tolerance
        and rt_close < ref_low
        and rt_close < rt_open
    )

    signal = "call" if call_retest else "put" if put_retest else None

    if signal == "call":
        reason = (
            f"CALL | ruptura alcista fuerte ({breakout_body_ratio:.2f} del rango), "
            f"retest del máximo anterior y rechazo alcista "
            f"(vela de confirmación {retest_body_ratio:.2f})"
        )
    elif signal == "put":
        reason = (
            f"PUT | ruptura bajista fuerte ({breakout_body_ratio:.2f} del rango), "
            f"retest del mínimo anterior y rechazo bajista "
            f"(vela de confirmación {retest_body_ratio:.2f})"
        )
    else:
        reason = "Sin secuencia válida de ruptura + retesteo + rechazo"

    confirmed = signal in ("call", "put")

    return {
        "signal": signal,
        "prediction": signal.upper() if signal else "NO SIGNAL",
        "force_candle": breakout_strong,
        "price_action_confirmed": confirmed,
        "reason": reason,
        "analysis": {
            "pair": pair,
            "mode": mode,
            "signal_type": "breakout_retest_rejection",
            "breakout_body_ratio": breakout_body_ratio,
            "retest_body_ratio": retest_body_ratio,
            "breakout_up": breakout_up,
            "breakout_down": breakout_down,
            "breakout_strong": breakout_strong,
            "call_retest": call_retest,
            "put_retest": put_retest,
            "reference_high": ref_high,
            "reference_low": ref_low,
            "tolerance": tolerance,
            "breakout_open": bo_open,
            "breakout_high": bo_high,
            "breakout_low": bo_low,
            "breakout_close": bo_close,
            "retest_open": rt_open,
            "retest_high": rt_high,
            "retest_low": rt_low,
            "retest_close": rt_close,
        },
    }


def analyze(data, pair=None, mode="M1_M1"):
    """Alias de compatibilidad para integraciones que llamen analyze()."""
    return analyze_market(data, pair=pair, mode=mode)
