from __future__ import annotations

"""Estrategia de accion del precio para M1, M2 y M5.

Reglas principales:
- M1 -> expiracion 1 minuto.
- M2 -> expiracion 2 minutos.
- M5 -> expiracion 5 minutos.
- Solo usa velas cerradas y accion del precio; no usa indicadores.
- La entrada siempre va en la direccion de la vela analizada.
- M1 y M2 no pueden operar contra la tendencia M5 cerrada.
- Una vela de reversa por si sola NO genera una entrada contra la tendencia.
- Dojis/indecision bloquean la entrada.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 30
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 30
SR_LOOKBACK = 20
ZONE_TOLERANCE = 0.0015
DOJI_BODY_MAX = 0.10
INDECISION_BODY_MAX = 0.25
STRONG_BODY_MIN = 0.55
WICK_BODY_MIN = 1.20

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
    "M2_M2": {"analysis_tf": "M2", "entry_tf": "M2", "expiration": 2},
    "M5_M5": {"analysis_tf": "M5", "entry_tf": "M5", "expiration": 5},
}


def _empty(reason: str = "sin señal", mode: str = "") -> Dict[str, Any]:
    cfg = MODE_CONFIG.get(mode, {})
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": mode,
        "analysis_timeframe": cfg.get("analysis_tf"),
        "entry_timeframe": cfg.get("entry_tf"),
        "target_expiration_minutes": cfg.get("expiration"),
        "analysis": {},
    }


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d = d.rename(columns={"max": "high", "min": "low"})
    required = ["open", "high", "low", "close"]
    if any(c not in d.columns for c in required):
        return pd.DataFrame()
    for c in required:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    if "from" in d.columns:
        d["from"] = pd.to_numeric(d["from"], errors="coerce")
        d = d.sort_values("from")
    return d.dropna(subset=required).reset_index(drop=True)


def _direction(row: pd.Series) -> str:
    if float(row["close"]) > float(row["open"]):
        return "bullish"
    if float(row["close"]) < float(row["open"]):
        return "bearish"
    return "neutral"


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "open": o, "high": h, "low": l, "close": c,
        "range": rng, "body": body,
        "body_ratio": body / rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_body": upper / max(body, 1e-12),
        "lower_body": lower / max(body, 1e-12),
        "close_pos": (c - l) / rng,
    }


def _swings(data: pd.DataFrame) -> Tuple[list, list]:
    highs, lows = [], []
    start = max(SWING_LEFT, len(data) - SWING_LOOKBACK - SWING_RIGHT)
    end = len(data) - SWING_RIGHT
    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        lh = data.iloc[i-SWING_LEFT:i]["high"].max()
        rh = data.iloc[i+1:i+1+SWING_RIGHT]["high"].max()
        ll = data.iloc[i-SWING_LEFT:i]["low"].min()
        rl = data.iloc[i+1:i+1+SWING_RIGHT]["low"].min()
        if h >= float(lh) and h >= float(rh):
            highs.append((i, h))
        if l <= float(ll) and l <= float(rl):
            lows.append((i, l))
    return highs, lows


def _structure(data: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _swings(data)
    structure = "range"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            structure = "bullish"
        elif lh and ll:
            structure = "bearish"
    return {
        "structure": structure,
        "highs": highs,
        "lows": lows,
        "last_high": highs[-1][1] if highs else None,
        "previous_high": highs[-2][1] if len(highs) >= 2 else None,
        "last_low": lows[-1][1] if lows else None,
        "previous_low": lows[-2][1] if len(lows) >= 2 else None,
    }


def _candle_info(data: pd.DataFrame) -> Dict[str, Any]:
    i = len(data) - 1
    cur = _metrics(data.iloc[i])
    prev = _metrics(data.iloc[i - 1]) if i else cur
    direction = _direction(data.iloc[i])
    prev_direction = _direction(data.iloc[i - 1]) if i else "neutral"
    doji = cur["body_ratio"] <= DOJI_BODY_MAX
    indecision = cur["body_ratio"] <= INDECISION_BODY_MAX
    strong = cur["body_ratio"] >= STRONG_BODY_MIN
    bullish_momentum = (
        direction == "bullish" and cur["body_ratio"] >= 0.50
        and cur["close_pos"] >= 0.70 and cur["close"] > prev["high"]
    )
    bearish_momentum = (
        direction == "bearish" and cur["body_ratio"] >= 0.50
        and cur["close_pos"] <= 0.30 and cur["close"] < prev["low"]
    )
    bullish_engulf = (
        direction == "bullish" and prev_direction == "bearish"
        and cur["open"] <= prev["close"] and cur["close"] >= prev["open"]
    )
    bearish_engulf = (
        direction == "bearish" and prev_direction == "bullish"
        and cur["open"] >= prev["close"] and cur["close"] <= prev["open"]
    )
    upper_rejection = cur["upper_body"] >= WICK_BODY_MIN and cur["close_pos"] <= 0.45
    lower_rejection = cur["lower_body"] >= WICK_BODY_MIN and cur["close_pos"] >= 0.55
    return {
        **cur,
        "direction": direction,
        "previous_direction": prev_direction,
        "doji": doji,
        "indecision": indecision,
        "strong": strong,
        "bullish_momentum": bullish_momentum,
        "bearish_momentum": bearish_momentum,
        "bullish_engulf": bullish_engulf,
        "bearish_engulf": bearish_engulf,
        "upper_rejection": upper_rejection,
        "lower_rejection": lower_rejection,
    }


def _recent_pressure(data: pd.DataFrame, trend: str) -> Dict[str, Any]:
    if len(data) < 4:
        return {"aligned": False, "aligned_count": 0, "opposite_count": 0}
    recent = data.iloc[-4:]
    dirs = [_direction(row) for _, row in recent.iterrows()]
    aligned = "bullish" if trend == "bullish" else "bearish" if trend == "bearish" else None
    aligned_count = sum(1 for x in dirs if x == aligned)
    opposite_count = sum(1 for x in dirs if x not in (aligned, "neutral")) if aligned else 0
    return {"aligned": aligned_count >= 2, "aligned_count": aligned_count, "opposite_count": opposite_count, "directions": dirs}


def _zones(data: pd.DataFrame, st: Dict[str, Any]):
    recent = data.iloc[-SR_LOOKBACK:]
    support = st.get("last_low")
    resistance = st.get("last_high")
    if support is None:
        support = float(recent["low"].min())
    if resistance is None:
        resistance = float(recent["high"].max())
    return support, resistance


def _zone_rejection(cur: pd.Series, support, resistance) -> Dict[str, Any]:
    m = _metrics(cur)
    near_support = m["low"] <= support * (1 + ZONE_TOLERANCE) and m["close"] >= support
    near_resistance = m["high"] >= resistance * (1 - ZONE_TOLERANCE) and m["close"] <= resistance
    bull = near_support and m["lower_body"] >= WICK_BODY_MIN and m["close_pos"] >= 0.60
    bear = near_resistance and m["upper_body"] >= WICK_BODY_MIN and m["close_pos"] <= 0.40
    if near_support and near_resistance:
        zone = "ambiguous"
    elif near_support:
        zone = "support"
    elif near_resistance:
        zone = "resistance"
    else:
        zone = "none"
    return {"zone": zone, "bull_rejection": bull, "bear_rejection": bear,
            "near_support": near_support, "near_resistance": near_resistance}


def _blocked(data, st, info, zone, mode, reason, higher="range"):
    r = _empty(reason, mode)
    ts = None
    if "from" in data.columns and pd.notna(data.iloc[-1]["from"]):
        ts = int(data.iloc[-1]["from"])
    r.update({
        "direction": info["direction"],
        "trend": st["structure"],
        "higher_trend": higher,
        "candle_timestamp": ts,
        "analysis": {
            "structure": st["structure"], "higher_structure": higher,
            "candle": info, "zone": zone,
        },
    })
    return r


def analyze_market(df: Optional[pd.DataFrame] = None, pair: Optional[str] = None,
                   mode: str = "M1_M1", higher_tf_df: Optional[pd.DataFrame] = None,
                   **kwargs: Any) -> Dict[str, Any]:
    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)
    result = _empty(mode=mode)
    if len(data) < MIN_BARS:
        return _blocked(data, {"structure": "range"}, {"direction": "neutral"}, {}, mode,
                        f"historial insuficiente {len(data)}/{MIN_BARS}")

    st = _structure(data)
    trend = st["structure"]
    info = _candle_info(data)
    support, resistance = _zones(data, st)
    zone = _zone_rejection(data.iloc[-1], support, resistance)

    higher_trend = "range"
    if higher_tf_df is not None and isinstance(higher_tf_df, pd.DataFrame) and not higher_tf_df.empty:
        h = _normalize(higher_tf_df)
        if len(h) >= MIN_BARS:
            higher_trend = _structure(h)["structure"]

    # M1 y M2 deben respetar M5. M5 se evalua con su propia estructura.
    if mode in ("M1_M1", "M2_M2") and higher_trend in ("bullish", "bearish"):
        allowed = higher_trend
    else:
        allowed = trend

    if trend == "range":
        return _blocked(data, st, info, zone, mode, "sin tendencia clara", higher_trend)

    if higher_trend in ("bullish", "bearish") and mode in ("M1_M1", "M2_M2"):
        if trend in ("bullish", "bearish") and trend != higher_trend:
            # Puede ser un retroceso; nunca se toma como una reversa contra M5.
            return _blocked(data, st, info, zone, mode,
                            f"estructura {trend} contra M5 {higher_trend}", higher_trend)

    # La vela cerrada debe ir en la direccion de la entrada.
    if info["doji"] or info["indecision"]:
        return _blocked(data, st, info, zone, mode, "vela cerrada sin direccion suficiente", higher_trend)

    signal = "call" if info["direction"] == "bullish" else "put"
    if signal == "call" and allowed != "bullish":
        return _blocked(data, st, info, zone, mode, "CALL contra tendencia", higher_trend)
    if signal == "put" and allowed != "bearish":
        return _blocked(data, st, info, zone, mode, "PUT contra tendencia", higher_trend)

    pressure = _recent_pressure(data, allowed)
    continuation = pressure["aligned"] or (
        signal == "call" and info["bullish_momentum"]
    ) or (
        signal == "put" and info["bearish_momentum"]
    )

    # Reversa solo es valida si vuelve a favor de la tendencia superior.
    aligned_reversal = (
        (signal == "call" and allowed == "bullish" and info["bullish_engulf"] and info["previous_direction"] == "bearish")
        or
        (signal == "put" and allowed == "bearish" and info["bearish_engulf"] and info["previous_direction"] == "bullish")
    )

    # En M1/M2 no basta una vela aislada: exigimos presion o momentum.
    # En M5 permitimos continuidad fuerte o rechazo en zona real.
    rejection = (
        (signal == "call" and zone.get("zone") == "support" and zone.get("bull_rejection"))
        or
        (signal == "put" and zone.get("zone") == "resistance" and zone.get("bear_rejection"))
    )

    if mode in ("M1_M1", "M2_M2"):
        valid = continuation or aligned_reversal
    else:
        valid = continuation or aligned_reversal or rejection

    if not valid:
        return _blocked(data, st, info, zone, mode,
                        "sin continuidad suficiente para confirmar la entrada", higher_trend)

    # No permitimos comprar dentro de impulso bajista ni vender dentro de impulso alcista.
    if signal == "call" and allowed != "bullish":
        return _blocked(data, st, info, zone, mode, "CALL contra movimiento", higher_trend)
    if signal == "put" and allowed != "bearish":
        return _blocked(data, st, info, zone, mode, "PUT contra movimiento", higher_trend)

    score = 50
    score += 15 if trend == allowed else 0
    score += 15 if pressure["aligned_count"] >= 3 else 5 if pressure["aligned_count"] >= 2 else 0
    score += 10 if (info["bullish_momentum"] or info["bearish_momentum"]) else 0
    score += 10 if aligned_reversal else 0
    score += 10 if rejection else 0
    score = min(100, score)

    reason = (
        f"{signal.upper()} | tendencia {allowed} | vela anterior {signal} | "
        f"presion {pressure['aligned_count']}/4 | "
        f"{'momentum' if (info['bullish_momentum'] or info['bearish_momentum']) else 'continuidad'}"
    )
    if aligned_reversal:
        reason += " | reversion alineada"
    if rejection:
        reason += " | rechazo en zona"

    ts = int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None
    return {
        "signal": signal,
        "direction": info["direction"],
        "trend": trend,
        "higher_trend": higher_trend,
        "reason": reason,
        "score": score,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": MODE_CONFIG[mode]["analysis_tf"],
        "entry_timeframe": MODE_CONFIG[mode]["entry_tf"],
        "target_expiration_minutes": MODE_CONFIG[mode]["expiration"],
        "candle_timestamp": ts,
        "analysis": {
            "structure": trend, "higher_structure": higher_trend,
            "allowed_direction": allowed, "candle": info,
            "pressure": pressure, "continuation": continuation,
            "aligned_reversal": aligned_reversal, "zone": zone,
            "rejection": rejection,
        },
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
