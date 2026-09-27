"""strategy.py - MOMENTUM M1 con confirmacion de ubicacion de entrada.

N = vela M1 cerrada que se analiza.
N+1 = vela M1 donde el bot puede ejecutar.

La estrategia NO opera por una vela grande solamente. Exige:
1) ruptura de un maximo/minimo reciente,
2) tendencia EMA 9/21/50,
3) desplazamiento con cuerpo y rango suficientes,
4) cierre fuerte,
5) compresion previa,
6) precio previo cerca de la ruptura,
7) distancia controlada desde EMA21,
8) ruptura no demasiado extendida,
9) sin agotamiento por demasiadas velas consecutivas.

No usa soporte/resistencia clasicos.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
import pandas as pd

MIN_BARS = 60
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 6
PRE_BREAKOUT_CANDLES = 3

MIN_BODY_RATIO = 0.60
MIN_BODY_ATR = 0.45
MIN_RANGE_ATR = 0.80
MIN_CLOSE_POSITION = 0.78

MAX_BREAKOUT_EXTENSION_ATR = 0.80
MAX_EMA_DISTANCE_ATR = 1.25
MAX_COMPRESSION_RANGE_ATR = 1.80
MAX_COMPRESSION_AVG_RANGE_ATR = 0.75
MAX_PRE_BREAKOUT_DISTANCE_ATR = 0.35
MAX_CONSECUTIVE = 3

MIN_MOMENTUM_SCORE = 82
EPS = 1e-12


def _empty_result(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": "momentum",
        "entry_type": "momentum_breakout",
        "entry_quality": 0,
        "rsi": 50.0,
        "atr": 0.0,
        "candle_timestamp": None,
        "analysis": {},
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
    out = out.dropna(subset=required).sort_values(sort_col).reset_index(drop=True)
    return out


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    value = tr.rolling(period).mean().iloc[-1]
    return float(value) if pd.notna(value) and value > 0 else 0.0


def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    g, l = gain.iloc[-1], loss.iloc[-1]
    if pd.isna(g) or pd.isna(l):
        return 50.0
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return float(100.0 - 100.0 / (1.0 + g / l))


def _metrics(row: pd.Series) -> Dict[str, float]:
    op, hi, lo, cl = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(hi - lo, EPS)
    body = abs(cl - op)
    return {
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "range": rng,
        "body": body,
        "body_ratio": body / rng,
        "close_position": (cl - lo) / rng,
    }


def _consecutive(data: pd.DataFrame, direction: str) -> int:
    target = "bull" if direction == "bullish" else "bear"
    count = 0
    for _, row in data.tail(MAX_CONSECUTIVE + 2).iloc[::-1].iterrows():
        d = "bull" if row["close"] > row["open"] else "bear" if row["close"] < row["open"] else "neutral"
        if d != target:
            break
        count += 1
    return count


def _build_data(
    df: Optional[pd.DataFrame],
    candle_1m: Any,
    previous_m1: Optional[pd.DataFrame],
    candle_5m: Any,
    previous_m5: Optional[pd.DataFrame],
) -> pd.DataFrame:
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


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    candle_5m: Any = None,
    previous_m5: Optional[pd.DataFrame] = None,
    m1_block: Any = None,
    pair: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    data = _build_data(df, candle_1m, previous_m1, candle_5m, previous_m5)
    result = _empty_result()

    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"
        return result

    current = data.iloc[-1]
    hist = data.iloc[:-1].copy()
    atr = _atr(data)
    if atr <= 0:
        result["reason"] = "ATR M1 inválido"
        return result

    c = _metrics(current)
    direction = "bullish" if c["close"] > c["open"] else "bearish" if c["close"] < c["open"] else "range"
    rsi = _rsi(data["close"])

    ema9s = _ema(data["close"], EMA_FAST)
    ema21s = _ema(data["close"], EMA_MID)
    ema50s = _ema(data["close"], EMA_SLOW)
    ema9 = float(ema9s.iloc[-1])
    ema21 = float(ema21s.iloc[-1])
    ema50 = float(ema50s.iloc[-1])
    ema9_prev = float(ema9s.iloc[-4])
    ema21_prev = float(ema21s.iloc[-4])
    ema50_prev = float(ema50s.iloc[-4])

    reference = hist.tail(BREAKOUT_LOOKBACK)
    breakout_high = float(reference["high"].max())
    breakout_low = float(reference["low"].min())

    pre = hist.tail(PRE_BREAKOUT_CANDLES)
    pre_range = float(pre["high"].max() - pre["low"].min())
    pre_avg_range = float((pre["high"] - pre["low"]).mean())
    compression_range_atr = pre_range / atr
    compression_avg_atr = pre_avg_range / atr
    compression_ok = (
        compression_range_atr <= MAX_COMPRESSION_RANGE_ATR
        and compression_avg_atr <= MAX_COMPRESSION_AVG_RANGE_ATR
    )

    previous_close = float(hist.iloc[-1]["close"])
    if direction == "bullish":
        pre_breakout_distance = max(0.0, breakout_high - previous_close) / atr
    else:
        pre_breakout_distance = max(0.0, previous_close - breakout_low) / atr
    pre_breakout_test = pre_breakout_distance <= MAX_PRE_BREAKOUT_DISTANCE_ATR

    body_atr = c["body"] / atr
    range_atr = c["range"] / atr
    close_strength = c["close_position"] if direction == "bullish" else 1.0 - c["close_position"]

    if direction == "bullish":
        breakout = c["close"] > breakout_high
        trend = (
            ema9 > ema21 > ema50
            and ema9 > ema9_prev
            and ema21 > ema21_prev
            and ema50 >= ema50_prev
            and c["close"] > ema21
        )
        breakout_extension = max(0.0, c["close"] - breakout_high) / atr
        ema_distance = max(0.0, c["close"] - ema21) / atr
    elif direction == "bearish":
        breakout = c["close"] < breakout_low
        trend = (
            ema9 < ema21 < ema50
            and ema9 < ema9_prev
            and ema21 < ema21_prev
            and ema50 <= ema50_prev
            and c["close"] < ema21
        )
        breakout_extension = max(0.0, breakout_low - c["close"]) / atr
        ema_distance = max(0.0, ema21 - c["close"]) / atr
    else:
        breakout = False
        trend = False
        breakout_extension = 999.0
        ema_distance = 999.0

    consecutive = _consecutive(data, direction) if direction in ("bullish", "bearish") else 0

    checks = [
        breakout,
        trend,
        c["body_ratio"] >= MIN_BODY_RATIO,
        body_atr >= MIN_BODY_ATR,
        range_atr >= MIN_RANGE_ATR,
        close_strength >= MIN_CLOSE_POSITION,
        compression_ok,
        pre_breakout_test,
        breakout_extension <= MAX_BREAKOUT_EXTENSION_ATR,
        ema_distance <= MAX_EMA_DISTANCE_ATR,
        consecutive <= MAX_CONSECUTIVE,
    ]
    names = [
        "ruptura", "tendencia", "cuerpo", "cuerpo_ATR", "rango_ATR",
        "cierre", "compresion", "test_previo", "extension",
        "ubicacion_EMA21", "agotamiento",
    ]
    weights = [18, 10, 8, 8, 7, 8, 10, 10, 8, 6, 7]
    score = int(sum(w for ok, w in zip(checks, weights) if ok))

    reasons = []
    if breakout: reasons.append("ruptura M1 confirmada")
    if trend: reasons.append("tendencia EMA 9/21/50 confirmada")
    if c["body_ratio"] >= MIN_BODY_RATIO: reasons.append("cuerpo de desplazamiento fuerte")
    if body_atr >= MIN_BODY_ATR: reasons.append("cuerpo suficiente frente al ATR")
    if range_atr >= MIN_RANGE_ATR: reasons.append("rango suficiente frente al ATR")
    if close_strength >= MIN_CLOSE_POSITION: reasons.append("cierre fuerte en el extremo")
    if compression_ok: reasons.append("compresion previa")
    if pre_breakout_test: reasons.append("precio previo cerca de la ruptura")
    if breakout_extension <= MAX_BREAKOUT_EXTENSION_ATR: reasons.append("entrada no demasiado extendida")
    if ema_distance <= MAX_EMA_DISTANCE_ATR: reasons.append("distancia a EMA21 controlada")
    if consecutive <= MAX_CONSECUTIVE: reasons.append("sin agotamiento por velas consecutivas")

    ts = int(current["from"]) if "from" in data.columns and pd.notna(current["from"]) else None
    result.update({
        "direction": direction,
        "trend": direction if trend else "range",
        "rsi": rsi,
        "atr": atr,
        "candle_timestamp": ts,
        "analysis": {
            "timeframe": "M1",
            "momentum": True,
            "atr": atr,
            "breakout_high": breakout_high,
            "breakout_low": breakout_low,
            "breakout_extension_atr": breakout_extension,
            "ema_distance_atr": ema_distance,
            "body_ratio": c["body_ratio"],
            "body_atr": body_atr,
            "range_atr": range_atr,
            "close_strength": close_strength,
            "compression_range_atr": compression_range_atr,
            "compression_avg_range_atr": compression_avg_atr,
            "pre_breakout_distance_atr": pre_breakout_distance,
            "consecutive": consecutive,
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
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
        "reason": f"{signal.upper()} | MOMENTUM M1 CONFIRMADO | " + "; ".join(reasons),
    })
    return result


def get_signal(df: pd.DataFrame):
    return analyze_market(df=df).get("signal")


def signal(df: pd.DataFrame):
    return get_signal(df)
