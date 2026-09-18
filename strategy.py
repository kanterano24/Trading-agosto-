"""Estrategia de rechazo estructural para Binary OTC M1.

Interfaz compatible con bot vv2:
    analyze_market(candle_1m=..., previous_m1=..., pair=...)

Reglas principales:
- Solo se analizan velas cerradas.
- La señal se prepara en N y se ejecuta en N+1.
- Solo se permiten estructuras explícitas de rechazo:
    * bearish_to_bullish_rejection: rechazo de soporte para CALL.
    * bullish_to_bearish_rejection: rechazo de resistencia para PUT.
- Cualquier estructura no identificada (unknown) queda bloqueada.
- El score mínimo es 90.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
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


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Filtro estructural
# ---------------------------------------------------------------------------
def _swing_structure(data: pd.DataFrame, support: float, resistance: float) -> Dict[str, Any]:
    """Devuelve una estructura explícita o unknown.

    Se buscan pivotes simples de 3 velas en el tramo reciente. La estructura
    solo se acepta cuando existe un extremo reciente que haya sido rechazado:
      - LL/HL cerca del soporte para una reacción alcista.
      - HH/LH cerca de la resistencia para una reacción bajista.
    """
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

    if last_low and previous_low:
        if abs(last_low[1] - support) <= max(abs(resistance - support) * 0.20, EPS):
            if last_low[1] < previous_low[1]:
                result.update(name="LL_support_rejection", confirmed=True)
            elif last_low[1] > previous_low[1]:
                result.update(name="HL_support_rejection", confirmed=True)

    if last_high and previous_high:
        if abs(last_high[1] - resistance) <= max(abs(resistance - support) * 0.20, EPS):
            if last_high[1] > previous_high[1]:
                result.update(name="HH_resistance_rejection", confirmed=True)
            elif last_high[1] < previous_high[1]:
                result.update(name="LH_resistance_rejection", confirmed=True)

    return result


def _rejection_structure(
    candle: Dict[str, float],
    support: float,
    resistance: float,
    tolerance: float,
    bullish_context: bool,
    bearish_context: bool,
    swing: Dict[str, Any],
) -> Tuple[str, bool, list[str]]:
    """Autoriza exclusivamente estructuras de rechazo completas."""
    near_support = candle["low"] <= support + tolerance and candle["close"] > support
    near_resistance = candle["high"] >= resistance - tolerance and candle["close"] < resistance

    bullish_candle = (
        near_support
        and candle["lower"] >= max(candle["body"] * 1.40, EPS)
        and candle["close"] > candle["open"]
        and candle["close_position"] >= 0.62
        and candle["body_ratio"] <= 0.65
    )
    bearish_candle = (
        near_resistance
        and candle["upper"] >= max(candle["body"] * 1.40, EPS)
        and candle["close"] < candle["open"]
        and candle["close_position"] <= 0.38
        and candle["body_ratio"] <= 0.65
    )

    if bullish_candle and (bullish_context or swing["name"] in {"LL_support_rejection", "HL_support_rejection"}):
        reasons = ["bearish_to_bullish_rejection", "support_reclaimed"]
        if swing["name"] != "unknown":
            reasons.append(swing["name"])
        return "bearish_to_bullish_rejection", True, reasons

    if bearish_candle and (bearish_context or swing["name"] in {"HH_resistance_rejection", "LH_resistance_rejection"}):
        reasons = ["bullish_to_bearish_rejection", "resistance_reclaimed_down"]
        if swing["name"] != "unknown":
            reasons.append(swing["name"])
        return "bullish_to_bearish_rejection", True, reasons

    return "unknown", False, []


# ---------------------------------------------------------------------------
# Análisis principal
# ---------------------------------------------------------------------------
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

    candle = _metrics(data.iloc[-1])
    previous = data.iloc[:-1]
    support = _safe_float(previous["low"].tail(LOOKBACK).min())
    resistance = _safe_float(previous["high"].tail(LOOKBACK).max())
    tolerance = max(atr * ZONE_ATR_FACTOR, candle["range"] * 0.10)

    fast = _ema(previous["close"].tail(30), EMA_FAST)
    slow = _ema(previous["close"].tail(30), EMA_SLOW)
    bullish_context = fast > slow
    bearish_context = fast < slow

    swing = _swing_structure(previous, support, resistance)
    structure, confirmed, structure_reasons = _rejection_structure(
        candle=candle,
        support=support,
        resistance=resistance,
        tolerance=tolerance,
        bullish_context=bullish_context,
        bearish_context=bearish_context,
        swing=swing,
    )

    analysis = {
        "pair": pair,
        "pattern": "structural_rejection_only",
        "structure": structure,
        "structure_confirmed": confirmed,
        "swing_structure": swing,
        "execution_mode": "next_candle",
        "expiration_minutes": 1,
        "atr": atr,
        "last_swing_high": resistance,
        "last_swing_low": support,
        "support": support,
        "resistance": resistance,
        "tolerance": tolerance,
        "fast_ema": fast,
        "slow_ema": slow,
        "bullish_context": bullish_context,
        "bearish_context": bearish_context,
        "candle": candle,
        "force": True,
    }

    # Bloqueo duro: jamás enviar una señal con estructura unknown.
    if not confirmed or structure == "unknown":
        return _empty("unknown_or_unconfirmed_rejection_structure", analysis)

    score = 76
    reasons = list(structure_reasons)

    if candle["lower"] >= candle["body"] * 2.0 or candle["upper"] >= candle["body"] * 2.0:
        score += 7
        reasons.append("long_rejection_wick")

    if bullish_context or bearish_context:
        score += 5
        reasons.append("ema_context")

    if structure == "bearish_to_bullish_rejection" and candle["close"] > support + tolerance * 0.25:
        score += 5
        reasons.append("close_above_support")

    if structure == "bullish_to_bearish_rejection" and candle["close"] < resistance - tolerance * 0.25:
        score += 5
        reasons.append("close_below_resistance")

    if candle["body_ratio"] <= 0.45:
        score += 7
        reasons.append("controlled_body")

    if score < MIN_SCORE:
        return _empty("valid_structure_but_score_below_90", analysis)

    if structure == "bearish_to_bullish_rejection":
        return {
            "signal": "call",
            "direction": "bullish",
            "score": min(score, 100),
            "entry_quality": min(score, 100),
            "entry_type": "force",
            "blocked": False,
            "reason": ",".join(reasons) + " | N cerrada / ejecución N+1",
            "signal_price": candle["close"],
            "candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
            "analysis": analysis,
        }

    if structure == "bullish_to_bearish_rejection":
        return {
            "signal": "put",
            "direction": "bearish",
            "score": min(score, 100),
            "entry_quality": min(score, 100),
            "entry_type": "force",
            "blocked": False,
            "reason": ",".join(reasons) + " | N cerrada / ejecución N+1",
            "signal_price": candle["close"],
            "candle_timestamp": int(data.iloc[-1]["from"]) if "from" in data.columns else None,
            "analysis": analysis,
        }

    return _empty("structure_not_allowed", analysis)


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado: solo estructuras explícitas de rechazo, score mínimo 90, ejecución N+1")
