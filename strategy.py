from __future__ import annotations

import pandas as pd

# QUANT MODE M1 — reglas experimentales basadas en las operaciones compartidas.
# Estas condiciones buscan filtrar señales de menor calidad; no garantizan ganancias.
IMPULSE_BODY_RATIO = 0.60
CONFIRMATION_BODY_RATIO = 0.45
RETEST_TOLERANCE_RATIO = 0.20
MAX_EXTENSION_RATIO = 0.30


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
    if any(col not in df.columns for col in required):
        return None

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required).reset_index(drop=True)
    return df if not df.empty else None


def _no_signal(reason, analysis=None):
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
    Analiza velas M1 cerradas.

    CALL:
      1) La vela de ruptura cierra alcista por encima del máximo de referencia.
      2) La ruptura tiene cuerpo/rango >= 0.60.
      3) La vela siguiente retestea la zona del máximo roto dentro de tolerancia.
      4) Cierra alcista por encima del nivel, con cuerpo/rango >= 0.45.
      5) La confirmación no queda extendida más de 0.30 del rango de ruptura.

    PUT: reglas simétricas bajo el mínimo de referencia.

    Se requieren al menos tres velas cerradas: referencia, ruptura y retesteo/
    confirmación. El bot debe llamar esta función solo con velas cerradas.
    """
    df = _prepare(data)
    if df is None or len(df) < 3:
        return _no_signal("Se necesitan al menos 3 velas cerradas")

    reference = df.iloc[-3]
    breakout = df.iloc[-2]
    confirm = df.iloc[-1]

    ref_high, ref_low = float(reference.high), float(reference.low)
    bo_open, bo_high = float(breakout.open), float(breakout.high)
    bo_low, bo_close = float(breakout.low), float(breakout.close)
    cf_open, cf_high = float(confirm.open), float(confirm.high)
    cf_low, cf_close = float(confirm.low), float(confirm.close)

    bo_range = bo_high - bo_low
    cf_range = cf_high - cf_low
    if bo_range <= 0 or cf_range <= 0 or ref_high <= ref_low:
        return _no_signal("Rango inválido en las velas analizadas")

    impulse_ratio = abs(bo_close - bo_open) / bo_range
    confirm_ratio = abs(cf_close - cf_open) / cf_range

    breakout_up = bo_close > ref_high and bo_close > bo_open
    breakout_down = bo_close < ref_low and bo_close < bo_open
    impulse_strong = impulse_ratio >= IMPULSE_BODY_RATIO
    confirm_strong = confirm_ratio >= CONFIRMATION_BODY_RATIO
    tolerance = bo_range * RETEST_TOLERANCE_RATIO

    # Extensión medida desde el nivel roto hasta el cierre de confirmación.
    call_extension = max(0.0, cf_close - ref_high) / bo_range
    put_extension = max(0.0, ref_low - cf_close) / bo_range
    extension_ok_call = call_extension <= MAX_EXTENSION_RATIO
    extension_ok_put = put_extension <= MAX_EXTENSION_RATIO

    call_retest = (
        breakout_up and impulse_strong
        and cf_low >= ref_high - tolerance
        and cf_low <= ref_high + tolerance
        and cf_close > ref_high and cf_close > cf_open
        and confirm_strong and extension_ok_call
    )
    put_retest = (
        breakout_down and impulse_strong
        and cf_high >= ref_low - tolerance
        and cf_high <= ref_low + tolerance
        and cf_close < ref_low and cf_close < cf_open
        and confirm_strong and extension_ok_put
    )

    signal = "call" if call_retest else "put" if put_retest else None

    if signal == "call":
        reason = (
            f"CALL | ruptura alcista fuerte ({impulse_ratio:.2f} del rango), "
            f"retest y rechazo alcista; confirmación {confirm_ratio:.2f}; "
            f"extensión {call_extension:.2f} (máx. {MAX_EXTENSION_RATIO:.2f})"
        )
    elif signal == "put":
        reason = (
            f"PUT | ruptura bajista fuerte ({impulse_ratio:.2f} del rango), "
            f"retest y rechazo bajista; confirmación {confirm_ratio:.2f}; "
            f"extensión {put_extension:.2f} (máx. {MAX_EXTENSION_RATIO:.2f})"
        )
    else:
        reasons = []
        if not (breakout_up or breakout_down):
            reasons.append("sin cierre de ruptura")
        elif not impulse_strong:
            reasons.append("impulso débil")
        if not confirm_strong:
            reasons.append("confirmación débil")
        if not (call_retest or put_retest):
            reasons.append("retest/rechazo no confirmado")
        if breakout_up and not extension_ok_call:
            reasons.append("CALL demasiado extendido")
        if breakout_down and not extension_ok_put:
            reasons.append("PUT demasiado extendido")
        reason = "Sin señal: " + (", ".join(dict.fromkeys(reasons)) or "filtros no cumplidos")

    confirmed = signal is not None
    return {
        "signal": signal,
        "prediction": signal.upper() if signal else "NO SIGNAL",
        "force_candle": impulse_strong,
        "price_action_confirmed": confirmed,
        "reason": reason,
        "analysis": {
            "pair": pair,
            "mode": mode,
            "signal_type": "breakout_retest_rejection_extension_filter",
            "breakout_body_ratio": impulse_ratio,
            "confirmation_body_ratio": confirm_ratio,
            "call_extension_ratio": call_extension,
            "put_extension_ratio": put_extension,
            "breakout_up": breakout_up,
            "breakout_down": breakout_down,
            "breakout_strong": impulse_strong,
            "confirmation_strong": confirm_strong,
            "call_retest": call_retest,
            "put_retest": put_retest,
            "extension_ok_call": extension_ok_call,
            "extension_ok_put": extension_ok_put,
            "reference_high": ref_high,
            "reference_low": ref_low,
            "retest_tolerance": tolerance,
            "max_extension_ratio": MAX_EXTENSION_RATIO,
        },
    }


def analyze(data, pair=None, mode="M1_M1"):
    """Alias de compatibilidad para el bot."""
    return analyze_market(data, pair=pair, mode=mode)
