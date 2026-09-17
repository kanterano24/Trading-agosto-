"""
strategy.py

Estrategia para Binary OTC M1:
Bollinger Bands + RSI + ATR Trailing Stops.

Regla de entrada en la MISMA vela de señal:
CALL:
  1) La vela toca/cruza la banda inferior de Bollinger.
  2) La vela muestra rechazo inferior y cierra por encima de la banda.
  3) El ATR Trailing Stop está en modo alcista.
  4) El RSI sale de sobreventa o confirma recuperación desde la zona baja.

PUT: reglas inversas con la banda superior, rechazo superior,
ATR Trailing Stop bajista y salida de sobrecompra.

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

BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
ATR_PERIOD = 14
ATR_TRAILING_MULTIPLIER = 2.0

MIN_REJECTION_WICK_RATIO = 0.25
MIN_CLOSE_POSITION_CALL = 0.55
MAX_CLOSE_POSITION_PUT = 0.45
MIN_SCORE = 70

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
    aliases = {"max": "high", "min": "low", "timestamp": "from"}
    out.rename(columns=aliases, inplace=True)
    required = ["open", "high", "low", "close"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
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


def _rsi(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    result.loc[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    result.loc[(avg_gain == 0) & (avg_loss > 0)] = 0.0
    return result


def _atr_trailing_stop(data: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Equivalente funcional del script ATR Trailing Stops mostrado."""
    tr = _true_range(data)
    atr = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    distance = atr * ATR_TRAILING_MULTIPLIER

    stops: list[float] = []
    positions: list[int] = []
    previous_stop = 0.0
    previous_position = 0

    for i, row in data.iterrows():
        close = _safe_float(row["close"])
        high = _safe_float(row["high"])
        low = _safe_float(row["low"])
        d = _safe_float(distance.iloc[i], 0.0)

        if d <= 0:
            stops.append(np.nan)
            positions.append(0)
            continue

        upper_base = high - d
        lower_base = low + d

        if previous_stop <= 0:
            stop = upper_base if previous_position >= 0 else lower_base
            position = 1 if close >= stop else -1
        elif close > previous_stop and data["close"].iloc[i - 1] > previous_stop:
            stop = max(previous_stop, upper_base)
            position = 1
        elif close < previous_stop and data["close"].iloc[i - 1] < previous_stop:
            stop = min(previous_stop, lower_base)
            position = -1
        elif close > previous_stop:
            stop = upper_base
            position = 1
        else:
            stop = lower_base
            position = -1

        previous_stop = stop
        previous_position = position
        stops.append(stop)
        positions.append(position)

    return pd.Series(stops, index=data.index), pd.Series(positions, index=data.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty:
        return out

    out["tr"] = _true_range(out)
    out["atr"] = out["tr"].rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    out["bb_mid"] = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
    out["bb_std"] = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + BB_STD * out["bb_std"]
    out["bb_lower"] = out["bb_mid"] - BB_STD * out["bb_std"]
    out["rsi"] = _rsi(out["close"])
    out["atr_stop"], out["atr_position"] = _atr_trailing_stop(out)
    return out


def _candle_metrics(row: pd.Series) -> Dict[str, float]:
    o = _safe_float(row.get("open"))
    h = _safe_float(row.get("high"))
    l = _safe_float(row.get("low"))
    c = _safe_float(row.get("close"))
    rng = max(h - l, EPS)
    body = abs(c - o)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "upper": max(h - max(o, c), 0.0),
        "lower": max(min(o, c) - l, 0.0),
        "body_ratio": body / rng,
        "close_position": (c - l) / rng,
    }


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


def _signal_score(
    side: str,
    candle: Dict[str, float],
    rsi: float,
    previous_rsi: float,
    atr_position: int,
    touched_band: bool,
    rejection: bool,
) -> int:
    score = 0
    score += 25 if touched_band else 0
    score += 25 if rejection else 0
    score += 20 if (side == "call" and atr_position == 1) or (side == "put" and atr_position == -1) else 0
    if side == "call":
        score += 20 if previous_rsi <= RSI_OVERSOLD < rsi else 10 if rsi > previous_rsi and rsi <= 45 else 0
        score += 10 if candle["close_position"] >= MIN_CLOSE_POSITION_CALL else 0
    else:
        score += 20 if previous_rsi >= RSI_OVERBOUGHT > rsi else 10 if rsi < previous_rsi and rsi >= 55 else 0
        score += 10 if candle["close_position"] <= MAX_CLOSE_POSITION_PUT else 0
    return min(100, score)


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

    ind = add_indicators(data)
    if len(ind) < max(MIN_BARS, BB_PERIOD + 2):
        return _empty_result("Indicadores insuficientes")

    current = ind.iloc[-1]
    previous = ind.iloc[-2]
    cm = _candle_metrics(current)
    price = cm["close"]
    lower = _safe_float(current.get("bb_lower"), np.nan)
    upper = _safe_float(current.get("bb_upper"), np.nan)
    rsi = _safe_float(current.get("rsi"), np.nan)
    previous_rsi = _safe_float(previous.get("rsi"), np.nan)
    atr = _safe_float(current.get("atr"), 0.0)
    atr_stop = _safe_float(current.get("atr_stop"), np.nan)
    atr_position = int(_safe_float(current.get("atr_position"), 0))

    base_analysis = {
        "pair": pair,
        "atr": atr,
        "atr_stop": atr_stop,
        "atr_position": atr_position,
        "rsi": rsi,
        "previous_rsi": previous_rsi,
        "bb_lower": lower,
        "bb_mid": _safe_float(current.get("bb_mid"), np.nan),
        "bb_upper": upper,
        "candle": cm,
        "signal_candle": True,
        "execution_mode": "same_candle",
        "expiration_minutes": 3,
    }

    if not all(math.isfinite(v) for v in [lower, upper, rsi, previous_rsi, atr]) or atr <= 0:
        result = _empty_result("Valores de indicadores inválidos")
        result["analysis"] = base_analysis
        return result

    call_touch = cm["low"] <= lower
    call_rejection = (
        call_touch
        and price > lower
        and cm["close_position"] >= MIN_CLOSE_POSITION_CALL
        and cm["lower"] / cm["range"] >= MIN_REJECTION_WICK_RATIO
        and price >= cm["open"]
    )
    put_touch = cm["high"] >= upper
    put_rejection = (
        put_touch
        and price < upper
        and cm["close_position"] <= MAX_CLOSE_POSITION_PUT
        and cm["upper"] / cm["range"] >= MIN_REJECTION_WICK_RATIO
        and price <= cm["open"]
    )

    call_score = _signal_score("call", cm, rsi, previous_rsi, atr_position, call_touch, call_rejection)
    put_score = _signal_score("put", cm, rsi, previous_rsi, atr_position, put_touch, put_rejection)

    base_analysis.update({
        "call_touch": call_touch,
        "call_rejection": call_rejection,
        "put_touch": put_touch,
        "put_rejection": put_rejection,
        "call_score": call_score,
        "put_score": put_score,
    })

    candidates = []
    if call_rejection and atr_position == 1 and rsi > previous_rsi and rsi < 55:
        candidates.append((call_score, "call", "CALL | rechazo banda inferior + ATR alcista + RSI recuperando"))
    if put_rejection and atr_position == -1 and rsi < previous_rsi and rsi > 45:
        candidates.append((put_score, "put", "PUT | rechazo banda superior + ATR bajista + RSI debilitándose"))

    if not candidates:
        result = _empty_result("Sin confirmación BB + ATR Trailing Stop + RSI")
        result["analysis"] = base_analysis
        return result

    score, signal, reason = max(candidates, key=lambda item: item[0])
    if score < MIN_SCORE:
        result = _empty_result(f"Señal descartada por score {score}/{MIN_SCORE}")
        result["analysis"] = base_analysis
        return result

    result = {
        "signal": signal,
        "direction": "bullish" if signal == "call" else "bearish",
        "score": int(score),
        "entry_quality": int(score),
        "entry_type": "bb_atr_rsi",
        "blocked": False,
        "reason": reason + " | ejecución durante la vela de señal | expiración 3 minutos",
        "signal_price": price,
        "candle_timestamp": int(current["from"]) if "from" in current and pd.notna(current["from"]) else None,
        "analysis": base_analysis,
    }
    return result


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente: BB + ATR Trailing Stops + RSI")
