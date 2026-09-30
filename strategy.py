from __future__ import annotations

"""SNIPER OTC - M1 intrabar: operar SOLO dentro de la vela de fuerza.

REGLA
-----
CALL:
- Contexto M1 alcista (HH + HL usando solo velas cerradas anteriores).
- El CI(14) cruza ARRIBA 61.8 mientras la vela M1 actual esta abierta.
- La vela de fuerza actual es VERDE.
- La accion del precio confirma retroceso/rechazo: mecha inferior,
  cierre actual en la mitad superior y no rompe el ultimo swing low.
- Entrada en cualquier momento de ESA MISMA vela M1. No se espera a la
  siguiente vela y no existe un limite de segundos desde la apertura.

PUT:
- Contexto M1 bajista (LH + LL usando solo velas cerradas anteriores).
- El CI(14) cruza ABAJO 38.2 mientras la vela M1 actual esta abierta.
- La vela de fuerza actual es ROJA.
- Accion del precio: mecha superior, cierre actual en la mitad inferior
  y no rompe el ultimo swing high.
- Entrada en cualquier momento de ESA MISMA vela M1.

No usa M5, EMA, RSI, MACD, Bollinger, ATR, volumen ni S/R.
"""

import math
import os
from typing import Any, Dict, Optional

import pandas as pd

MIN_BARS = 35
CI_PERIOD = 14
OVERBOUGHT = 61.8
OVERSOLD = 38.2

MIN_CI_CROSS_DELTA = float(os.getenv("MIN_CI_CROSS_DELTA", "0.75"))
MIN_CI_PENETRATION = float(os.getenv("MIN_CI_PENETRATION", "0.35"))
STRUCTURE_BARS = int(os.getenv("STRUCTURE_BARS", "20"))
SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "2"))
MAX_SIGNAL_BODY_RATIO = float(os.getenv("MAX_SIGNAL_BODY_RATIO", "0.70"))
MIN_REJECTION_WICK_RATIO = float(os.getenv("MIN_REJECTION_WICK_RATIO", "0.20"))
CALL_MIN_CLOSE_POS = float(os.getenv("CALL_MIN_CLOSE_POS", "0.55"))
PUT_MAX_CLOSE_POS = float(os.getenv("PUT_MAX_CLOSE_POS", "0.45"))

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1}
}


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None, "direction": "range", "trend": "range",
        "higher_trend": "range", "reason": reason, "score": 0,
        "blocked": True, "mode": "M1_M1", "analysis_timeframe": "M1",
        "entry_timeframe": "M1", "target_expiration_minutes": 1,
        "analysis": {},
    }


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    d = df.copy().rename(columns={"max": "high", "min": "low"})
    required = ["open", "high", "low", "close"]
    if any(c not in d.columns for c in required):
        return pd.DataFrame()
    for c in required:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    if "from" in d.columns:
        d["from"] = pd.to_numeric(d["from"], errors="coerce")
        d = d.sort_values("from").drop_duplicates("from")
    return d.dropna(subset=required).reset_index(drop=True)


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close = data["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr_sum = tr.rolling(period, min_periods=period).sum()
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    price_range = hh - ll
    ci = pd.Series(float("nan"), index=data.index, dtype=float)
    valid = tr_sum.notna() & price_range.notna() & (price_range > 0) & (tr_sum > 0)
    ci.loc[valid] = 100.0 * (tr_sum.loc[valid] / price_range.loc[valid]).map(math.log10) / math.log10(period)
    return ci


def _pivot_points(data: pd.DataFrame, lookback: int):
    highs, lows = [], []
    if len(data) < 2 * lookback + 1:
        return highs, lows
    for i in range(lookback, len(data) - lookback):
        h = float(data.iloc[i]["high"]); l = float(data.iloc[i]["low"])
        lh = data.iloc[i-lookback:i]["high"].astype(float)
        rh = data.iloc[i+1:i+lookback+1]["high"].astype(float)
        ll = data.iloc[i-lookback:i]["low"].astype(float)
        rl = data.iloc[i+1:i+lookback+1]["low"].astype(float)
        if h >= float(lh.max()) and h >= float(rh.max()): highs.append((i, h))
        if l <= float(ll.min()) and l <= float(rl.min()): lows.append((i, l))
    return highs, lows


def _m1_trend(data: pd.DataFrame) -> Dict[str, Any]:
    # The current force candle is the last row; structure comes only from
    # completed candles before it.
    if len(data) < 12:
        return {"structure": "range", "reason": "pocas velas M1"}
    context = data.iloc[:-1].iloc[-STRUCTURE_BARS:].reset_index(drop=True)
    highs, lows = _pivot_points(context, SWING_LOOKBACK)
    if len(highs) >= 2 and len(lows) >= 2:
        h1_i, h1 = highs[-2]; h2_i, h2 = highs[-1]
        l1_i, l1 = lows[-2]; l2_i, l2 = lows[-1]
        hh, hl = h2 > h1, l2 > l1
        lh, ll = h2 < h1, l2 < l1
        structure = "bullish" if hh and hl else "bearish" if lh and ll else "range"
        return {
            "structure": structure, "method": "M1_HH_HL_LH_LL",
            "higher_high": hh, "higher_low": hl, "lower_high": lh,
            "lower_low": ll, "previous_swing_high": h1,
            "latest_swing_high": h2, "previous_swing_low": l1,
            "latest_swing_low": l2, "high_indices": [h1_i, h2_i],
            "low_indices": [l1_i, l2_i],
        }
    recent = context.iloc[-5:]
    bc = int(sum(float(r["close"]) > float(r["open"]) for _, r in recent.iterrows()))
    rc = int(sum(float(r["close"]) < float(r["open"]) for _, r in recent.iterrows()))
    net = float(recent.iloc[-1]["close"]) - float(recent.iloc[0]["close"])
    structure = "bullish" if bc >= 3 and net > 0 else "bearish" if rc >= 3 and net < 0 else "range"
    return {"structure": structure, "method": "M1_candles_fallback", "bullish_count": bc, "bearish_count": rc, "net_move": net}


def _intrabar_cross(ci: pd.Series) -> Dict[str, Any]:
    if len(ci) < 2 or pd.isna(ci.iloc[-2]) or pd.isna(ci.iloc[-1]):
        return {"valid": False, "type": None, "reason": "CI insuficiente"}
    prev, curr = float(ci.iloc[-2]), float(ci.iloc[-1])
    delta = curr - prev
    call = prev <= OVERBOUGHT and curr > OVERBOUGHT and delta >= MIN_CI_CROSS_DELTA and curr - OVERBOUGHT >= MIN_CI_PENETRATION
    put = prev >= OVERSOLD and curr < OVERSOLD and -delta >= MIN_CI_CROSS_DELTA and OVERSOLD - curr >= MIN_CI_PENETRATION
    if call:
        return {"valid": True, "type": "call", "previous": prev, "current": curr, "delta": delta, "level": OVERBOUGHT}
    if put:
        return {"valid": True, "type": "put", "previous": prev, "current": curr, "delta": delta, "level": OVERSOLD}
    return {"valid": False, "type": None, "previous": prev, "current": curr, "delta": delta, "reason": "sin cruce valido"}


def _force_candle_action(data: pd.DataFrame, signal: str, trend: Dict[str, Any]) -> Dict[str, Any]:
    row = data.iloc[-1]
    op, hi, lo, cl = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(hi - lo, 1e-12)
    body_ratio = abs(cl - op) / rng
    upper_ratio = max(0.0, hi - max(op, cl)) / rng
    lower_ratio = max(0.0, min(op, cl) - lo) / rng
    close_pos = (cl - lo) / rng
    green, red = cl > op, cl < op
    latest_high = trend.get("latest_swing_high")
    latest_low = trend.get("latest_swing_low")

    if signal == "call":
        color_ok = green
        rejection_ok = lower_ratio >= MIN_REJECTION_WICK_RATIO and close_pos >= CALL_MIN_CLOSE_POS
        structure_ok = latest_low is not None and lo >= float(latest_low)
        body_ok = body_ratio <= MAX_SIGNAL_BODY_RATIO
        valid = color_ok and rejection_ok and structure_ok and body_ok
        desc = "vela FUERZA verde + rechazo inferior + cierre superior + HL intacto"
    else:
        color_ok = red
        rejection_ok = upper_ratio >= MIN_REJECTION_WICK_RATIO and close_pos <= PUT_MAX_CLOSE_POS
        structure_ok = latest_high is not None and hi <= float(latest_high)
        body_ok = body_ratio <= MAX_SIGNAL_BODY_RATIO
        valid = color_ok and rejection_ok and structure_ok and body_ok
        desc = "vela FUERZA roja + rechazo superior + cierre inferior + LH intacto"
    return {
        "valid": valid, "color": "green" if green else "red" if red else "neutral",
        "color_ok": color_ok, "body_ratio": body_ratio, "body_ok": body_ok,
        "upper_wick_ratio": upper_ratio, "lower_wick_ratio": lower_ratio,
        "close_position": close_pos, "rejection_ok": rejection_ok,
        "structure_ok": structure_ok, "latest_swing_low": latest_low,
        "latest_swing_high": latest_high, "description": desc,
    }


def analyze_market(df: Optional[pd.DataFrame] = None, pair: Optional[str] = None,
                   mode: str = "M1_M1", higher_tf_df: Optional[pd.DataFrame] = None,
                   **kwargs: Any) -> Dict[str, Any]:
    del pair, higher_tf_df, kwargs
    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial M1 insuficiente {len(data)}/{MIN_BARS}")

    ci = _choppiness_index(data)
    trend = _m1_trend(data)
    cross = _intrabar_cross(ci)
    last_ts = int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None
    analysis = {
        "analysis_timeframe": "M1", "trend_timeframe": "M1", "trend": trend,
        "choppiness_period": CI_PERIOD, "overbought": OVERBOUGHT, "oversold": OVERSOLD,
        "cross": cross, "force_candle_timestamp": last_ts, "intrabar": True,
    }
    if not cross["valid"]:
        r = _empty(f"M1 {trend['structure']} | sin cruce valido | CI {cross.get('previous', float('nan')):.2f}->{cross.get('current', float('nan')):.2f}")
        r.update({"trend": trend["structure"], "direction": trend["structure"], "analysis": analysis})
        return r

    signal, structure = cross["type"], trend["structure"]
    if (signal == "call" and structure != "bullish") or (signal == "put" and structure != "bearish"):
        r = _empty(f"{signal.upper()} bloqueado | estructura M1={structure} no acompaña el cruce")
        r.update({"trend": structure, "direction": structure, "analysis": analysis})
        return r

    action = _force_candle_action(data, signal, trend)
    analysis["price_action"] = action
    if not action["valid"]:
        r = _empty(f"{signal.upper()} bloqueado | vela de fuerza sin confirmacion | {action['description']}")
        r.update({"trend": structure, "direction": structure, "analysis": analysis})
        return r

    if signal == "call":
        reason = f"CALL | M1 ALCISTA | CI 14 {cross['previous']:.2f}->{cross['current']:.2f} cruza ARRIBA 61.8 | vela FUERZA VERDE | rechazo confirmado | operar dentro de la M1 actual"
    else:
        reason = f"PUT | M1 BAJISTA | CI 14 {cross['previous']:.2f}->{cross['current']:.2f} cruza ABAJO 38.2 | vela FUERZA ROJA | rechazo confirmado | operar dentro de la M1 actual"
    return {
        "signal": signal, "direction": signal, "trend": structure, "higher_trend": "M1",
        "reason": reason, "score": 100, "blocked": False, "mode": mode,
        "analysis_timeframe": "M1", "entry_timeframe": "M1",
        "target_expiration_minutes": 1, "analysis": analysis,
    }


def get_signal(df: Optional[pd.DataFrame], **kwargs: Any) -> Optional[str]:
    return analyze_market(df=df, **kwargs).get("signal")


def signal(df: Optional[pd.DataFrame], **kwargs: Any) -> Optional[str]:
    return get_signal(df, **kwargs)
