"""
strategy.py

Estrategia de CONTINUIDAD FILTRADA para Binary OTC M1.

Secuencia operativa:
- Se analiza la vela N cuando ya está cerrada.
- Si N cumple continuidad alcista/bajista, se prepara la entrada.
- El bot ejecuta en N+1 (la cuarta vela cuando el patrón observado
  está compuesto por tres velas previas).

CALL:
  1) La vela actual es verde.
  2) La vela anterior también es verde.
  3) El cuerpo actual es fuerte: >= 120% del promedio de cuerpos.
  4) El cierre actual rompe el máximo de la vela anterior.
  5) La mecha superior no supera el 50% del cuerpo.

PUT:
  1) La vela actual es roja.
  2) La vela anterior también es roja.
  3) El cuerpo actual es fuerte: >= 120% del promedio de cuerpos.
  4) El cierre actual rompe el mínimo de la vela anterior.
  5) La mecha inferior no supera el 50% del cuerpo.

Este módulo solamente analiza; no ejecuta operaciones ni decide la expiración.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import math

import numpy as np
import pandas as pd

MIN_BARS = 12
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
    out.rename(columns={"max": "high", "min": "low", "timestamp": "from"}, inplace=True)
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
    out = out[(out["high"] >= out[["open", "close"]].max(axis=1)) &
              (out["low"] <= out[["open", "close"]].min(axis=1))]
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


def _candle_metrics(row: pd.Series) -> Dict[str, float]:
    open_price = _safe_float(row.get("open"))
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))
    body = abs(close - open_price)
    upper_wick = max(high - max(close, open_price), 0.0)
    lower_wick = max(min(close, open_price) - low, 0.0)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "range": max(high - low, EPS),
    }


def _context_analysis(data: pd.DataFrame, current: pd.Series, pair: Optional[str]) -> Dict[str, Any]:
    tr = _true_range(data)
    atr = _safe_float(tr.tail(ATR_PERIOD).mean(), 0.0)
    prior = data.iloc[:-1].tail(SWING_LOOKBACK)
    last_swing_high = _safe_float(prior["high"].max(), np.nan) if not prior.empty else np.nan
    last_swing_low = _safe_float(prior["low"].min(), np.nan) if not prior.empty else np.nan
    cm = _candle_metrics(current)
    return {
        "pair": pair,
        "atr": atr,
        "atr_stop": np.nan,
        "atr_position": 1 if current["close"] >= current["open"] else -1,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "candle": cm,
        "signal_candle": True,
        "execution_mode": "next_candle_sniper",
        "expiration_minutes": 3,
        "force": True,
        "continuity": {
            "force_period": FORCE_PERIOD,
            "force_percent": FORCE_PERCENT,
            "wick_percent": WICK_PERCENT,
        },
    }


def _empty_result(reason: str, analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    data = _build_dataframe(df=df, candle_1m=candle_1m, previous_m1=previous_m1)
    if len(data) < MIN_BARS:
        return _empty_result(f"Historial insuficiente {len(data)}/{MIN_BARS}")

    current = data.iloc[-1]
    previous = data.iloc[-2]
    current_metrics = _candle_metrics(current)
    previous_metrics = _candle_metrics(previous)
    analysis = _context_analysis(data, current, pair)

    bodies = (data["close"] - data["open"]).abs()
    average_body = _safe_float(bodies.iloc[:-1].tail(FORCE_PERIOD).mean(), 0.0)
    current_body = current_metrics["body"]
    strong_candle = average_body > EPS and current_body >= average_body * FORCE_PERCENT / 100.0

    current_green = current_metrics["close"] > current_metrics["open"]
    current_red = current_metrics["close"] < current_metrics["open"]
    previous_green = previous_metrics["close"] > previous_metrics["open"]
    previous_red = previous_metrics["close"] < previous_metrics["open"]

    small_upper_wick = current_metrics["upper_wick"] <= current_body * WICK_PERCENT / 100.0
    small_lower_wick = current_metrics["lower_wick"] <= current_body * WICK_PERCENT / 100.0

    buy_continuation = (
        current_green
        and previous_green
        and strong_candle
        and current_metrics["close"] > previous_metrics["high"]
        and small_upper_wick
    )
    sell_continuation = (
        current_red
        and previous_red
        and strong_candle
        and current_metrics["close"] < previous_metrics["low"]
        and small_lower_wick
    )

    analysis.update({
        "average_body": average_body,
        "current_body": current_body,
        "strong_candle": strong_candle,
        "current_green": current_green,
        "current_red": current_red,
        "previous_green": previous_green,
        "previous_red": previous_red,
        "small_upper_wick": small_upper_wick,
        "small_lower_wick": small_lower_wick,
        "previous_high": previous_metrics["high"],
        "previous_low": previous_metrics["low"],
        "buy_continuation": buy_continuation,
        "sell_continuation": sell_continuation,
    })

    if buy_continuation:
        return {
            "signal": "call",
            "direction": "bullish",
            "score": 100,
            "entry_quality": 100,
            "entry_type": "force",
            "blocked": False,
            "reason": "CALL | continuidad alcista confirmada | ejecución N+1 modo sniper",
            "signal_price": current_metrics["close"],
            "candle_timestamp": int(current["from"]) if "from" in current and pd.notna(current["from"]) else None,
            "analysis": analysis,
        }

    if sell_continuation:
        return {
            "signal": "put",
            "direction": "bearish",
            "score": 100,
            "entry_quality": 100,
            "entry_type": "force",
            "blocked": False,
            "reason": "PUT | continuidad bajista confirmada | ejecución N+1 modo sniper",
            "signal_price": current_metrics["close"],
            "candle_timestamp": int(current["from"]) if "from" in current and pd.notna(current["from"]) else None,
            "analysis": analysis,
        }

    return _empty_result("Sin continuidad filtrada", analysis)


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente: CONTINUIDAD FILTRADA")
