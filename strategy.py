"""strategy.py - MOMENTUM M1 100% PRICE ACTION.

N = vela M1 cerrada que se analiza.
N+1 = vela M1 donde el bot puede ejecutar.

La decisión usa únicamente OHLC y estructura reciente del precio.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import statistics
import pandas as pd

MIN_BARS = 60
BREAKOUT_LOOKBACK = 6
PRE_BREAKOUT_CANDLES = 3
RANGE_REFERENCE_CANDLES = 14

MIN_BODY_RATIO = 0.60
MIN_BODY_VS_PREV_RANGE = 1.15
MIN_RANGE_VS_PREV_RANGE = 1.10
MIN_CLOSE_POSITION = 0.78

MAX_BREAKOUT_EXTENSION_RANGE = 0.80
MAX_PRE_BREAKOUT_DISTANCE_RANGE = 0.35
MAX_COMPRESSION_RANGE_RATIO = 1.80
MAX_COMPRESSION_AVG_RATIO = 0.75
MAX_CLOSE_DISTANCE_FROM_BREAKOUT_RANGE = 0.80
MAX_CONSECUTIVE = 3
MIN_MOMENTUM_SCORE = 82
EPS = 1e-12


def _empty_result(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None, "direction": "range", "trend": "range",
        "reason": reason, "score": 0, "continuity": False,
        "blocked": True, "zone": "momentum",
        "entry_type": "price_action_breakout", "entry_quality": 0,
        "candle_timestamp": None, "analysis": {},
    }


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "max" in out.columns and "high" not in out.columns:
        out = out.rename(columns={"max": "high"})
    if "min" in out.columns and "low" not in out.columns:
        out = out.rename(columns={"min": "low"})
    required = ["open", "high", "low", "close"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()
    for c in required:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
    sort_col = "from" if "from" in out.columns else "close"
    return out.dropna(subset=required).sort_values(sort_col).reset_index(drop=True)


def _metrics(row: pd.Series) -> Dict[str, float]:
    op, hi, lo, cl = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(hi - lo, EPS)
    body = abs(cl - op)
    upper = max(0.0, hi - max(op, cl))
    lower = max(0.0, min(op, cl) - lo)
    return {
        "open": op, "high": hi, "low": lo, "close": cl,
        "range": rng, "body": body, "body_ratio": body / rng,
        "close_position": (cl - lo) / rng,
        "upper_wick_ratio": upper / rng, "lower_wick_ratio": lower / rng,
    }


def _candle_direction(row: pd.Series) -> str:
    if float(row["close"]) > float(row["open"]):
        return "bullish"
    if float(row["close"]) < float(row["open"]):
        return "bearish"
    return "neutral"


def _consecutive(data: pd.DataFrame, direction: str) -> int:
    count = 0
    for _, row in data.tail(MAX_CONSECUTIVE + 3).iloc[::-1].iterrows():
        if _candle_direction(row) != direction:
            break
        count += 1
    return count


def _range_median(data: pd.DataFrame) -> float:
    ranges = (data["high"].astype(float) - data["low"].astype(float)).tail(RANGE_REFERENCE_CANDLES)
    values = [float(v) for v in ranges.tolist() if pd.notna(v) and float(v) > 0]
    return float(statistics.median(values)) if values else 0.0


def _build_data(df: Optional[pd.DataFrame], candle_1m: Any,
                previous_m1: Optional[pd.DataFrame], candle_5m: Any,
                previous_m5: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is not None:
        return _normalize(df.copy())
    if previous_m1 is not None:
        base = previous_m1.copy()
        if candle_1m is not None:
            base = pd.concat([base, pd.DataFrame([candle_1m])], ignore_index=True)
        return _normalize(base)
    if previous_m5 is not None:
        base = previous_m5.copy()
        if candle_5m is not None:
            base = pd.concat([base, pd.DataFrame([candle_5m])], ignore_index=True)
        return _normalize(base)
    return pd.DataFrame()


def analyze_market(df: Optional[pd.DataFrame] = None, candle_1m: Any = None,
                   previous_m1: Optional[pd.DataFrame] = None, candle_5m: Any = None,
                   previous_m5: Optional[pd.DataFrame] = None, m1_block: Any = None,
                   pair: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    data = _build_data(df, candle_1m, previous_m1, candle_5m, previous_m5)
    result = _empty_result()
    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"
        return result

    current = data.iloc[-1]
    hist = data.iloc[:-1].copy()
    c = _metrics(current)
    direction = "bullish" if c["close"] > c["open"] else "bearish" if c["close"] < c["open"] else "range"

    reference = hist.tail(BREAKOUT_LOOKBACK)
    if len(reference) < BREAKOUT_LOOKBACK:
        result["reason"] = "Historial de ruptura insuficiente"
        return result

    breakout_high = float(reference["high"].max())
    breakout_low = float(reference["low"].min())
    previous_close = float(hist.iloc[-1]["close"])
    reference_range = _range_median(hist)
    if reference_range <= EPS:
        result["reason"] = "Rango de referencia invalido"
        return result

    body_vs_prev_range = c["body"] / reference_range
    range_vs_prev_range = c["range"] / reference_range
    close_strength = c["close_position"] if direction == "bullish" else 1.0 - c["close_position"] if direction == "bearish" else 0.0

    pre = hist.tail(PRE_BREAKOUT_CANDLES)
    pre_ranges = pre["high"].astype(float) - pre["low"].astype(float)
    pre_range = float(pre["high"].max() - pre["low"].min())
    pre_avg_range = float(pre_ranges.mean())
    compression_range_ratio = pre_range / reference_range
    compression_avg_ratio = pre_avg_range / reference_range
    compression_ok = compression_range_ratio <= MAX_COMPRESSION_RANGE_RATIO and compression_avg_ratio <= MAX_COMPRESSION_AVG_RATIO

    if direction == "bullish":
        pre_breakout_distance = max(0.0, breakout_high - previous_close) / max(c["range"], EPS)
        breakout = c["close"] > breakout_high
        breakout_extension = max(0.0, c["close"] - breakout_high) / max(c["range"], EPS)
        close_distance_from_breakout = max(0.0, c["close"] - breakout_high) / max(c["range"], EPS)
        failed_reclaim = c["high"] > breakout_high and c["close"] <= breakout_high
    elif direction == "bearish":
        pre_breakout_distance = max(0.0, previous_close - breakout_low) / max(c["range"], EPS)
        breakout = c["close"] < breakout_low
        breakout_extension = max(0.0, breakout_low - c["close"]) / max(c["range"], EPS)
        close_distance_from_breakout = max(0.0, breakout_low - c["close"]) / max(c["range"], EPS)
        failed_reclaim = c["low"] < breakout_low and c["close"] >= breakout_low
    else:
        pre_breakout_distance = 999.0
        breakout = False
        breakout_extension = 999.0
        close_distance_from_breakout = 999.0
        failed_reclaim = True

    pre_breakout_test = pre_breakout_distance <= MAX_PRE_BREAKOUT_DISTANCE_RANGE
    prev_direction = _candle_direction(hist.iloc[-1])
    if direction == "bullish":
        continuation = prev_direction in ("bullish", "neutral") and c["close"] > previous_close
    elif direction == "bearish":
        continuation = prev_direction in ("bearish", "neutral") and c["close"] < previous_close
    else:
        continuation = False

    consecutive = _consecutive(data, direction) if direction in ("bullish", "bearish") else 0
    no_failed_reclaim = not failed_reclaim

    checks = [
        breakout,
        c["body_ratio"] >= MIN_BODY_RATIO,
        body_vs_prev_range >= MIN_BODY_VS_PREV_RANGE,
        range_vs_prev_range >= MIN_RANGE_VS_PREV_RANGE,
        close_strength >= MIN_CLOSE_POSITION,
        compression_ok,
        pre_breakout_test,
        breakout_extension <= MAX_BREAKOUT_EXTENSION_RANGE,
        close_distance_from_breakout <= MAX_CLOSE_DISTANCE_FROM_BREAKOUT_RANGE,
        continuation,
        consecutive <= MAX_CONSECUTIVE,
        no_failed_reclaim,
    ]
    names = [
        "ruptura", "cuerpo", "cuerpo_vs_rango_previo", "rango_vs_rango_previo",
        "cierre_fuerte", "compresion", "precio_previo_cerca", "extension_controlada",
        "ubicacion_de_cierre", "continuidad", "sin_agotamiento", "sin_falsa_ruptura",
    ]
    weights = [18, 10, 9, 8, 9, 10, 8, 7, 6, 6, 5, 4]
    score = int(sum(w for ok, w in zip(checks, weights) if ok))

    reasons = []
    labels = [
        (breakout, "ruptura M1 confirmada"),
        (c["body_ratio"] >= MIN_BODY_RATIO, "cuerpo dominante"),
        (body_vs_prev_range >= MIN_BODY_VS_PREV_RANGE, "cuerpo mayor al rango previo"),
        (range_vs_prev_range >= MIN_RANGE_VS_PREV_RANGE, "desplazamiento mayor al rango habitual"),
        (close_strength >= MIN_CLOSE_POSITION, "cierre fuerte en el extremo"),
        (compression_ok, "compresion previa"),
        (pre_breakout_test, "precio previo preparado para ruptura"),
        (breakout_extension <= MAX_BREAKOUT_EXTENSION_RANGE, "ruptura no excesivamente extendida"),
        (close_distance_from_breakout <= MAX_CLOSE_DISTANCE_FROM_BREAKOUT_RANGE, "cierre en zona util de entrada"),
        (continuation, "continuidad del movimiento"),
        (consecutive <= MAX_CONSECUTIVE, "sin agotamiento"),
        (no_failed_reclaim, "sin falsa ruptura"),
    ]
    for ok, label in labels:
        if ok:
            reasons.append(label)

    ts = int(current["from"]) if "from" in data.columns and pd.notna(current["from"]) else None
    result.update({
        "direction": direction,
        "trend": direction if breakout else "range",
        "candle_timestamp": ts,
        "analysis": {
            "timeframe": "M1",
            "momentum": True,
            "price_action_only": True,
            "breakout_high": breakout_high,
            "breakout_low": breakout_low,
            "breakout_extension_range": breakout_extension,
            "body_ratio": c["body_ratio"],
            "body_vs_prev_range": body_vs_prev_range,
            "range_vs_prev_range": range_vs_prev_range,
            "close_strength": close_strength,
            "reference_range": reference_range,
            "compression_range_ratio": compression_range_ratio,
            "compression_avg_range_ratio": compression_avg_ratio,
            "pre_breakout_distance_range": pre_breakout_distance,
            "close_distance_from_breakout_range": close_distance_from_breakout,
            "candle_range": c["range"],
            "consecutive": consecutive,
            "continuation": continuation,
            "failed_reclaim": failed_reclaim,
            "confirmation_count": int(sum(checks)),
            "confirmation_total": len(checks),
            "reasons": reasons,
        },
    })

    if direction not in ("bullish", "bearish"):
        result["reason"] = "Vela sin direccion"
        return result
    if not all(checks):
        missing = [name for name, ok in zip(names, checks) if not ok]
        result["reason"] = "Momentum M1 bloqueado | faltan: " + ", ".join(missing)
        return result
    if score < MIN_MOMENTUM_SCORE:
        result["reason"] = f"Momentum M1 bloqueado | calidad {score}/100"
        return result

    signal = "call" if direction == "bullish" else "put"
    result.update({
        "signal": signal,
        "score": score,
        "continuity": True,
        "blocked": False,
        "entry_quality": score,
        "reason": f"{signal.upper()} | MOMENTUM M1 PRICE ACTION CONFIRMADO | " + "; ".join(reasons),
    })
    return result


def get_signal(df: pd.DataFrame):
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame):
    return get_signal(df)
