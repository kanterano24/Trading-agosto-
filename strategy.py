"""strategy.py - MOMENTUM M1 + UBICACION + RECHAZO S/R.

La vela N cerrada genera una señal para ejecutar en N+1.

PASO 1:
- Evita entradas demasiado extendidas, tardias o sin espacio.

PASO 2:
- Exige que la entrada este relacionada con una zona de soporte/resistencia.
- Busca rechazo real de la zona (mecha + recuperacion del nivel).
- Permite que N sea la propia vela de rechazo o la vela de confirmacion
  inmediatamente posterior.
- La entrada se bloquea si hay momentum pero no existe rechazo S/R.

La estrategia NO ejecuta operaciones; solo devuelve la senal.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 35
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 5

# Momentum
MIN_BODY_RATIO = 0.55
MIN_BODY_ATR = 0.35
MAX_BODY_ATR = 4.0
MIN_CLOSE_POSITION = 0.75
MIN_RANGE_ATR = 0.70
MIN_MOMENTUM_SCORE = 70
MAX_CONSECUTIVE = 5

# PASO 1: ubicacion
LOCATION_LOOKBACK = 20
LOCATION_EXCLUDE_RECENT = BREAKOUT_LOOKBACK
MIN_ROOM_ATR = 0.50
MAX_ENTRY_EXTENSION_ATR = 1.75
MAX_LATE_CONSECUTIVE = 3

# PASO 2: soporte / resistencia + rechazo
SR_LOOKBACK = 30
SR_PIVOT_LEFT = 2
SR_PIVOT_RIGHT = 2
SR_ZONE_TOL_ATR = 0.35
MIN_REJECTION_WICK_RATIO = 0.25
MIN_REJECTION_WICK_BODY = 0.80
MIN_REJECTION_CLOSE_POS = 0.65
CONFIRMATION_MAX_BARS = 1
MIN_REJECTION_SCORE = 20
REJECTION_MIN_BODY_RATIO = 0.20
REJECTION_MIN_BODY_ATR = 0.25
REJECTION_MIN_RANGE_ATR = 0.55
REJECTION_MIN_CLOSE_POSITION = 0.65

EPS = 1e-12


def _empty_result(reason="sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": "momentum",
        "entry_type": "momentum_rejection_sr",
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

    sort_col = "from" if "from" in out.columns else required[0]
    return (
        out.dropna(subset=required)
        .sort_values(sort_col)
        .reset_index(drop=True)
    )


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    rolling = tr.rolling(period).mean()
    if len(df) < period or pd.isna(rolling.iloc[-1]):
        return 0.0
    return float(rolling.iloc[-1])


def _rsi(s: pd.Series, period: int = 14) -> float:
    d = s.diff()
    gain = d.clip(lower=0).rolling(period).mean()
    loss = (-d.clip(upper=0)).rolling(period).mean()
    if pd.isna(gain.iloc[-1]) or pd.isna(loss.iloc[-1]):
        return 50.0
    if loss.iloc[-1] == 0:
        return 100.0 if gain.iloc[-1] > 0 else 50.0
    return float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1]))


def _metrics(c: pd.Series) -> Tuple[float, float, float, float]:
    rng = max(float(c.high - c.low), EPS)
    body = abs(float(c.close - c.open))
    body_ratio = body / rng
    close_pos = float(c.close - c.low) / rng
    return body, rng, body_ratio, close_pos


def _location_reference(hist: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return hist
    if len(hist) > LOCATION_EXCLUDE_RECENT:
        base = hist.iloc[:-LOCATION_EXCLUDE_RECENT]
    else:
        base = hist.iloc[:0]
    return base.tail(LOCATION_LOOKBACK)


def _pivot_levels(hist: pd.DataFrame) -> Tuple[list[float], list[float]]:
    """Obtiene pivotes simples para no tratar cualquier vela aislada como S/R."""
    if len(hist) < SR_PIVOT_LEFT + SR_PIVOT_RIGHT + 1:
        return [], []

    start = max(0, len(hist) - SR_LOOKBACK)
    x = hist.iloc[start:].reset_index(drop=True)
    supports: list[float] = []
    resistances: list[float] = []

    for i in range(SR_PIVOT_LEFT, len(x) - SR_PIVOT_RIGHT):
        lo = float(x.iloc[i].low)
        hi = float(x.iloc[i].high)
        left_lows = x.iloc[i-SR_PIVOT_LEFT:i].low
        right_lows = x.iloc[i+1:i+1+SR_PIVOT_RIGHT].low
        left_highs = x.iloc[i-SR_PIVOT_LEFT:i].high
        right_highs = x.iloc[i+1:i+1+SR_PIVOT_RIGHT].high

        if lo <= float(left_lows.min()) and lo <= float(right_lows.min()):
            supports.append(lo)
        if hi >= float(left_highs.max()) and hi >= float(right_highs.max()):
            resistances.append(hi)

    return supports, resistances


def _nearest_zone(
    price: float,
    levels: list[float],
    atr: float,
    direction: str,
) -> Tuple[Optional[float], float]:
    """Devuelve el nivel mas cercano en la zona operable."""
    if not levels or atr <= 0:
        return None, float("inf")

    if direction == "bullish":
        candidates = [x for x in levels if x <= price + SR_ZONE_TOL_ATR * atr]
    else:
        candidates = [x for x in levels if x >= price - SR_ZONE_TOL_ATR * atr]

    if not candidates:
        return None, float("inf")

    level = min(candidates, key=lambda x: abs(price - x))
    return float(level), abs(price - level) / atr


def _fallback_zones(hist: pd.DataFrame) -> Tuple[float, float]:
    ref = hist.tail(SR_LOOKBACK)
    if ref.empty:
        return 0.0, 0.0
    return float(ref.low.min()), float(ref.high.max())


def _candle_rejection(c: pd.Series, zone: float, atr: float, direction: str) -> Dict[str, Any]:
    rng = max(float(c.high - c.low), EPS)
    body = abs(float(c.close - c.open))
    lower_wick = min(float(c.open), float(c.close)) - float(c.low)
    upper_wick = float(c.high) - max(float(c.open), float(c.close))
    close_pos = (float(c.close) - float(c.low)) / rng
    close_from_high = (float(c.high) - float(c.close)) / rng
    distance = abs(float(c.close) - zone) / max(atr, EPS)

    if direction == "bullish":
        wick = lower_wick
        wick_ratio = lower_wick / rng
        wick_body = lower_wick / max(body, EPS)
        rejected = (
            float(c.low) <= zone + SR_ZONE_TOL_ATR * atr
            and float(c.close) > zone
            and wick_ratio >= MIN_REJECTION_WICK_RATIO
            and wick_body >= MIN_REJECTION_WICK_BODY
            and close_pos >= MIN_REJECTION_CLOSE_POS
            and float(c.close) >= float(c.open)
        )
    else:
        wick = upper_wick
        wick_ratio = upper_wick / rng
        wick_body = upper_wick / max(body, EPS)
        rejected = (
            float(c.high) >= zone - SR_ZONE_TOL_ATR * atr
            and float(c.close) < zone
            and wick_ratio >= MIN_REJECTION_WICK_RATIO
            and wick_body >= MIN_REJECTION_WICK_BODY
            and close_from_high >= MIN_REJECTION_CLOSE_POS
            and float(c.close) <= float(c.open)
        )

    quality = 0
    if rejected:
        quality += 10
        if wick_ratio >= 0.35:
            quality += 5
        if wick_body >= 1.25:
            quality += 5
        if distance <= 0.25:
            quality += 5

    return {
        "rejected": bool(rejected),
        "wick": float(wick),
        "wick_ratio": float(wick_ratio),
        "wick_body": float(wick_body),
        "close_pos": float(close_pos),
        "distance_atr": float(distance),
        "quality": int(min(25, quality)),
    }


def _find_rejection_setup(
    data: pd.DataFrame,
    current: pd.Series,
    atr: float,
    direction: str,
    support: Optional[float],
    resistance: Optional[float],
) -> Dict[str, Any]:
    zone = support if direction == "bullish" else resistance
    if zone is None or atr <= 0:
        return {
            "ok": False,
            "confirmed": False,
            "zone": zone,
            "rejection_index": None,
            "bars_after_rejection": None,
            "quality": 0,
            "details": {},
        }

    current_idx = len(data) - 1
    candidates = [(current_idx, current)]
    if current_idx > 0:
        candidates.append((current_idx - 1, data.iloc[current_idx - 1]))

    # Primero intentamos la vela actual. Si la vela anterior rechazo la zona,
    # la vela N actual debe confirmar el movimiento.
    for idx, candle in candidates:
        rej = _candle_rejection(candle, zone, atr, direction)
        if not rej["rejected"]:
            continue

        bars_after = current_idx - idx
        confirmed = True
        if bars_after == 1:
            if direction == "bullish":
                confirmed = (
                    float(current.close) > float(current.open)
                    and float(current.close) > float(candle.high)
                )
            else:
                confirmed = (
                    float(current.close) < float(current.open)
                    and float(current.close) < float(candle.low)
                )

        if confirmed:
            return {
                "ok": True,
                "confirmed": True,
                "zone": float(zone),
                "rejection_index": int(idx),
                "bars_after_rejection": int(bars_after),
                "quality": int(rej["quality"]),
                "details": rej,
            }

    return {
        "ok": False,
        "confirmed": False,
        "zone": float(zone),
        "rejection_index": None,
        "bars_after_rejection": None,
        "quality": 0,
        "details": {},
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    candle_5m: Any = None,
    previous_m5: Optional[pd.DataFrame] = None,
    m1_block=None,
    pair=None,
    **kwargs,
):
    if df is None:
        if previous_m1 is not None:
            base = previous_m1.copy()
            if candle_1m is not None:
                base = pd.concat([base, pd.DataFrame([candle_1m])], ignore_index=True)
        elif previous_m5 is not None:
            base = previous_m5.copy()
            if candle_5m is not None:
                base = pd.concat([base, pd.DataFrame([candle_5m])], ignore_index=True)
        else:
            base = pd.DataFrame()
    else:
        base = df.copy()

    data = _normalize(base)
    result = _empty_result()
    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"
        return result

    current = data.iloc[-1]
    hist = data.iloc[:-1]
    atr = _atr(data)
    rsi = _rsi(data["close"])
    if atr <= 0:
        result["reason"] = "ATR M1 inválido"
        return result

    body, rng, body_ratio, close_pos = _metrics(current)
    direction = (
        "bullish" if current.close > current.open
        else "bearish" if current.close < current.open
        else "range"
    )

    ema9_series = _ema(data.close, EMA_FAST)
    ema21_series = _ema(data.close, EMA_MID)
    ema50_series = _ema(data.close, EMA_SLOW)
    ema9 = ema9_series.iloc[-1]
    ema21 = ema21_series.iloc[-1]
    ema50 = ema50_series.iloc[-1]
    ema9p = ema9_series.iloc[-4]
    ema21p = ema21_series.iloc[-4]

    recent = hist.tail(BREAKOUT_LOOKBACK)
    prev_high = float(recent.high.max())
    prev_low = float(recent.low.min())
    bullish_break = float(current.close) > prev_high
    bearish_break = float(current.close) < prev_low

    body_atr = body / atr
    range_atr = rng / atr

    trend_bull = (
        ema9 > ema21 > ema50
        and ema9 > ema9p
        and ema21 > ema21p
        and current.close > ema21
    )
    trend_bear = (
        ema9 < ema21 < ema50
        and ema9 < ema9p
        and ema21 < ema21p
        and current.close < ema21
    )

    dirs = [
        "bull" if r.close > r.open else "bear" if r.close < r.open else "neutral"
        for _, r in data.tail(MAX_CONSECUTIVE + 1).iterrows()
    ]
    consecutive = 0
    wanted = "bull" if direction == "bullish" else "bear"
    if direction != "range":
        for d in reversed(dirs):
            if d == wanted:
                consecutive += 1
            else:
                break

    # ========================================================
    # PASO 1 - UBICACION
    # ========================================================
    location_ref = _location_reference(hist)
    if not location_ref.empty:
        major_high = float(location_ref.high.max())
        major_low = float(location_ref.low.min())
    else:
        major_high = float(prev_high)
        major_low = float(prev_low)

    if direction == "bullish":
        entry_extension_atr = (float(current.close) - float(ema21)) / atr
        room_to_resistance = (major_high - float(current.close)) / atr
    elif direction == "bearish":
        entry_extension_atr = (float(ema21) - float(current.close)) / atr
        room_to_support = (float(current.close) - major_low) / atr
    else:
        room_to_resistance = room_to_support = 0.0
        entry_extension_atr = 0.0

    if direction == "bullish":
        room_atr = max(0.0, room_to_resistance)
        if float(current.close) > major_high:
            room_atr = float("inf")
    elif direction == "bearish":
        room_atr = max(0.0, room_to_support)
        if float(current.close) < major_low:
            room_atr = float("inf")
    else:
        room_atr = 0.0

    late_entry = consecutive > MAX_LATE_CONSECUTIVE
    too_extended = entry_extension_atr > MAX_ENTRY_EXTENSION_ATR
    too_close_to_zone = room_atr < MIN_ROOM_ATR
    location_ok = not (late_entry or too_extended or too_close_to_zone)

    # ========================================================
    # PASO 2 - S/R + RECHAZO + CONFIRMACION
    # ========================================================
    supports, resistances = _pivot_levels(hist)
    fallback_support, fallback_resistance = _fallback_zones(hist)

    support, support_dist_atr = _nearest_zone(
        float(current.close), supports, atr, "bullish"
    )
    resistance, resistance_dist_atr = _nearest_zone(
        float(current.close), resistances, atr, "bearish"
    )

    if support is None and fallback_support:
        if abs(float(current.close) - fallback_support) / atr <= SR_ZONE_TOL_ATR:
            support = fallback_support
            support_dist_atr = abs(float(current.close) - support) / atr

    if resistance is None and fallback_resistance:
        if abs(float(current.close) - fallback_resistance) / atr <= SR_ZONE_TOL_ATR:
            resistance = fallback_resistance
            resistance_dist_atr = abs(float(current.close) - resistance) / atr

    rejection = _find_rejection_setup(
        data=data,
        current=current,
        atr=atr,
        direction=direction,
        support=support,
        resistance=resistance,
    )

    rejection_ok = bool(rejection["ok"] and rejection["confirmed"])

    # Para que la entrada sea coherente con el rechazo, la tendencia no tiene
    # que ser perfecta: un rechazo contra una zona puede ser el inicio del
    # giro. Sin embargo, exigimos desplazamiento/cuerpo razonable.
    displacement_ok = (
        body_ratio >= REJECTION_MIN_BODY_RATIO
        and body_atr >= REJECTION_MIN_BODY_ATR
        and range_atr >= REJECTION_MIN_RANGE_ATR
    )

    checks = []
    reasons: list[str] = []
    if direction == "bullish":
        checks = [
            body_ratio >= REJECTION_MIN_BODY_RATIO,
            body_atr >= REJECTION_MIN_BODY_ATR,
            range_atr >= REJECTION_MIN_RANGE_ATR,
            close_pos >= REJECTION_MIN_CLOSE_POSITION,
            consecutive <= MAX_CONSECUTIVE,
            displacement_ok,
        ]
        if bullish_break:
            reasons.append("rompe máximo reciente")
        if body_ratio >= MIN_BODY_RATIO:
            reasons.append("cuerpo fuerte")
        if body_atr >= MIN_BODY_ATR:
            reasons.append("cuerpo con fuerza ATR")
        if close_pos >= MIN_CLOSE_POSITION:
            reasons.append("cierre cerca del máximo")
        if trend_bull:
            reasons.append("EMA 9/21/50 alcistas")
    elif direction == "bearish":
        close_pos_bear = 1 - close_pos
        checks = [
            body_ratio >= REJECTION_MIN_BODY_RATIO,
            body_atr >= REJECTION_MIN_BODY_ATR,
            range_atr >= REJECTION_MIN_RANGE_ATR,
            close_pos_bear >= REJECTION_MIN_CLOSE_POSITION,
            consecutive <= MAX_CONSECUTIVE,
            displacement_ok,
        ]
        if bearish_break:
            reasons.append("rompe mínimo reciente")
        if body_ratio >= MIN_BODY_RATIO:
            reasons.append("cuerpo fuerte")
        if body_atr >= MIN_BODY_ATR:
            reasons.append("cuerpo con fuerza ATR")
        if close_pos_bear >= MIN_CLOSE_POSITION:
            reasons.append("cierre cerca del mínimo")
        if trend_bear:
            reasons.append("EMA 9/21/50 bajistas")
    else:
        checks = []

    momentum_ok = bool(checks) and all(checks)

    score = 0
    if direction != "range":
        score += 10 if body_ratio >= MIN_BODY_RATIO else 0
        score += 10 if body_atr >= MIN_BODY_ATR else 0
        score += 10 if range_atr >= MIN_RANGE_ATR else 0
        close_condition = (close_pos >= MIN_CLOSE_POSITION) if direction == "bullish" else (close_pos <= (1 - MIN_CLOSE_POSITION))
        score += 10 if close_condition else 0
        score += 10 if (trend_bull if direction == "bullish" else trend_bear) else 0
        score += 10 if consecutive <= MAX_CONSECUTIVE else 0
        score += 10 if displacement_ok else 0
        score += 10 if (bullish_break if direction == "bullish" else bearish_break) else 0
        score += 10 if location_ok else 0
        score += int(min(20, rejection.get("quality", 0)))

    score = int(min(100, score))

    location_penalty = 0
    if late_entry:
        location_penalty += 10
        reasons.append("entrada tardía: demasiadas velas consecutivas")
    if too_extended:
        location_penalty += 10
        reasons.append("precio demasiado extendido respecto a EMA 21")
    if too_close_to_zone:
        location_penalty += 10
        reasons.append("poco espacio hasta zona extrema previa")
    score = max(0, score - location_penalty)

    if rejection_ok:
        if direction == "bullish":
            reasons.append("rechazo confirmado de soporte")
        else:
            reasons.append("rechazo confirmado de resistencia")
        if rejection.get("bars_after_rejection") == 1:
            reasons.append("confirmación en la vela siguiente al rechazo")
    else:
        reasons.append("sin rechazo S/R confirmado")

    if not momentum_ok:
        result["reason"] = "Momentum M1 insuficiente"
    elif not location_ok:
        result["reason"] = "Entrada bloqueada por ubicación"
    elif not rejection_ok:
        result["reason"] = "Entrada bloqueada: falta rechazo S/R confirmado"
    elif score < MIN_MOMENTUM_SCORE:
        result["reason"] = f"Score insuficiente: {score}/100"
    else:
        result["reason"] = "Condiciones completas"

    ts = int(current["from"]) if "from" in data.columns and pd.notna(current["from"]) else None

    result.update(
        {
            "direction": direction,
            "trend": direction,
            "rsi": rsi,
            "atr": atr,
            "candle_timestamp": ts,
            "score": int(score),
            "entry_quality": int(score),
            "blocked": not (momentum_ok and location_ok and rejection_ok and score >= MIN_MOMENTUM_SCORE),
            "analysis": {
                "timeframe": "M1",
                "momentum": True,
                "location_filter": True,
                "sr_rejection_filter": True,
                "breakout_high": prev_high,
                "breakout_low": prev_low,
                "major_resistance": major_high,
                "major_support": major_low,
                "support_zone": support,
                "resistance_zone": resistance,
                "support_distance_atr": float(support_dist_atr),
                "resistance_distance_atr": float(resistance_dist_atr),
                "rejection": rejection,
                "rejection_ok": rejection_ok,
                "displacement_ok": displacement_ok,
                "location_ok": location_ok,
                "location_penalty": location_penalty,
                "body_ratio": body_ratio,
                "body_atr": body_atr,
                "range_atr": range_atr,
                "close_position": close_pos,
                "consecutive": consecutive,
                "entry_extension_atr": float(entry_extension_atr),
                "room_to_zone_atr": float(room_atr),
                "late_entry": late_entry,
                "too_extended": too_extended,
                "too_close_to_zone": too_close_to_zone,
                "momentum_ok": momentum_ok,
                "trend_bull": trend_bull,
                "trend_bear": trend_bear,
                "ema9": float(ema9),
                "ema21": float(ema21),
                "ema50": float(ema50),
                "reasons": reasons,
            },
        }
    )

    if momentum_ok and location_ok and rejection_ok and score >= MIN_MOMENTUM_SCORE:
        result.update(
            {
                "signal": "call" if direction == "bullish" else "put",
                "continuity": True,
                "blocked": False,
                "zone": "support_rejection" if direction == "bullish" else "resistance_rejection",
                "entry_type": "MOMENTUM_M1_REJECTION_SR",
                "reason": (
                    ("CALL" if direction == "bullish" else "PUT")
                    + " | MOMENTUM M1 | RECHAZO S/R | "
                    + "; ".join(reasons)
                ),
            }
        )

    return result


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
