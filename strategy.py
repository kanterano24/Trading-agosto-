from __future__ import annotations

from typing import Any, Dict, Optional, List, Tuple
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

MIN_CONTINUITY_BODY_RATIO = 0.40
MIN_SCORE_TO_TRADE = 75

LOOKBACK = 20

# Tolerancia para considerar dos precios como la misma zona.
LEVEL_TOLERANCE_RATIO = 0.0015

# Tamaño mínimo del cuerpo para considerar continuidad.
STRONG_BODY_RATIO = 0.50

# Máximo número de contactos permitidos por tu lógica.
MAX_LEVEL_TOUCHES = 4


# ============================================================
# DATA
# ============================================================

def safe_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    required = {"open", "close", "high", "low"}

    if not required.issubset(df.columns):
        return pd.DataFrame()

    df = df.copy()

    for c in required:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(subset=list(required), inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_range(c: Dict[str, Any]) -> float:
    return max(float(c["high"]) - float(c["low"]), 0.0)


def candle_body(c: Dict[str, Any]) -> float:
    return abs(float(c["close"]) - float(c["open"]))


def body_ratio(c: Dict[str, Any]) -> float:
    r = candle_range(c)

    if r <= 0:
        return 0.0

    return candle_body(c) / r


def is_bullish(c: Dict[str, Any]) -> bool:
    return float(c["close"]) > float(c["open"])


def is_bearish(c: Dict[str, Any]) -> bool:
    return float(c["close"]) < float(c["open"])


def upper_wick(c: Dict[str, Any]) -> float:
    return float(c["high"]) - max(float(c["open"]), float(c["close"]))


def lower_wick(c: Dict[str, Any]) -> float:
    return min(float(c["open"]), float(c["close"])) - float(c["low"])


def is_strong_bullish(c: Dict[str, Any]) -> bool:
    return (
        is_bullish(c)
        and body_ratio(c) >= MIN_CONTINUITY_BODY_RATIO
    )


def is_strong_bearish(c: Dict[str, Any]) -> bool:
    return (
        is_bearish(c)
        and body_ratio(c) >= MIN_CONTINUITY_BODY_RATIO
    )


# ============================================================
# TREND
# ============================================================

def detect_trend(candles: List[Dict[str, Any]]) -> str:
    """
    Detecta tendencia usando:
    - progresión de cierres
    - estructura de máximos
    - estructura de mínimos
    """

    if len(candles) < 6:
        return "NEUTRAL"

    bullish = 0
    bearish = 0

    for i in range(1, len(candles)):
        prev = candles[i - 1]
        curr = candles[i]

        # Progresión del cierre
        if float(curr["close"]) > float(prev["close"]):
            bullish += 1
        elif float(curr["close"]) < float(prev["close"]):
            bearish += 1

        # Máximo creciente / decreciente
        if float(curr["high"]) > float(prev["high"]):
            bullish += 0.5
        elif float(curr["high"]) < float(prev["high"]):
            bearish += 0.5

        # Mínimo creciente / decreciente
        if float(curr["low"]) > float(prev["low"]):
            bullish += 0.5
        elif float(curr["low"]) < float(prev["low"]):
            bearish += 0.5

    if bullish > bearish + 2:
        return "BULLISH"

    if bearish > bullish + 2:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# PIVOTS
# ============================================================

def get_pivot_lows(
    candles: List[Dict[str, Any]]
) -> List[Tuple[int, float]]:
    pivots = []

    if len(candles) < 3:
        return pivots

    for i in range(1, len(candles) - 1):
        prev_low = float(candles[i - 1]["low"])
        curr_low = float(candles[i]["low"])
        next_low = float(candles[i + 1]["low"])

        if curr_low <= prev_low and curr_low <= next_low:
            pivots.append((i, curr_low))

    return pivots


def get_pivot_highs(
    candles: List[Dict[str, Any]]
) -> List[Tuple[int, float]]:
    pivots = []

    if len(candles) < 3:
        return pivots

    for i in range(1, len(candles) - 1):
        prev_high = float(candles[i - 1]["high"])
        curr_high = float(candles[i]["high"])
        next_high = float(candles[i + 1]["high"])

        if curr_high >= prev_high and curr_high >= next_high:
            pivots.append((i, curr_high))

    return pivots


# ============================================================
# POINT #1
# IMPULSO BAJISTA -> REACCIÓN -> 2 HIGHER LOWS -> CALL
# ============================================================

def detect_bullish_structure(
    candles: List[Dict[str, Any]]
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "score": 0,
        "reason": "",
        "higher_lows": 0,
        "green_count": 0,
    }

    if len(candles) < 10:
        return result

    pivots = get_pivot_lows(candles)

    if len(pivots) < 2:
        return result

    # ========================================================
    # 1. IMPULSO BAJISTA PREVIO
    # ========================================================

    early = candles[:5]

    bearish_impulse = 0

    for i in range(1, len(early)):
        if (
            float(early[i]["close"])
            < float(early[i - 1]["close"])
        ):
            bearish_impulse += 1

    if bearish_impulse >= 3:
        result["score"] += 20

    # ========================================================
    # 2. DOS RETROCESOS CON MÍNIMOS MÁS ALTOS
    # ========================================================

    last_pivots = pivots[-3:]

    higher_lows = 0

    for i in range(1, len(last_pivots)):
        if last_pivots[i][1] > last_pivots[i - 1][1]:
            higher_lows += 1

    result["higher_lows"] = higher_lows

    if higher_lows >= 2:
        result["score"] += 30
    elif higher_lows >= 1:
        result["score"] += 15

    # ========================================================
    # 3. REACCIÓN ALCISTA
    # ========================================================

    recent = candles[-5:]

    green_count = sum(
        1 for c in recent
        if is_bullish(c)
    )

    result["green_count"] = green_count

    if green_count >= 3:
        result["score"] += 15
    elif green_count >= 2:
        result["score"] += 10

    # ========================================================
    # 4. CONTINUIDAD
    # Segunda o tercera vela verde
    # ========================================================

    last_three = candles[-3:]

    consecutive_green = 0

    for c in reversed(last_three):
        if is_bullish(c):
            consecutive_green += 1
        else:
            break

    if consecutive_green == 2:
        result["score"] += 20

    elif consecutive_green >= 3:
        result["score"] += 20

    # ========================================================
    # 5. CONFIRMACIÓN DE PROGRESIÓN
    # ========================================================

    if (
        float(candles[-1]["close"])
        > float(candles[-2]["close"])
        > float(candles[-3]["close"])
    ):
        result["score"] += 15

    # ========================================================
    # 6. ÚLTIMA VELA CON CUERPO SUFICIENTE
    # ========================================================

    if is_strong_bullish(candles[-1]):
        result["score"] += 10

    # ========================================================
    # VALIDACIÓN FINAL
    # ========================================================

    if (
        higher_lows >= 2
        and consecutive_green >= 2
        and result["score"] >= 75
    ):
        result["valid"] = True
        result["reason"] = (
            "IMPULSO BAJISTA + REACCIÓN + "
            "2 MÍNIMOS CRECIENTES + CONTINUIDAD ALCISTA"
        )

    result["score"] = min(int(result["score"]), 100)

    return result


# ============================================================
# POINT #2
# IMPULSO ALCISTA -> REACCIÓN -> 2 LOWER HIGHS -> PUT
# ============================================================

def detect_bearish_structure(
    candles: List[Dict[str, Any]]
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "score": 0,
        "reason": "",
        "lower_highs": 0,
        "red_count": 0,
    }

    if len(candles) < 10:
        return result

    pivots = get_pivot_highs(candles)

    if len(pivots) < 2:
        return result

    # ========================================================
    # 1. IMPULSO ALCISTA PREVIO
    # ========================================================

    early = candles[:5]

    bullish_impulse = 0

    for i in range(1, len(early)):
        if (
            float(early[i]["close"])
            > float(early[i - 1]["close"])
        ):
            bullish_impulse += 1

    if bullish_impulse >= 3:
        result["score"] += 20

    # ========================================================
    # 2. DOS RETROCESOS CON MÁXIMOS MÁS BAJOS
    # ========================================================

    last_pivots = pivots[-3:]

    lower_highs = 0

    for i in range(1, len(last_pivots)):
        if last_pivots[i][1] < last_pivots[i - 1][1]:
            lower_highs += 1

    result["lower_highs"] = lower_highs

    if lower_highs >= 2:
        result["score"] += 30
    elif lower_highs >= 1:
        result["score"] += 15

    # ========================================================
    # 3. REACCIÓN BAJISTA
    # ========================================================

    recent = candles[-5:]

    red_count = sum(
        1 for c in recent
        if is_bearish(c)
    )

    result["red_count"] = red_count

    if red_count >= 3:
        result["score"] += 15
    elif red_count >= 2:
        result["score"] += 10

    # ========================================================
    # 4. CONTINUIDAD
    # Segunda o tercera vela roja
    # ========================================================

    last_three = candles[-3:]

    consecutive_red = 0

    for c in reversed(last_three):
        if is_bearish(c):
            consecutive_red += 1
        else:
            break

    if consecutive_red == 2:
        result["score"] += 20

    elif consecutive_red >= 3:
        result["score"] += 20

    # ========================================================
    # 5. PROGRESIÓN
    # ========================================================

    if (
        float(candles[-1]["close"])
        < float(candles[-2]["close"])
        < float(candles[-3]["close"])
    ):
        result["score"] += 15

    # ========================================================
    # 6. FUERZA DE LA ÚLTIMA VELA
    # ========================================================

    if is_strong_bearish(candles[-1]):
        result["score"] += 10

    # ========================================================
    # VALIDACIÓN FINAL
    # ========================================================

    if (
        lower_highs >= 2
        and consecutive_red >= 2
        and result["score"] >= 75
    ):
        result["valid"] = True
        result["reason"] = (
            "IMPULSO ALCISTA + REACCIÓN + "
            "2 MÁXIMOS DECRECIENTES + CONTINUIDAD BAJISTA"
        )

    result["score"] = min(int(result["score"]), 100)

    return result


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def count_support_touches(
    candles: List[Dict[str, Any]],
    level: float
) -> int:

    tolerance = max(abs(level) * LEVEL_TOLERANCE_RATIO, 0.000001)

    touches = 0

    for c in candles:
        if abs(float(c["low"]) - level) <= tolerance:
            touches += 1

    return touches


def count_resistance_touches(
    candles: List[Dict[str, Any]],
    level: float
) -> int:

    tolerance = max(abs(level) * LEVEL_TOLERANCE_RATIO, 0.000001)

    touches = 0

    for c in candles:
        if abs(float(c["high"]) - level) <= tolerance:
            touches += 1

    return touches


# ============================================================
# POINT #3
# TENDENCIA + ZONA + TOQUE 1 A 4 + CONTINUIDAD
# ============================================================

def detect_trend_level_setup(
    candles: List[Dict[str, Any]]
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "reason": "",
        "touches": 0,
        "level_type": None,
    }

    if len(candles) < 10:
        return result

    trend = detect_trend(candles)

    if trend == "NEUTRAL":
        return result

    recent = candles[-10:]

    # ========================================================
    # TENDENCIA ALCISTA -> SOPORTE
    # ========================================================

    if trend == "BULLISH":

        pivot_lows = get_pivot_lows(recent)

        if not pivot_lows:
            return result

        level = pivot_lows[-1][1]

        touches = count_support_touches(recent, level)

        result["touches"] = touches
        result["level_type"] = "SUPPORT"

        if 1 <= touches <= MAX_LEVEL_TOUCHES:
            result["score"] += 20

        last = recent[-1]
        prev = recent[-2]

        # El precio llega cerca del soporte
        tolerance = max(
            abs(level) * LEVEL_TOLERANCE_RATIO,
            0.000001
        )

        near_support = (
            abs(float(last["low"]) - level) <= tolerance * 2
            or float(last["low"]) <= level + tolerance * 2
        )

        if near_support:
            result["score"] += 20

        # Rechazo alcista
        if lower_wick(last) > candle_body(last) * 0.5:
            result["score"] += 10

        # Continuidad
        if (
            is_bullish(last)
            and float(last["close"]) > float(prev["close"])
        ):
            result["score"] += 25

        if is_strong_bullish(last):
            result["score"] += 15

        # Evitar cambio estructural bajista
        if float(last["low"]) < min(
            float(c["low"])
            for c in recent[-4:-1]
        ):
            result["score"] -= 25

        if (
            1 <= touches <= MAX_LEVEL_TOUCHES
            and near_support
            and is_bullish(last)
            and result["score"] >= 75
        ):
            result["valid"] = True
            result["signal"] = "call"
            result["reason"] = (
                f"TENDENCIA ALCISTA + SOPORTE "
                f"+ TOQUE #{touches} + CONTINUIDAD"
            )

    # ========================================================
    # TENDENCIA BAJISTA -> RESISTENCIA
    # ========================================================

    elif trend == "BEARISH":

        pivot_highs = get_pivot_highs(recent)

        if not pivot_highs:
            return result

        level = pivot_highs[-1][1]

        touches = count_resistance_touches(recent, level)

        result["touches"] = touches
        result["level_type"] = "RESISTANCE"

        if 1 <= touches <= MAX_LEVEL_TOUCHES:
            result["score"] += 20

        last = recent[-1]
        prev = recent[-2]

        tolerance = max(
            abs(level) * LEVEL_TOLERANCE_RATIO,
            0.000001
        )

        near_resistance = (
            abs(float(last["high"]) - level) <= tolerance * 2
            or float(last["high"]) >= level - tolerance * 2
        )

        if near_resistance:
            result["score"] += 20

        # Rechazo bajista
        if upper_wick(last) > candle_body(last) * 0.5:
            result["score"] += 10

        # Continuidad
        if (
            is_bearish(last)
            and float(last["close"]) < float(prev["close"])
        ):
            result["score"] += 25

        if is_strong_bearish(last):
            result["score"] += 15

        # Evitar cambio estructural alcista
        if float(last["high"]) > max(
            float(c["high"])
            for c in recent[-4:-1]
        ):
            result["score"] -= 25

        if (
            1 <= touches <= MAX_LEVEL_TOUCHES
            and near_resistance
            and is_bearish(last)
            and result["score"] >= 75
        ):
            result["valid"] = True
            result["signal"] = "put"
            result["reason"] = (
                f"TENDENCIA BAJISTA + RESISTENCIA "
                f"+ TOQUE #{touches} + CONTINUIDAD"
            )

    result["score"] = min(max(int(result["score"]), 0), 100)

    return result


# ============================================================
# MAIN STRATEGY
# ============================================================

def analyze_market(candle_1m, previous_m1=None, pair: str = None):

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
    }

    # Mantiene la misma condición de compatibilidad
    # que tu archivo actual.
    if not pair:
        return result

    hist = safe_dataframe(previous_m1)

    if len(hist) < 10:
        return result

    # Usamos las últimas velas disponibles para el análisis.
    data = hist.tail(LOOKBACK).to_dict("records")

    if len(data) < 10:
        return result

    # ========================================================
    # DIRECCIÓN GENERAL
    # ========================================================

    direction = detect_trend(data)

    result["direction"] = direction

    candidates = []

    # ========================================================
    # SETUP #1 - CALL
    # ========================================================

    bullish = detect_bullish_structure(data)

    if bullish["valid"]:
        candidates.append({
            "valid": True,
            "signal": "call",
            "score": bullish["score"],
            "reason": bullish["reason"],
            "setup": "HIGHER_LOWS",
        })

    # ========================================================
    # SETUP #2 - PUT
    # ========================================================

    bearish = detect_bearish_structure(data)

    if bearish["valid"]:
        candidates.append({
            "valid": True,
            "signal": "put",
            "score": bearish["score"],
            "reason": bearish["reason"],
            "setup": "LOWER_HIGHS",
        })

    # ========================================================
    # SETUP #3 - TENDENCIA + NIVEL
    # ========================================================

    trend_setup = detect_trend_level_setup(data)

    if trend_setup["valid"]:
        candidates.append({
            "valid": True,
            "signal": trend_setup["signal"],
            "score": trend_setup["score"],
            "reason": trend_setup["reason"],
            "setup": "TREND_LEVEL",
        })

    # ========================================================
    # ELEGIR SOLO LA MEJOR SEÑAL
    # ========================================================

    if not candidates:
        return result

    best = max(candidates, key=lambda x: x["score"])

    result["valid"] = True
    result["signal"] = best["signal"]
    result["score"] = min(int(best["score"]), 100)

    # Campos adicionales.
    # No rompen bot.py porque son datos extra.
    result["reason"] = best["reason"]
    result["setup"] = best["setup"]

    return result
