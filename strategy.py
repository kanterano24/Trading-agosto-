"""strategy.py

Estrategia de rechazo + confirmacion para Binary OTC M1.

Flujo obligatorio:
    N   = vela de rechazo cerrada.
    N+1 = vela de confirmacion cerrada.
    N+2 = primera vela en la que el bot puede ejecutar con esta logica.

IMPORTANTE:
- No se genera una senal solamente porque exista una vela de rechazo.
- CALL: rechazo en soporte y N+1 confirma con vela alcista.
- PUT: rechazo en resistencia y N+1 confirma con vela bajista.
- Para una confirmacion fuerte, N+1 debe cerrar por encima del maximo de N
  (CALL) o por debajo del minimo de N (PUT).
- Se bloquea cualquier estructura desconocida.
- Esta estrategia no garantiza ganancias; probar primero en DEMO.

Interfaz compatible con vv2:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import pandas as pd

MIN_BARS = 35
MAX_CANDLES = 120
ATR_PERIOD = 14
EMA_FAST = 8
EMA_SLOW = 21
LOOKBACK = 60
SWING_LOOKBACK = 18
ZONE_ATR_FACTOR = 0.30
MIN_SCORE = 90
EPS = 1e-10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
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
    return out.reset_index(drop=True).tail(MAX_CANDLES).reset_index(drop=True)


def _build_dataframe(
    candle_1m: Any,
    previous_m1: Optional[pd.DataFrame],
    df: Optional[pd.DataFrame],
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


def _metrics(row: pd.Series) -> Dict[str, float]:
    open_price = _safe_float(row.get("open"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))
    candle_range = max(high - low, EPS)
    body = abs(close - open_price)

    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "range": candle_range,
        "body": body,
        "upper": max(high - max(open_price, close), 0.0),
        "lower": max(min(open_price, close) - low, 0.0),
        "close_position": (close - low) / candle_range,
        "body_ratio": body / candle_range,
    }


def _ema(values: pd.Series, period: int) -> float:
    if values.empty:
        return 0.0
    return _safe_float(values.ewm(span=period, adjust=False).mean().iloc[-1])


def _empty(reason: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "score": 0,
        "entry_quality": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason,
        "analysis": analysis or {},
    }


def _swing_structure(data: pd.DataFrame, support: float, resistance: float) -> Dict[str, Any]:
    if len(data) < 7:
        return {"name": "unknown", "confirmed": False, "reason": "few_bars"}

    recent = data.tail(SWING_LOOKBACK).reset_index(drop=True)
    highs = recent["high"].to_numpy(dtype=float)
    lows = recent["low"].to_numpy(dtype=float)
    pivot_highs = []
    pivot_lows = []

    for i in range(1, len(recent) - 1):
        if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            pivot_highs.append((i, highs[i]))
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            pivot_lows.append((i, lows[i]))

    last_low = pivot_lows[-1] if pivot_lows else None
    previous_low = pivot_lows[-2] if len(pivot_lows) >= 2 else None
    last_high = pivot_highs[-1] if pivot_highs else None
    previous_high = pivot_highs[-2] if len(pivot_highs) >= 2 else None

    result = {
        "name": "unknown",
        "confirmed": False,
        "last_pivot_low": last_low[1] if last_low else None,
        "previous_pivot_low": previous_low[1] if previous_low else None,
        "last_pivot_high": last_high[1] if last_high else None,
        "previous_pivot_high": previous_high[1] if previous_high else None,
    }

    zone_width = max(abs(resistance - support) * 0.20, EPS)
    if last_low and previous_low and abs(last_low[1] - support) <= zone_width:
        if last_low[1] < previous_low[1]:
            result.update(name="LL_support_rejection", confirmed=True)
        elif last_low[1] > previous_low[1]:
            result.update(name="HL_support_rejection", confirmed=True)

    if last_high and previous_high and abs(last_high[1] - resistance) <= zone_width:
        if last_high[1] > previous_high[1]:
            result.update(name="HH_resistance_rejection", confirmed=True)
        elif last_high[1] < previous_high[1]:
            result.update(name="LH_resistance_rejection", confirmed=True)

    return result


def _rejection_side(
    candle: Dict[str, float],
    support: float,
    resistance: float,
    tolerance: float,
) -> Tuple[str, bool, list[str]]:
    near_support = candle["low"] <= support + tolerance and candle["close"] > support
    near_resistance = candle["high"] >= resistance - tolerance and candle["close"] < resistance

    bullish_rejection = (
        near_support
        and candle["lower"] >= max(candle["body"] * 1.40, EPS)
        and candle["close_position"] >= 0.60
        and candle["body_ratio"] <= 0.70
    )
    bearish_rejection = (
        near_resistance
        and candle["upper"] >= max(candle["body"] * 1.40, EPS)
        and candle["close_position"] <= 0.40
        and candle["body_ratio"] <= 0.70
    )

    if bullish_rejection:
        return "call", True, ["bullish_support_rejection", "close_above_support"]
    if bearish_rejection:
        return "put", True, ["bearish_resistance_rejection", "close_below_resistance"]
    return "unknown", False, []


def _confirm_next_candle(
    direction: str,
    rejection: Dict[str, float],
    confirmation: Dict[str, float],
) -> Tuple[bool, list[str]]:
    """Confirma la direccion usando la vela posterior ya cerrada."""
    reasons: list[str] = []

    if direction == "call":
        bullish = confirmation["close"] > confirmation["open"]
        closes_above_rejection = confirmation["close"] > rejection["high"]
        holds_rejection = confirmation["close"] > rejection["close"]
        if bullish and holds_rejection:
            reasons.append("next_candle_bullish_confirmation")
            if closes_above_rejection:
                reasons.append("close_above_rejection_high")
            return True, reasons

    if direction == "put":
        bearish = confirmation["close"] < confirmation["open"]
        closes_below_rejection = confirmation["close"] < rejection["low"]
        holds_rejection = confirmation["close"] < rejection["close"]
        if bearish and holds_rejection:
            reasons.append("next_candle_bearish_confirmation")
            if closes_below_rejection:
                reasons.append("close_below_rejection_low")
            return True, reasons

    return False, []


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    data = _build_dataframe(candle_1m, previous_m1, df)
    if len(data) < MIN_BARS:
        return _empty(f"Historial insuficiente {len(data)}/{MIN_BARS}")

    data = data.copy()
    data["tr"] = _true_range(data)
    data["atr"] = data["tr"].rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    atr = _safe_float(data.iloc[-1]["atr"], 0.0)
    if atr <= 0.0:
        return _empty("ATR insuficiente")

    # La ultima vela es N+1, y la anterior es N (rechazo).
    rejection = _metrics(data.iloc[-2])
    confirmation = _metrics(data.iloc[-1])
    context_data = data.iloc[:-2]
    if len(context_data) < 10:
        return _empty("Contexto insuficiente para soporte/resistencia")

    support = _safe_float(context_data["low"].tail(LOOKBACK).min())
    resistance = _safe_float(context_data["high"].tail(LOOKBACK).max())
    tolerance = max(atr * ZONE_ATR_FACTOR, rejection["range"] * 0.10)

    fast = _ema(context_data["close"].tail(30), EMA_FAST)
    slow = _ema(context_data["close"].tail(30), EMA_SLOW)
    bullish_context = fast > slow
    bearish_context = fast < slow

    swing = _swing_structure(context_data, support, resistance)
    direction, rejected, rejection_reasons = _rejection_side(
        rejection, support, resistance, tolerance
    )

    analysis = {
        "pair": pair,
        "pattern": "rejection_with_next_candle_confirmation",
        "structure": swing["name"],
        "structure_confirmed": swing.get("confirmed", False),
        # Niveles estructurales que utiliza bot.py para revalidar la entrada.
        "last_swing_high": swing.get("last_pivot_high"),
        "last_swing_low": swing.get("last_pivot_low"),
        "rejection_detected": rejected,
        "confirmation_checked": True,
        "execution_mode": "after_N_plus_1_close",
        "execution_candle": "N+2",
        "expiration_minutes": 1,
        "atr": atr,
        "support": support,
        "resistance": resistance,
        "tolerance": tolerance,
        "fast_ema": fast,
        "slow_ema": slow,
        "bullish_context": bullish_context,
        "bearish_context": bearish_context,
        "rejection_candle": rejection,
        "confirmation_candle": confirmation,
        "rejection_timestamp": int(data.iloc[-2]["from"]) if "from" in data.columns else None,
        "confirmation_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
        "force": True,
    }

    allowed_structures = {
        "call": {"LL_support_rejection", "HL_support_rejection"},
        "put": {"HH_resistance_rejection", "LH_resistance_rejection"},
    }

    if not rejected or direction == "unknown":
        return _empty("no_valid_rejection_on_N", analysis)

    if swing["name"] not in allowed_structures[direction]:
        return _empty("structure_not_allowed_or_unknown", analysis)

    confirmed, confirmation_reasons = _confirm_next_candle(
        direction, rejection, confirmation
    )
    if not confirmed:
        return _empty("N_plus_1_did_not_confirm_direction", analysis)

    score = 90
    reasons = rejection_reasons + [swing["name"]] + confirmation_reasons

    if direction == "call" and confirmation["close"] > rejection["high"]:
        score += 5
        reasons.append("strong_break_of_rejection_high")
    if direction == "put" and confirmation["close"] < rejection["low"]:
        score += 5
        reasons.append("strong_break_of_rejection_low")

    if confirmation["body_ratio"] >= 0.45:
        score += 3
        reasons.append("confirmation_body_present")

    if (direction == "call" and bullish_context) or (direction == "put" and bearish_context):
        score += 2
        reasons.append("ema_context_aligned")

    score = min(score, 100)
    if score < MIN_SCORE:
        return _empty("confirmed_but_score_below_90", analysis)

    signal = "call" if direction == "call" else "put"
    return {
        "signal": signal,
        "direction": "bullish" if signal == "call" else "bearish",
        "score": score,
        "entry_quality": score,
        "entry_type": "force",
        "blocked": False,
        "reason": ",".join(reasons) + " | N rechazo + N+1 confirmada / ejecutar N+2",
        "signal_price": confirmation["close"],
        "candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
        "analysis": analysis,
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado: rechazo en N + confirmacion cerrada en N+1; ejecucion N+2")
