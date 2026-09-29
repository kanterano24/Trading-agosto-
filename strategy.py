from __future__ import annotations

"""Estrategia direccional basada en estructura, tendencia e impulso.

Reglas:
- Analiza las ultimas 30 velas cerradas del timeframe de entrada.
- NO usa soporte, resistencia, rechazo S/R ni recorrido hacia zonas.
- Solo permite CALL cuando estructura + tendencia + impulso son alcistas.
- Solo permite PUT cuando estructura + tendencia + impulso son bajistas.
- La vela cerrada de entrada debe estar alineada con el impulso.
- M1/M2, ademas, deben estar alineados con la tendencia M5 usando 30 velas M5.
- Si alguna condicion principal es ambigua o contraria, la entrada se bloquea.
- Solo se usan velas cerradas.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 30
STRUCTURE_BARS = 30
SWING_LEFT = 2
SWING_RIGHT = 2

IMPULSE_BARS = 5
IMPULSE_MIN_ALIGNED = 3
IMPULSE_MIN_BODY = 0.40
IMPULSE_MIN_DISPLACEMENT = 0.80

TREND_MIN_DISPLACEMENT = 1.00
TREND_STRONG_DISPLACEMENT = 2.00
MIN_SWINGS_FOR_STRUCTURE = 2

DOJI_BODY_MAX = 0.10
INDECISION_BODY_MAX = 0.20
CONFIRM_BODY_MIN = 0.30

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
        "candle_timestamp": None,
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
        d = d.sort_values("from")

    return d.dropna(subset=required).reset_index(drop=True)


def _direction(row: pd.Series) -> str:
    o = float(row["open"])
    c = float(row["close"])
    if c > o:
        return "bullish"
    if c < o:
        return "bearish"
    return "neutral"


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "body_ratio": body / rng,
        "close_pos": (c - l) / rng,
    }


def _swings(data: pd.DataFrame) -> Tuple[list, list]:
    d = data.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
    highs, lows = [], []

    for i in range(SWING_LEFT, len(d) - SWING_RIGHT):
        h = float(d.iloc[i]["high"])
        l = float(d.iloc[i]["low"])
        left_high = float(d.iloc[i - SWING_LEFT:i]["high"].max())
        right_high = float(d.iloc[i + 1:i + 1 + SWING_RIGHT]["high"].max())
        left_low = float(d.iloc[i - SWING_LEFT:i]["low"].min())
        right_low = float(d.iloc[i + 1:i + 1 + SWING_RIGHT]["low"].min())

        if h >= left_high and h >= right_high:
            highs.append((i, h))
        if l <= left_low and l <= right_low:
            lows.append((i, l))

    return highs, lows


def _structure(data: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _swings(data)
    bullish = bearish = False

    if len(highs) >= MIN_SWINGS_FOR_STRUCTURE and len(lows) >= MIN_SWINGS_FOR_STRUCTURE:
        last_h, prev_h = highs[-1][1], highs[-2][1]
        last_l, prev_l = lows[-1][1], lows[-2][1]
        bullish = last_h > prev_h and last_l > prev_l
        bearish = last_h < prev_h and last_l < prev_l

    # En tendencias limpias puede no existir un swing confirmado reciente.
    # En ese caso medimos la estructura de 30 velas por mitades, sin usar S/R.
    if not bullish and not bearish:
        d = data.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
        half = len(d) // 2
        first = d.iloc[:half]
        second = d.iloc[half:]
        first_high = float(first["high"].max())
        second_high = float(second["high"].max())
        first_low = float(first["low"].min())
        second_low = float(second["low"].min())
        first_close = float(first.iloc[-1]["close"])
        second_close = float(second.iloc[-1]["close"])
        bullish = second_high > first_high and second_low > first_low and second_close > first_close
        bearish = second_high < first_high and second_low < first_low and second_close < first_close

    structure = "bullish" if bullish else "bearish" if bearish else "range"
    return {
        "structure": structure,
        "highs": highs,
        "lows": lows,
        "high_count": len(highs),
        "low_count": len(lows),
        "last_high": highs[-1][1] if highs else None,
        "previous_high": highs[-2][1] if len(highs) >= 2 else None,
        "last_low": lows[-1][1] if lows else None,
        "previous_low": lows[-2][1] if len(lows) >= 2 else None,
    }


def _trend_30(data: pd.DataFrame) -> Dict[str, Any]:
    d = data.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
    first_close = float(d.iloc[0]["close"])
    last_close = float(d.iloc[-1]["close"])
    net = last_close - first_close

    ranges = (d["high"] - d["low"]).astype(float)
    typical = max(float(ranges.median()), 1e-12)
    displacement = abs(net) / typical

    dirs = [_direction(row) for _, row in d.iterrows()]
    bull_count = sum(x == "bullish" for x in dirs)
    bear_count = sum(x == "bearish" for x in dirs)

    if net > 0 and displacement >= TREND_MIN_DISPLACEMENT:
        trend = "bullish"
    elif net < 0 and displacement >= TREND_MIN_DISPLACEMENT:
        trend = "bearish"
    else:
        trend = "range"

    return {
        "trend": trend,
        "net_move": net,
        "displacement_ranges": displacement,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "strength": min(1.0, displacement / TREND_STRONG_DISPLACEMENT),
        "bars": STRUCTURE_BARS,
    }


def _combined_trend(data: pd.DataFrame) -> Dict[str, Any]:
    st = _structure(data)
    tr = _trend_30(data)
    combined = st["structure"] if st["structure"] == tr["trend"] and st["structure"] in ("bullish", "bearish") else "range"
    return {
        "trend": combined,
        "swing": st["structure"],
        "price": tr["trend"],
        "structure": st,
        "trend_detail": tr,
    }


def _impulse(data: pd.DataFrame) -> Dict[str, Any]:
    d = data.iloc[-IMPULSE_BARS:].reset_index(drop=True)
    dirs = [_direction(row) for _, row in d.iterrows()]
    bull_count = sum(x == "bullish" for x in dirs)
    bear_count = sum(x == "bearish" for x in dirs)

    net = float(d.iloc[-1]["close"]) - float(d.iloc[0]["close"])
    ranges = (d["high"] - d["low"]).astype(float)
    typical = max(float(ranges.median()), 1e-12)
    displacement = abs(net) / typical
    avg_body = float(sum(_metrics(row)["body_ratio"] for _, row in d.iterrows()) / len(d))

    bullish = bull_count >= IMPULSE_MIN_ALIGNED and net > 0 and displacement >= IMPULSE_MIN_DISPLACEMENT and avg_body >= IMPULSE_MIN_BODY
    bearish = bear_count >= IMPULSE_MIN_ALIGNED and net < 0 and displacement >= IMPULSE_MIN_DISPLACEMENT and avg_body >= IMPULSE_MIN_BODY
    impulse = "bullish" if bullish else "bearish" if bearish else "range"
    aligned_count = bull_count if impulse == "bullish" else bear_count if impulse == "bearish" else max(bull_count, bear_count)

    consistency = aligned_count / IMPULSE_BARS
    displacement_strength = min(1.0, displacement / 2.0)
    body_strength = min(1.0, avg_body / 0.70)
    strength = 0.40 * consistency + 0.40 * displacement_strength + 0.20 * body_strength

    return {
        "impulse": impulse,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "aligned_count": aligned_count,
        "net_move": net,
        "displacement_ranges": displacement,
        "average_body_ratio": avg_body,
        "strength": strength,
        "bars": IMPULSE_BARS,
    }


def _candle_info(data: pd.DataFrame) -> Dict[str, Any]:
    cur = _metrics(data.iloc[-1])
    prev = _metrics(data.iloc[-2])
    direction = _direction(data.iloc[-1])
    return {
        **cur,
        "direction": direction,
        "previous_direction": _direction(data.iloc[-2]),
        "doji": cur["body_ratio"] <= DOJI_BODY_MAX,
        "indecision": cur["body_ratio"] <= INDECISION_BODY_MAX,
        "close_progress_bull": cur["close"] > prev["close"],
        "close_progress_bear": cur["close"] < prev["close"],
    }


def _directional_score(trend: str, structure: str, impulse: str, candle_direction: str, trend_detail: Dict[str, Any], impulse_detail: Dict[str, Any]) -> int:
    if trend not in ("bullish", "bearish") or structure != trend or impulse != trend or candle_direction != trend:
        return 0
    score = 80
    score += int(round(10 * float(trend_detail.get("strength", 0.0))))
    score += int(round(10 * float(impulse_detail.get("strength", 0.0))))
    return min(100, max(80, score))


def _blocked(data: pd.DataFrame, st: Dict[str, Any], info: Dict[str, Any], local: Dict[str, Any], mode: str, reason: str, higher: str = "range", higher_detail: Optional[Dict[str, Any]] = None, impulse: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = _empty(reason, mode)
    ts = None
    if not data.empty and "from" in data.columns and pd.notna(data.iloc[-1]["from"]):
        ts = int(data.iloc[-1]["from"])
    r.update({
        "direction": info.get("direction", "neutral"),
        "trend": local.get("trend", st.get("structure", "range")),
        "higher_trend": higher,
        "candle_timestamp": ts,
        "analysis": {
            "bars_analyzed": min(len(data), STRUCTURE_BARS),
            "structure": st,
            "trend": local.get("trend", "range"),
            "trend_detail": local.get("trend_detail", {}),
            "impulse": impulse or {},
            "higher_structure": higher,
            "higher_detail": higher_detail or {},
            "candle": info,
        },
    })
    return r


def analyze_market(df: Optional[pd.DataFrame] = None, pair: Optional[str] = None, mode: str = "M1_M1", higher_tf_df: Optional[pd.DataFrame] = None, **kwargs: Any) -> Dict[str, Any]:
    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _blocked(data, {"structure": "range", "highs": [], "lows": []}, {"direction": "neutral", "doji": True, "indecision": True}, {"trend": "range"}, mode, f"historial insuficiente {len(data)}/{MIN_BARS} velas")

    # Solo las ultimas 30 velas cerradas entran en la decision.
    data = data.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
    local = _combined_trend(data)
    trend = local["trend"]
    st = local["structure"]
    trend_detail = local["trend_detail"]
    info = _candle_info(data)
    impulse_detail = _impulse(data)
    impulse = impulse_detail["impulse"]

    higher_trend = "range"
    higher_detail: Dict[str, Any] = {}

    if mode in ("M1_M1", "M2_M2"):
        if higher_tf_df is not None and isinstance(higher_tf_df, pd.DataFrame) and not higher_tf_df.empty:
            h = _normalize(higher_tf_df)
            if len(h) >= MIN_BARS:
                h = h.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
                higher_detail = _combined_trend(h)
                higher_trend = higher_detail["trend"]

        if higher_trend not in ("bullish", "bearish"):
            return _blocked(data, st, info, local, mode, "M1/M2 bloqueado: tendencia M5 no clara en las ultimas 30 velas", higher_trend, higher_detail, impulse_detail)
        if trend not in ("bullish", "bearish"):
            return _blocked(data, st, info, local, mode, "M1/M2 bloqueado: estructura/tendencia local no clara en 30 velas", higher_trend, higher_detail, impulse_detail)
        if trend != higher_trend:
            return _blocked(data, st, info, local, mode, f"M1/M2 bloqueado: tendencia local {trend} contra M5 {higher_trend}", higher_trend, higher_detail, impulse_detail)
        allowed = higher_trend
    else:
        if trend not in ("bullish", "bearish"):
            return _blocked(data, st, info, local, mode, "M5 bloqueado: estructura/tendencia no clara en las ultimas 30 velas", higher_trend, higher_detail, impulse_detail)
        allowed = trend

    if st["structure"] != allowed:
        return _blocked(data, st, info, local, mode, f"entrada bloqueada: estructura {st['structure']} no acompana {allowed}", higher_trend, higher_detail, impulse_detail)

    if trend_detail["trend"] != allowed:
        return _blocked(data, st, info, local, mode, f"entrada bloqueada: tendencia de 30 velas {trend_detail['trend']} no acompana {allowed}", higher_trend, higher_detail, impulse_detail)

    if impulse != allowed:
        return _blocked(data, st, info, local, mode, f"entrada bloqueada: impulso {impulse} no acompana la tendencia {allowed}", higher_trend, higher_detail, impulse_detail)

    if info["doji"] or info["indecision"]:
        return _blocked(data, st, info, local, mode, "entrada bloqueada: ultima vela cerrada sin fuerza suficiente", higher_trend, higher_detail, impulse_detail)

    if info["direction"] != allowed:
        return _blocked(data, st, info, local, mode, f"entrada bloqueada: ultima vela {info['direction']} contra {allowed}", higher_trend, higher_detail, impulse_detail)

    if allowed == "bullish" and not info["close_progress_bull"]:
        return _blocked(data, st, info, local, mode, "entrada bloqueada: el cierre final no continua el impulso alcista", higher_trend, higher_detail, impulse_detail)
    if allowed == "bearish" and not info["close_progress_bear"]:
        return _blocked(data, st, info, local, mode, "entrada bloqueada: el cierre final no continua el impulso bajista", higher_trend, higher_detail, impulse_detail)

    if info["body_ratio"] < CONFIRM_BODY_MIN:
        return _blocked(data, st, info, local, mode, "entrada bloqueada: cuerpo de la ultima vela demasiado debil", higher_trend, higher_detail, impulse_detail)

    signal = "call" if allowed == "bullish" else "put"
    score = _directional_score(allowed, st["structure"], impulse, info["direction"], trend_detail, impulse_detail)

    if score < 80:
        return _blocked(data, st, info, local, mode, "entrada bloqueada: confluencia insuficiente", higher_trend, higher_detail, impulse_detail)

    reason = (
        f"{signal.upper()} | estructura {st['structure']} | "
        f"tendencia 30v {trend_detail['trend']} ({trend_detail['displacement_ranges']:.1f}R) | "
        f"impulso {impulse} {impulse_detail['aligned_count']}/{IMPULSE_BARS} | score {score}/100"
    )

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
            "bars_analyzed": STRUCTURE_BARS,
            "structure": trend,
            "local_swing": st["structure"],
            "trend_detail": trend_detail,
            "impulse": impulse_detail,
            "higher_structure": higher_trend,
            "higher_detail": higher_detail,
            "allowed_direction": allowed,
            "candle": info,
        },
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
