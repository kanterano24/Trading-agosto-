"""
strategy.py

ESTRATEGIA ESTRUCTURAL DE RECHAZO + FASE DE IMPULSO
+ CONTINUIDAD + DESCANSO + INDECISIÓN + FUERZA
+ DIVERGENCIA RSI ESTRUCTURAL PARA BINARY OTC M1.

CAMBIO SOLICITADO:
- Hacer mucho más selectiva la generación de señales.
- Solo entregar señal cuando SCORE > 90/100.
- Solo entregar señal cuando PROBABILIDAD/CONFIANZA >= 90/100.
- La probabilidad es una medida interna de confluencia de la estrategia,
  NO una probabilidad estadística garantizada de ganar.
- Se mantiene N como vela analizada y N+1 como ejecución en bot.py.
- Este módulo NO ejecuta operaciones.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import math

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURACIÓN
# ============================================================

MIN_BARS = 35
MAX_CANDLES = 80

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
SWING_LOOKBACK = 35
MIN_STRUCTURE_GAP_ATR = 0.05

ZONE_ATR = 0.28
MAX_ENTRY_DISTANCE_ATR = 0.40
MIN_ROOM_TO_OPPOSITE_ATR = 0.85

MIN_DISTANCE_FROM_ZONE_ATR = 0.35
MIN_DISTANCE_FROM_TRENDLINE_ATR = 0.30

MIN_BODY_RATIO = 0.25
MIN_REJECTION_WICK_RATIO = 0.42
MIN_WICK_BODY_RATIO = 1.40
MIN_CLOSE_POSITION_CALL = 0.68
MAX_CLOSE_POSITION_PUT = 0.32

MIN_BODY_ATR = 0.12
MAX_BODY_ATR = 1.35

IMPULSE_LOOKBACK = 12
MAX_IMPULSE_AGE = 5
MAX_IMPULSE_TOTAL_ATR = 3.20
MAX_CONSECUTIVE_DIRECTION_CANDLES = 5
MIN_IMPULSE_BODY_RATIO = 0.45
MIN_IMPULSE_BODY_ATR = 0.35
BREAKOUT_LOOKBACK = 5

LOOKBACK = 8
MIN_CANDLES = 4
MAX_RANGE_ATR = 2.20
MAX_DRIFT_ATR = 1.20

CALL_RSI_MIN = 38.0
CALL_RSI_MAX = 68.0
PUT_RSI_MIN = 32.0
PUT_RSI_MAX = 62.0

INDECISION_MAX_BODY_RATIO = 0.30
INDECISION_MIN_WICK_RATIO = 0.25

REST_MAX_BODY_RATIO = 0.50
REST_MIN_PREVIOUS_BODY_RATIO = 0.55

CONTINUITY_MIN_BODY_RATIO = 0.45
CONTINUITY_MIN_BODY_ATR = 0.20
CONTINUITY_MAX_BODY_ATR = 0.95
CONTINUITY_MIN_CLOSE_POSITION_CALL = 0.62
CONTINUITY_MAX_CLOSE_POSITION_PUT = 0.38
CONTINUITY_MIN_ROOM_ATR = 1.00
CONTINUITY_MAX_EXTENSION_ATR = 1.15

FORCE_MIN_BODY_RATIO = 0.65
FORCE_MIN_BODY_ATR = 0.35

DIVERGENCE_LOOKBACK = 25
DIVERGENCE_MIN_RSI_CHANGE = 2.0
DIVERGENCE_MIN_PRICE_CHANGE_ATR = 0.05

# Filtro nuevo de máxima precisión.
# "Mayor a 90" significa estrictamente 91..100.
MIN_ENTRY_SCORE = 91
MIN_ENTRY_PROBABILITY = 90

EPS = 1e-12


# ============================================================
# RESULTADO ESTABLE
# ============================================================

def _empty_result(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "probability": 0,
        "confidence": 0,
        "continuity": False,
        "blocked": True,
        "zone": None,
        "entry_type": None,
        "entry_quality": 0,
        "last_swing_high": None,
        "last_swing_low": None,
        "support": None,
        "resistance": None,
        "rsi": 0.0,
        "atr": 0.0,
        "candle_timestamp": None,
        "analysis": {},
    }


# ============================================================
# UTILIDADES
# ============================================================

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


# ============================================================
# NORMALIZACIÓN
# ============================================================

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    rename = {}
    if "max" in out.columns and "high" not in out.columns:
        rename["max"] = "high"
    if "min" in out.columns and "low" not in out.columns:
        rename["min"] = "low"

    rename.update({
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
    })

    out.rename(columns=rename, inplace=True)

    required = ["open", "high", "low", "close"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out.sort_values("from", inplace=True)
        out.drop_duplicates(subset=["from"], keep="last", inplace=True)

    out.dropna(subset=required, inplace=True)
    out.reset_index(drop=True, inplace=True)

    if len(out) > MAX_CANDLES:
        out = out.tail(MAX_CANDLES).reset_index(drop=True)

    return out


# ============================================================
# INDICADORES
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty:
        return out

    close = out["close"]
    high = out["high"]
    low = out["low"]

    out["ema9"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    out["ema21"] = close.ewm(span=EMA_MID, adjust=False).mean()
    out["ema50"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["tr"] = tr
    out["atr"] = tr.rolling(
        ATR_PERIOD,
        min_periods=ATR_PERIOD,
    ).mean()

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    out.loc[
        (avg_loss == 0) & (avg_gain > 0),
        "rsi",
    ] = 100.0

    out.loc[
        (avg_gain == 0) & (avg_loss > 0),
        "rsi",
    ] = 0.0

    return out


def _atr(history: pd.DataFrame) -> float:
    if history is None or history.empty:
        return 0.0

    if "tr" in history.columns:
        value = history["tr"].tail(ATR_PERIOD).mean()
    else:
        value = np.nan

    if pd.isna(value) or value <= 0:
        value = (
            history["high"] - history["low"]
        ).tail(ATR_PERIOD).mean()

    if pd.isna(value) or value <= 0:
        value = abs(float(history["close"].iloc[-1])) * 0.0001

    return float(max(value, EPS))


# ============================================================
# VELAS
# ============================================================

def candle_direction(candle: pd.Series) -> str:
    o = _safe_float(candle.get("open"))
    c = _safe_float(candle.get("close"))

    if c > o:
        return "bull"
    if c < o:
        return "bear"
    return "neutral"


def candle_metrics(candle: pd.Series) -> Dict[str, float]:
    o = _safe_float(candle.get("open"))
    h = _safe_float(candle.get("high"))
    l = _safe_float(candle.get("low"))
    c = _safe_float(candle.get("close"))

    rng = max(h - l, EPS)
    body = abs(c - o)
    upper = max(h - max(o, c), 0.0)
    lower = max(min(o, c) - l, 0.0)

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "upper": upper,
        "lower": lower,
        "body_ratio": body / rng,
        "close_position": (c - l) / rng,
    }


# ============================================================
# PIVOTES / ESTRUCTURA
# ============================================================

def _confirmed_swings(
    history: pd.DataFrame,
    left: int = PIVOT_LEFT,
    right: int = PIVOT_RIGHT,
) -> Tuple[list[Tuple[int, float]], list[Tuple[int, float]]]:
    highs: list[Tuple[int, float]] = []
    lows: list[Tuple[int, float]] = []

    if history is None or len(history) < left + right + 3:
        return highs, lows

    start = max(left, len(history) - SWING_LOOKBACK)
    end = len(history) - right

    for i in range(start, end):
        h = float(history["high"].iloc[i])
        l = float(history["low"].iloc[i])

        left_highs = history["high"].iloc[i - left:i]
        right_highs = history["high"].iloc[i + 1:i + right + 1]

        left_lows = history["low"].iloc[i - left:i]
        right_lows = history["low"].iloc[i + 1:i + right + 1]

        if (
            h >= float(left_highs.max())
            and h >= float(right_highs.max())
        ):
            highs.append((i, h))

        if (
            l <= float(left_lows.min())
            and l <= float(right_lows.min())
        ):
            lows.append((i, l))

    return highs, lows


def _last_swing_levels(history: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _confirmed_swings(history)

    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None

    w = history.tail(min(SWING_LOOKBACK, len(history)))

    if last_high is None and not w.empty:
        idx = int(w["high"].idxmax())
        last_high = (idx, float(w.loc[idx, "high"]))

    if last_low is None and not w.empty:
        idx = int(w["low"].idxmin())
        last_low = (idx, float(w.loc[idx, "low"]))

    return {
        "highs": highs,
        "lows": lows,
        "last_high": last_high,
        "last_low": last_low,
    }


def detect_structure(df: pd.DataFrame) -> str:
    work = _normalize(df)

    if len(work) < 12:
        return "range"

    swings = _last_swing_levels(work)
    highs = swings["highs"]
    lows = swings["lows"]

    if len(highs) >= 2 and len(lows) >= 2:
        h1 = highs[-2][1]
        h2 = highs[-1][1]
        l1 = lows[-2][1]
        l2 = lows[-1][1]

        atr = _atr(add_indicators(work))
        min_gap = max(atr * MIN_STRUCTURE_GAP_ATR, EPS)

        if h2 > h1 + min_gap and l2 > l1 + min_gap:
            return "bullish"

        if h2 < h1 - min_gap and l2 < l1 - min_gap:
            return "bearish"

    ind = add_indicators(work)

    if ind.empty:
        return "range"

    last = ind.iloc[-1]

    if last["ema9"] > last["ema21"] > last["ema50"]:
        return "bullish"

    if last["ema9"] < last["ema21"] < last["ema50"]:
        return "bearish"

    return "range"


def structure_score(df: pd.DataFrame) -> int:
    work = _normalize(df)

    if len(work) < 12:
        return 0

    swings = _last_swing_levels(work)
    highs = swings["highs"]
    lows = swings["lows"]

    score = 0

    if len(highs) >= 2:
        if highs[-1][1] != highs[-2][1]:
            score += 1

    if len(lows) >= 2:
        if lows[-1][1] != lows[-2][1]:
            score += 1

    structure = detect_structure(work)

    if structure in ("bullish", "bearish"):
        score += 2

    ind = add_indicators(work)

    if len(ind) >= 3:
        if (
            structure == "bullish"
            and ind["ema9"].iloc[-1] > ind["ema21"].iloc[-1]
        ):
            score += 1

        elif (
            structure == "bearish"
            and ind["ema9"].iloc[-1] < ind["ema21"].iloc[-1]
        ):
            score += 1

    return min(score, 5)


# ============================================================
# SOPORTE / RESISTENCIA
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = SWING_LOOKBACK,
) -> Tuple[float, float]:
    work = _normalize(df)

    if work.empty:
        return 0.0, 0.0

    x = work.tail(lookback)

    return (
        float(x["low"].min()),
        float(x["high"].max()),
    )


def _zone_test(
    candle: Dict[str, float],
    level: float,
    atr: float,
    side: str,
) -> Tuple[bool, float, str]:
    zone = max(atr * ZONE_ATR, EPS)

    if side == "support":
        touched = candle["low"] <= level + zone
        closed_above = candle["close"] > level

        wick_ok = (
            candle["lower"] / candle["range"]
            >= MIN_REJECTION_WICK_RATIO
            or candle["lower"]
            >= candle["body"] * MIN_WICK_BODY_RATIO
        )

        close_ok = (
            candle["close_position"]
            >= MIN_CLOSE_POSITION_CALL
        )

        valid = (
            touched
            and closed_above
            and wick_ok
            and close_ok
        )

        distance = abs(candle["close"] - level) / max(atr, EPS)

        return valid, distance, "rechazo de soporte"

    touched = candle["high"] >= level - zone
    closed_below = candle["close"] < level

    wick_ok = (
        candle["upper"] / candle["range"]
        >= MIN_REJECTION_WICK_RATIO
        or candle["upper"]
        >= candle["body"] * MIN_WICK_BODY_RATIO
    )

    close_ok = (
        candle["close_position"]
        <= MAX_CLOSE_POSITION_PUT
    )

    valid = (
        touched
        and closed_below
        and wick_ok
        and close_ok
    )

    distance = abs(candle["close"] - level) / max(atr, EPS)

    return valid, distance, "rechazo de resistencia"


def is_near_sr(
    df: pd.DataFrame,
    tolerance: float = 0.0,
) -> bool:
    work = _normalize(df)

    if len(work) < 5:
        return True

    atr = _atr(add_indicators(work))

    tol = (
        tolerance
        if tolerance > 0
        else atr * ZONE_ATR
    )

    low, high = recent_levels(work)
    price = float(work["close"].iloc[-1])

    return (
        abs(price - low) <= tol
        or abs(high - price) <= tol
    )


# ============================================================
# TENDENCIA / EXTENSIÓN
# ============================================================

def _ema_alignment(
    last: pd.Series,
    direction: str,
) -> bool:
    e9 = _safe_float(last.get("ema9"))
    e21 = _safe_float(last.get("ema21"))
    e50 = _safe_float(last.get("ema50"))
    close = _safe_float(last.get("close"))

    if direction == "bullish":
        return (
            e9 >= e21
            and e21 >= e50
            and close >= e21
        )

    return (
        e9 <= e21
        and e21 <= e50
        and close <= e21
    )


def _room_to_opposite(
    price: float,
    opposite_level: float,
    atr: float,
    direction: str,
) -> bool:
    if atr <= 0:
        return False

    if direction == "bullish":
        room = opposite_level - price
    else:
        room = price - opposite_level

    return room >= atr * MIN_ROOM_TO_OPPOSITE_ATR


def _not_overextended(
    price: float,
    last_high: float,
    last_low: float,
    atr: float,
    direction: str,
) -> bool:
    if atr <= 0:
        return False

    if direction == "bullish":
        return (
            last_high - price
            >= atr * MIN_ROOM_TO_OPPOSITE_ATR
        )

    return (
        price - last_low
        >= atr * MIN_ROOM_TO_OPPOSITE_ATR
    )


def _body_is_valid(
    candle: Dict[str, float],
    atr: float,
) -> bool:
    if atr <= 0:
        return False

    body_atr = candle["body"] / atr

    return (
        candle["body_ratio"] >= MIN_BODY_RATIO
        and MIN_BODY_ATR <= body_atr <= MAX_BODY_ATR
    )


# ============================================================
# FASE DE IMPULSO
# ============================================================

def _directional_streak(
    history: pd.DataFrame,
    direction: str,
) -> int:
    if history is None or history.empty:
        return 0

    streak = 0

    for i in range(len(history) - 1, -1, -1):
        o = float(history["open"].iloc[i])
        c = float(history["close"].iloc[i])

        if direction == "bullish" and c > o:
            streak += 1
        elif direction == "bearish" and c < o:
            streak += 1
        else:
            break

    return streak


def _impulse_state(
    history: pd.DataFrame,
    direction: str,
    atr: float,
) -> Dict[str, Any]:
    result = {
        "active": False,
        "phase": "sin_impulso",
        "age": 999,
        "total_atr": 0.0,
        "streak": 0,
        "exhausted": False,
    }

    if history is None or len(history) < 3 or atr <= 0:
        return result

    recent = history.tail(IMPULSE_LOOKBACK).copy()

    streak = _directional_streak(recent, direction)
    result["streak"] = streak

    if streak <= 0:
        return result

    age = 0
    total_body = 0.0

    for i in range(len(recent) - 1, -1, -1):
        o = float(recent["open"].iloc[i])
        c = float(recent["close"].iloc[i])

        correct = (
            c > o if direction == "bullish"
            else c < o
        )

        if not correct:
            break

        total_body += abs(c - o)
        age += 1

        if age >= IMPULSE_LOOKBACK:
            break

    total_atr = total_body / atr

    result["age"] = age
    result["total_atr"] = total_atr

    if age <= MAX_IMPULSE_AGE and total_atr <= MAX_IMPULSE_TOTAL_ATR:
        result["active"] = True
        result["phase"] = "inicio_impulso"

    elif age <= MAX_IMPULSE_AGE + 1:
        result["active"] = True
        result["phase"] = "impulso"

    else:
        result["phase"] = "impulso_avanzado"

    result["exhausted"] = (
        age > MAX_IMPULSE_AGE
        or total_atr > MAX_IMPULSE_TOTAL_ATR
        or streak > MAX_CONSECUTIVE_DIRECTION_CANDLES
    )

    return result


# ============================================================
# CONTINUIDAD / DESCANSO / INDECISIÓN / FUERZA
# ============================================================

def _is_indecision(c: Dict[str, float]) -> bool:
    wick_ratio = (
        (c["upper"] + c["lower"])
        / max(c["range"], EPS)
    )

    return (
        c["body_ratio"] <= INDECISION_MAX_BODY_RATIO
        and (
            c["upper"] / max(c["range"], EPS)
            >= INDECISION_MIN_WICK_RATIO
            or c["lower"] / max(c["range"], EPS)
            >= INDECISION_MIN_WICK_RATIO
        )
        and wick_ratio >= 0.45
    )


def _is_rest(
    c: Dict[str, float],
    previous: Dict[str, float],
) -> bool:
    return (
        c["body_ratio"] <= REST_MAX_BODY_RATIO
        and previous["body_ratio"] >= REST_MIN_PREVIOUS_BODY_RATIO
    )


def _is_continuity(
    c: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    if atr <= 0:
        return False

    body_atr = c["body"] / atr

    directional = (
        c["close"] > c["open"]
        if direction == "bullish"
        else c["close"] < c["open"]
    )

    return (
        directional
        and c["body_ratio"] >= CONTINUITY_MIN_BODY_RATIO
        and body_atr >= CONTINUITY_MIN_BODY_ATR
    )


def _rejection_has_confirmation(
    candle: Dict[str, float],
    previous: Dict[str, float],
    direction: str,
) -> bool:
    """
    Exige que la vela de rechazo cierre en la dirección esperada
    y que supere el cierre de la vela anterior. Esto evita entrar
    únicamente porque apareció una mecha cerca de una zona.
    """
    if direction == "bullish":
        return (
            candle["close"] > candle["open"]
            and candle["close"] > previous["close"]
        )

    return (
        candle["close"] < candle["open"]
        and candle["close"] < previous["close"]
    )


def _continuity_has_setup(
    candle: Dict[str, float],
    previous: Dict[str, float],
    previous2: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    """Confirma continuidad usando las velas anteriores de N."""
    if atr <= 0 or candle["body"] / atr > CONTINUITY_MAX_BODY_ATR:
        return False

    if direction == "bullish":
        return (
            candle["close"] > candle["open"]
            and candle["close"] > previous["high"]
            and candle["close_position"] >= CONTINUITY_MIN_CLOSE_POSITION_CALL
            and (
                previous["close"] <= previous["open"]
                or previous["body_ratio"] <= REST_MAX_BODY_RATIO
                or previous2["close"] <= previous2["open"]
            )
        )

    return (
        candle["close"] < candle["open"]
        and candle["close"] < previous["low"]
        and candle["close_position"] <= CONTINUITY_MAX_CLOSE_POSITION_PUT
        and (
            previous["close"] >= previous["open"]
            or previous["body_ratio"] <= REST_MAX_BODY_RATIO
            or previous2["close"] >= previous2["open"]
        )
    )


def _is_force(
    c: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    if atr <= 0:
        return False

    body_atr = c["body"] / atr

    directional = (
        c["close"] > c["open"]
        if direction == "bullish"
        else c["close"] < c["open"]
    )

    return (
        directional
        and c["body_ratio"] >= FORCE_MIN_BODY_RATIO
        and body_atr >= FORCE_MIN_BODY_ATR
    )


# ============================================================
# DIVERGENCIA RSI ESTRUCTURAL
# ============================================================

def _find_recent_pivot_pair(
    history: pd.DataFrame,
    direction: str,
) -> Optional[Tuple[int, int]]:
    if history is None or len(history) < 8:
        return None

    work = history.tail(DIVERGENCE_LOOKBACK).copy()
    offset = len(history) - len(work)

    if direction == "bullish":
        lows = work["low"].values
        pivots = []

        for i in range(1, len(work) - 1):
            if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
                pivots.append(i)

        if len(pivots) >= 2:
            return (
                offset + pivots[-2],
                offset + pivots[-1],
            )

    else:
        highs = work["high"].values
        pivots = []

        for i in range(1, len(work) - 1):
            if highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
                pivots.append(i)

        if len(pivots) >= 2:
            return (
                offset + pivots[-2],
                offset + pivots[-1],
            )

    return None


def detect_rsi_divergence(
    history: pd.DataFrame,
    atr: float,
) -> Dict[str, Any]:
    result = {
        "bullish": False,
        "bearish": False,
        "type": None,
        "price_change": 0.0,
        "rsi_change": 0.0,
        "first_index": None,
        "second_index": None,
    }

    if history is None or len(history) < 10:
        return result

    work = add_indicators(history)

    if work.empty or "rsi" not in work.columns:
        return result

    pair = _find_recent_pivot_pair(work, "bullish")

    if pair is not None:
        i1, i2 = pair

        p1 = float(work["low"].iloc[i1])
        p2 = float(work["low"].iloc[i2])
        r1 = _safe_float(work["rsi"].iloc[i1], 50.0)
        r2 = _safe_float(work["rsi"].iloc[i2], 50.0)

        price_change = p2 - p1
        rsi_change = r2 - r1

        result["price_change"] = price_change / max(atr, EPS)
        result["rsi_change"] = rsi_change
        result["first_index"] = i1
        result["second_index"] = i2

        if (
            p2 < p1
            and r2 > r1 + DIVERGENCE_MIN_RSI_CHANGE
            and abs(price_change) / max(atr, EPS)
            >= DIVERGENCE_MIN_PRICE_CHANGE_ATR
        ):
            result["bullish"] = True
            result["type"] = "bullish"

    pair = _find_recent_pivot_pair(work, "bearish")

    if pair is not None:
        i1, i2 = pair

        p1 = float(work["high"].iloc[i1])
        p2 = float(work["high"].iloc[i2])
        r1 = _safe_float(work["rsi"].iloc[i1], 50.0)
        r2 = _safe_float(work["rsi"].iloc[i2], 50.0)

        price_change = p2 - p1
        rsi_change = r2 - r1

        if (
            p2 > p1
            and r2 < r1 - DIVERGENCE_MIN_RSI_CHANGE
            and abs(price_change) / max(atr, EPS)
            >= DIVERGENCE_MIN_PRICE_CHANGE_ATR
        ):
            result["bearish"] = True
            result["type"] = "bearish"
            result["price_change"] = price_change / max(atr, EPS)
            result["rsi_change"] = rsi_change
            result["first_index"] = i1
            result["second_index"] = i2

    return result


# ============================================================
# TRENDLINE DINÁMICA
# ============================================================

def _trendline_distance_atr(
    history: pd.DataFrame,
    price: float,
    atr: float,
    direction: str,
) -> float:
    if atr <= 0 or history is None or len(history) < 6:
        return float("inf")

    swings = _last_swing_levels(history)

    if direction == "bullish":
        lows = swings["lows"]

        if len(lows) >= 2:
            x1, y1 = lows[-2]
            x2, y2 = lows[-1]

            if x2 != x1:
                slope = (y2 - y1) / (x2 - x1)
                projected = y2 + slope * (
                    len(history) - 1 - x2
                )

                return abs(price - projected) / atr

    else:
        highs = swings["highs"]

        if len(highs) >= 2:
            x1, y1 = highs[-2]
            x2, y2 = highs[-1]

            if x2 != x1:
                slope = (y2 - y1) / (x2 - x1)
                projected = y2 + slope * (
                    len(history) - 1 - x2
                )

                return abs(price - projected) / atr

    return float("inf")


# ============================================================
# PROBABILIDAD / CONFIANZA DE CONFLUENCIA
# ============================================================

def _calculate_probability(
    direction: str,
    structure: str,
    structure_score: int,
    ema_ok: bool,
    candle: Dict[str, float],
    atr: float,
    rsi: float,
    zone_distance: float,
    room_atr: float,
    impulse: Dict[str, Any],
    divergence: Dict[str, Any],
    rejection: bool,
    continuity: bool,
    rest: bool,
    force: bool,
    trendline_distance: float,
) -> Tuple[int, Dict[str, int]]:
    """
    Calcula una confianza interna de 0..100 basada en confluencias.
    No representa una probabilidad estadística de acierto.
    """

    points: Dict[str, int] = {
        "estructura": 0,
        "ema": 0,
        "vela": 0,
        "rsi": 0,
        "zona": 0,
        "espacio": 0,
        "impulso": 0,
        "divergencia": 0,
        "rechazo": 0,
        "tipo_entrada": 0,
        "tendencia_linea": 0,
    }

    if structure == direction:
        points["estructura"] = 18

    points["estructura"] += min(10, structure_score * 2)

    if ema_ok:
        points["ema"] = 12

    if atr > 0:
        body_atr = candle["body"] / atr

        if MIN_BODY_ATR <= body_atr <= MAX_BODY_ATR:
            points["vela"] = 8

        if candle["body_ratio"] >= 0.50:
            points["vela"] += 3

    if direction == "bullish":
        if CALL_RSI_MIN <= rsi <= CALL_RSI_MAX:
            points["rsi"] = 8
    else:
        if PUT_RSI_MIN <= rsi <= PUT_RSI_MAX:
            points["rsi"] = 8

    if zone_distance <= 0.30:
        points["zona"] = 10
    elif zone_distance <= MAX_ENTRY_DISTANCE_ATR:
        points["zona"] = 7

    if room_atr >= 1.20:
        points["espacio"] = 9
    elif room_atr >= MIN_ROOM_TO_OPPOSITE_ATR:
        points["espacio"] = 6

    if impulse.get("active"):
        points["impulso"] = 8

        if impulse.get("phase") == "inicio_impulso":
            points["impulso"] += 2

    if direction == "bullish" and divergence.get("bullish"):
        points["divergencia"] = 7

    elif direction == "bearish" and divergence.get("bearish"):
        points["divergencia"] = 7

    if rejection:
        points["rechazo"] = 10

    if continuity:
        points["tipo_entrada"] = 7

    elif rest:
        points["tipo_entrada"] = 6

    elif force:
        points["tipo_entrada"] = 6

    if trendline_distance >= MIN_DISTANCE_FROM_TRENDLINE_ATR:
        points["tendencia_linea"] = 3

    raw = sum(points.values())

    # Normalización a 100 con una exigencia alta.
    probability = int(round(_clamp(raw, 0, 100)))

    return probability, points


# ============================================================
# FILTRO FINAL DE MÁXIMA PRECISIÓN
# ============================================================

def _passes_precision_filter(
    score: int,
    probability: int,
    entry_quality: int,
) -> bool:
    return (
        score > 90
        and probability >= MIN_ENTRY_PROBABILITY
        and entry_quality >= MIN_ENTRY_SCORE
    )


# ============================================================
# CONSTRUCCIÓN DE DATAFRAME PARA LAS DOS API
# ============================================================

def _build_analysis_dataframe(
    candle_1m: Any = None,
    previous_m1: Any = None,
    df: Any = None,
) -> pd.DataFrame:
    """
    Compatible con:

        analyze_market(df)

    y con:

        analyze_market(
            candle_1m=...,
            previous_m1=...,
            pair=...
        )
    """

    if df is not None:
        return _normalize(df)

    previous = _normalize(previous_m1)

    if candle_1m is None:
        return previous

    if isinstance(candle_1m, pd.Series):
        row = candle_1m.to_dict()
    elif isinstance(candle_1m, dict):
        row = dict(candle_1m)
    else:
        return previous

    current = pd.DataFrame([row])

    current = _normalize(current)

    if previous.empty:
        return current

    if current.empty:
        return previous

    return _normalize(
        pd.concat(
            [previous, current],
            ignore_index=True,
        )
    )


# ============================================================
# API PRINCIPAL
# ============================================================

def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    result = _empty_result()

    clean = _build_analysis_dataframe(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        df=df,
    )

    if len(clean) < MIN_BARS:
        result["reason"] = (
            f"Historial insuficiente "
            f"{len(clean)}/{MIN_BARS}"
        )
        return result

    data = add_indicators(clean)

    if data.empty or len(data) < MIN_BARS:
        result["reason"] = "Indicadores insuficientes"
        return result

    # La última vela recibida es N, ya cerrada para bot.py.
    live = data.iloc[-1]
    previous = data.iloc[-2]
    previous2 = data.iloc[-3] if len(data) >= 3 else previous
    history = data.iloc[:-1].copy()

    atr = _atr(history)
    rsi = _safe_float(live.get("rsi"), 50.0)

    structure = detect_structure(history)
    s_score = structure_score(history)

    swings = _last_swing_levels(history)

    last_high = (
        swings["last_high"][1]
        if swings["last_high"]
        else None
    )

    last_low = (
        swings["last_low"][1]
        if swings["last_low"]
        else None
    )

    timestamp = None

    if "from" in data.columns:
        if not pd.isna(live.get("from")):
            timestamp = int(live["from"])

    result.update({
        "direction": structure,
        "trend": structure,
        "structure": structure,
        "structure_score": s_score,
        "atr": atr,
        "rsi": rsi,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "resistance": last_high,
        "support": last_low,
        "candle": candle_direction(live),
        "candle_timestamp": timestamp,
    })

    if pair:
        result["pair"] = pair

    if structure not in ("bullish", "bearish"):
        result["reason"] = "Estructura lateral/ambigua"
        return result

    if atr <= 0 or not math.isfinite(atr):
        result["reason"] = "ATR inválido"
        return result

    if last_high is None or last_low is None:
        result["reason"] = "No hay máximo/mínimo estructural"
        return result

    c = candle_metrics(live)
    p = candle_metrics(previous)

    price = c["close"]

    if not _body_is_valid(c, atr):
        result["reason"] = (
            "Vela N demasiado pequeña/grande"
        )
        return result

    # --------------------------------------------------------
    # Contexto de impulso
    # --------------------------------------------------------

    impulse = _impulse_state(
        history,
        structure,
        atr,
    )

    result["analysis"]["impulse"] = impulse

    if impulse["exhausted"]:
        result["reason"] = (
            "Impulso avanzado/exhausto"
        )
        result["analysis"]["impulse_phase"] = impulse["phase"]
        return result

    # --------------------------------------------------------
    # No perseguir el extremo opuesto
    # --------------------------------------------------------

    if not _not_overextended(
        price,
        last_high,
        last_low,
        atr,
        structure,
    ):
        result["reason"] = (
            "CALL bloqueado cerca del máximo"
            if structure == "bullish"
            else "PUT bloqueado cerca del mínimo"
        )
        result["zone"] = "extremo_opuesto"
        return result

    # --------------------------------------------------------
    # Tendencia dinámica
    # --------------------------------------------------------

    ema_ok = _ema_alignment(
        live,
        structure,
    )

    if not ema_ok:
        result["reason"] = (
            "EMA no confirma la estructura"
        )
        return result

    trendline_distance = _trendline_distance_atr(
        history,
        price,
        atr,
        structure,
    )

    if (
        math.isfinite(trendline_distance)
        and trendline_distance
        < MIN_DISTANCE_FROM_TRENDLINE_ATR
    ):
        result["reason"] = (
            "Precio demasiado cerca de la línea de tendencia"
        )
        result["zone"] = "trendline"
        return result

    # --------------------------------------------------------
    # Tipos de vela
    # --------------------------------------------------------

    indecision = _is_indecision(c)
    rest = _is_rest(c, p)
    continuity = _is_continuity(
        c,
        structure,
        atr,
    )
    force = _is_force(
        c,
        structure,
        atr,
    )

    # --------------------------------------------------------
    # Divergencia
    # --------------------------------------------------------

    divergence = detect_rsi_divergence(
        history,
        atr,
    )

    result["analysis"]["divergence"] = divergence

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if structure == "bullish":
        if not (
            CALL_RSI_MIN
            <= rsi
            <= CALL_RSI_MAX
        ):
            result["reason"] = (
                f"RSI CALL fuera de rango {rsi:.1f}"
            )
            return result

        support_ok, distance, zone_reason = _zone_test(
            c,
            last_low,
            atr,
            "support",
        )

        # La reversión solo existe con rechazo real.
        rejection = support_ok

        # Continuidad no puede comprar cerca de resistencia.
        near_resistance = (
            abs(price - last_high) / max(atr, EPS)
            < MIN_DISTANCE_FROM_ZONE_ATR
        )

        if near_resistance:
            result["reason"] = (
                "CALL bloqueado cerca de resistencia"
            )
            result["zone"] = "resistencia"
            return result

        room_atr = (
            last_high - price
        ) / max(atr, EPS)

        if room_atr < (CONTINUITY_MIN_ROOM_ATR if not rejection else MIN_ROOM_TO_OPPOSITE_ATR):
            result["reason"] = (
                "Poco espacio hasta resistencia"
            )
            return result

        if not rejection:
            extension_atr = abs(price - _safe_float(live.get("ema21"), price)) / max(atr, EPS)
            if extension_atr > CONTINUITY_MAX_EXTENSION_ATR:
                result["reason"] = "CALL bloqueado: continuidad demasiado extendida"
                return result

        continuity_setup = _continuity_has_setup(
            c, p, previous2, "bullish", atr
        )

        if not rejection and not continuity_setup:
            result["reason"] = (
                "CALL bloqueado: sin rechazo de soporte ni continuidad confirmada"
            )
            return result

        if rejection and not _rejection_has_confirmation(c, p, "bullish"):
            result["reason"] = (
                "CALL bloqueado: rechazo sin confirmación de cierre"
            )
            return result

        # Si es indecisión, N no dispara por sí sola.
        # Se exige contexto fuerte de rechazo/estructura.
        if indecision and not rejection:
            result["reason"] = (
                "Indecisión sin rechazo confirmado"
            )
            return result

        # Rechazo de soporte.
        if rejection:
            if distance > MAX_ENTRY_DISTANCE_ATR:
                result["reason"] = (
                    "Rechazo demasiado alejado del soporte"
                )
                return result

            if price <= p["close"]:
                result["reason"] = (
                    "Rechazo sin recuperación suficiente"
                )
                return result

        # Descanso solamente a favor de tendencia.
        if rest and structure != "bullish":
            result["reason"] = (
                "Descanso fuera de tendencia alcista"
            )
            return result

        # Fuerza solo al comienzo del movimiento.
        if force:
            if (
                not impulse["active"]
                or impulse["age"] > MAX_IMPULSE_AGE
            ):
                result["reason"] = (
                    "Vela de fuerza demasiado tarde"
                )
                return result

        probability, points = _calculate_probability(
            direction="bullish",
            structure=structure,
            structure_score=s_score,
            ema_ok=ema_ok,
            candle=c,
            atr=atr,
            rsi=rsi,
            zone_distance=distance
                if rejection
                else MAX_ENTRY_DISTANCE_ATR,
            room_atr=room_atr,
            impulse=impulse,
            divergence=divergence,
            rejection=rejection,
            continuity=continuity,
            rest=rest,
            force=force,
            trendline_distance=trendline_distance,
        )

        wick_strength = (
            c["lower"] / max(c["range"], EPS)
        )

        quality = int(round(
            50.0
            + min(20.0, wick_strength * 35.0)
            + min(15.0, s_score * 3.0)
            + (8.0 if ema_ok else 0.0)
            + (7.0 if divergence["bullish"] else 0.0)
        ))

        # Confluencia extra para continuidad limpia.
        if continuity:
            quality += 6

        if rest:
            quality += 4

        if force and impulse["phase"] == "inicio_impulso":
            quality += 5

        quality = int(_clamp(quality))

        score = int(
            round(
                (
                    quality * 0.55
                    + probability * 0.45
                )
            )
        )

        result["analysis"].update({
            "pattern": "CALL",
            "impulse_phase": impulse["phase"],
            "impulse_age": impulse["age"],
            "impulse_total_atr": impulse["total_atr"],
            "indecision": indecision,
            "rest": rest,
            "continuity": continuity,
            "force": force,
            "rejection": rejection,
            "rejection_confirmation": _rejection_has_confirmation(c, p, "bullish"),
            "probability_points": points,
            "trendline_distance_atr": trendline_distance,
            "room_to_opposite_atr": room_atr,
        })

        if not _passes_precision_filter(
            score,
            probability,
            quality,
        ):
            result["score"] = score
            result["probability"] = probability
            result["confidence"] = probability
            result["entry_quality"] = quality
            result["reason"] = (
                "CALL descartado por filtro de máxima precisión "
                f"| score={score}/100 "
                f"| probabilidad={probability}/100 "
                f"| calidad={quality}/100"
            )
            return result

        result.update({
            "signal": "call",
            "score": score,
            "probability": probability,
            "confidence": probability,
            "reason": (
                "CALL | ALTA CONFLUENCIA | "
                f"score={score}/100 | "
                f"probabilidad={probability}/100 | "
                f"calidad={quality}/100 | "
                "rechazo o continuidad/estructura/impulso confirmados"
            ),
            "continuity": continuity_setup,
            "blocked": False,
            "zone": (
                "soporte_rechazado"
                if rejection
                else "continuidad_alcista"
            ),
            "entry_type": (
                "rejection_support"
                if rejection
                else "high_confluence_call"
            ),
            "entry_quality": quality,
            "signal_price": price,
            "candle_open": c["open"],
            "candle_close": c["close"],
            "distance_to_zone_atr": (
                distance if rejection else None
            ),
        })

        return result

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    if not (
        PUT_RSI_MIN
        <= rsi
        <= PUT_RSI_MAX
    ):
        result["reason"] = (
            f"RSI PUT fuera de rango {rsi:.1f}"
        )
        return result

    resistance_ok, distance, zone_reason = _zone_test(
        c,
        last_high,
        atr,
        "resistance",
    )

    rejection = resistance_ok

    near_support = (
        abs(price - last_low) / max(atr, EPS)
        < MIN_DISTANCE_FROM_ZONE_ATR
    )

    if near_support:
        result["reason"] = (
            "PUT bloqueado cerca de soporte"
        )
        result["zone"] = "soporte"
        return result

    room_atr = (
        price - last_low
    ) / max(atr, EPS)

    if room_atr < (CONTINUITY_MIN_ROOM_ATR if not rejection else MIN_ROOM_TO_OPPOSITE_ATR):
        result["reason"] = (
            "Poco espacio hasta soporte"
        )
        return result

    if not rejection:
        extension_atr = abs(price - _safe_float(live.get("ema21"), price)) / max(atr, EPS)
        if extension_atr > CONTINUITY_MAX_EXTENSION_ATR:
            result["reason"] = "PUT bloqueado: continuidad demasiado extendida"
            return result

    continuity_setup = _continuity_has_setup(
        c, p, previous2, "bearish", atr
    )

    if not rejection and not continuity_setup:
        result["reason"] = (
            "PUT bloqueado: sin rechazo de resistencia ni continuidad confirmada"
        )
        return result

    if rejection and not _rejection_has_confirmation(c, p, "bearish"):
        result["reason"] = (
            "PUT bloqueado: rechazo sin confirmación de cierre"
        )
        return result

    if indecision and not rejection:
        result["reason"] = (
            "Indecisión sin rechazo confirmado"
        )
        return result

    if rejection:
        if distance > MAX_ENTRY_DISTANCE_ATR:
            result["reason"] = (
                "Rechazo demasiado alejado de resistencia"
            )
            return result

        if price >= p["close"]:
            result["reason"] = (
                "Rechazo sin recuperación bajista suficiente"
            )
            return result

    if rest and structure != "bearish":
        result["reason"] = (
            "Descanso fuera de tendencia bajista"
        )
        return result

    if force:
        if (
            not impulse["active"]
            or impulse["age"] > MAX_IMPULSE_AGE
        ):
            result["reason"] = (
                "Vela de fuerza demasiado tarde"
            )
            return result

    probability, points = _calculate_probability(
        direction="bearish",
        structure=structure,
        structure_score=s_score,
        ema_ok=ema_ok,
        candle=c,
        atr=atr,
        rsi=rsi,
        zone_distance=distance
            if rejection
            else MAX_ENTRY_DISTANCE_ATR,
        room_atr=room_atr,
        impulse=impulse,
        divergence=divergence,
        rejection=rejection,
        continuity=continuity,
        rest=rest,
        force=force,
        trendline_distance=trendline_distance,
    )

    wick_strength = (
        c["upper"] / max(c["range"], EPS)
    )

    quality = int(round(
        50.0
        + min(20.0, wick_strength * 35.0)
        + min(15.0, s_score * 3.0)
        + (8.0 if ema_ok else 0.0)
        + (7.0 if divergence["bearish"] else 0.0)
    ))

    if continuity:
        quality += 6

    if rest:
        quality += 4

    if force and impulse["phase"] == "inicio_impulso":
        quality += 5

    quality = int(_clamp(quality))

    score = int(
        round(
            (
                quality * 0.55
                + probability * 0.45
            )
        )
    )

    result["analysis"].update({
        "pattern": "PUT",
        "impulse_phase": impulse["phase"],
        "impulse_age": impulse["age"],
        "impulse_total_atr": impulse["total_atr"],
        "indecision": indecision,
        "rest": rest,
        "continuity": continuity,
        "force": force,
        "rejection": rejection,
        "rejection_confirmation": _rejection_has_confirmation(c, p, "bearish"),
        "probability_points": points,
        "trendline_distance_atr": trendline_distance,
        "room_to_opposite_atr": room_atr,
    })

    if not _passes_precision_filter(
        score,
        probability,
        quality,
    ):
        result["score"] = score
        result["probability"] = probability
        result["confidence"] = probability
        result["entry_quality"] = quality
        result["reason"] = (
            "PUT descartado por filtro de máxima precisión "
            f"| score={score}/100 "
            f"| probabilidad={probability}/100 "
            f"| calidad={quality}/100"
        )
        return result

    result.update({
        "signal": "put",
        "score": score,
        "probability": probability,
        "confidence": probability,
        "reason": (
            "PUT | ALTA CONFLUENCIA | "
            f"score={score}/100 | "
            f"probabilidad={probability}/100 | "
            f"calidad={quality}/100 | "
            "rechazo o continuidad/estructura/impulso confirmados"
        ),
        "continuity": continuity_setup,
        "blocked": False,
        "zone": (
            "resistencia_rechazada"
            if rejection
            else "continuidad_bajista"
        ),
        "entry_type": (
            "rejection_resistance"
            if rejection
            else "high_confluence_put"
        ),
        "entry_quality": quality,
        "signal_price": price,
        "candle_open": c["open"],
        "candle_close": c["close"],
        "distance_to_zone_atr": (
            distance if rejection else None
        ),
    })

    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py cargado correctamente.")
    print(
        "Filtro de entrada: rechazo confirmado de S/R + "
        f"SCORE > 90 y PROBABILIDAD >= {MIN_ENTRY_PROBABILITY}"
    )
