from __future__ import annotations

from typing import Any, Dict, Optional, List, Tuple
import pandas as pd


# ============================================================
# CONFIGURACIÓN
# ============================================================

MIN_SCORE_TO_TRADE = 75

# Para análisis M1
LOOKBACK_M1 = 20

# Para estructura M5
LOOKBACK_M5 = 20

# Tolerancia de zonas
LEVEL_TOLERANCE_RATIO = 0.0015

# Cuerpo mínimo
MIN_BODY_RATIO = 0.40

# Cuerpo considerado fuerte
STRONG_BODY_RATIO = 0.50

# Máximo de contactos de una zona
MAX_LEVEL_TOUCHES = 4


# ============================================================
# DATAFRAME
# ============================================================

def safe_dataframe(
    df: Optional[pd.DataFrame]
) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    required = {
        "open",
        "close",
        "high",
        "low",
    }

    if not required.issubset(df.columns):
        return pd.DataFrame()

    df = df.copy()

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df.dropna(
        subset=list(required),
        inplace=True
    )

    df.reset_index(
        drop=True,
        inplace=True
    )

    return df


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_range(c: Dict[str, Any]) -> float:

    return max(
        float(c["high"]) - float(c["low"]),
        0.0
    )


def candle_body(c: Dict[str, Any]) -> float:

    return abs(
        float(c["close"]) -
        float(c["open"])
    )


def body_ratio(c: Dict[str, Any]) -> float:

    total_range = candle_range(c)

    if total_range <= 0:
        return 0.0

    return candle_body(c) / total_range


def is_bullish(c: Dict[str, Any]) -> bool:

    return (
        float(c["close"]) >
        float(c["open"])
    )


def is_bearish(c: Dict[str, Any]) -> bool:

    return (
        float(c["close"]) <
        float(c["open"])
    )


def upper_wick(c: Dict[str, Any]) -> float:

    return (
        float(c["high"]) -
        max(
            float(c["open"]),
            float(c["close"])
        )
    )


def lower_wick(c: Dict[str, Any]) -> float:

    return (
        min(
            float(c["open"]),
            float(c["close"])
        ) -
        float(c["low"])
    )


def is_strong_bullish(c: Dict[str, Any]) -> bool:

    return (
        is_bullish(c)
        and body_ratio(c) >= STRONG_BODY_RATIO
    )


def is_strong_bearish(c: Dict[str, Any]) -> bool:

    return (
        is_bearish(c)
        and body_ratio(c) >= STRONG_BODY_RATIO
    )


# ============================================================
# PIVOTS
# ============================================================

def get_pivot_lows(
    candles: List[Dict[str, Any]]
) -> List[Tuple[int, float]]:

    pivots = []

    if len(candles) < 3:
        return pivots

    for i in range(
        1,
        len(candles) - 1
    ):

        previous_low = float(
            candles[i - 1]["low"]
        )

        current_low = float(
            candles[i]["low"]
        )

        next_low = float(
            candles[i + 1]["low"]
        )

        if (
            current_low <= previous_low
            and current_low <= next_low
        ):
            pivots.append(
                (i, current_low)
            )

    return pivots


def get_pivot_highs(
    candles: List[Dict[str, Any]]
) -> List[Tuple[int, float]]:

    pivots = []

    if len(candles) < 3:
        return pivots

    for i in range(
        1,
        len(candles) - 1
    ):

        previous_high = float(
            candles[i - 1]["high"]
        )

        current_high = float(
            candles[i]["high"]
        )

        next_high = float(
            candles[i + 1]["high"]
        )

        if (
            current_high >= previous_high
            and current_high >= next_high
        ):
            pivots.append(
                (i, current_high)
            )

    return pivots


# ============================================================
# TENDENCIA
# ============================================================

def detect_trend(
    candles: List[Dict[str, Any]]
) -> str:

    if len(candles) < 6:
        return "NEUTRAL"

    bullish = 0.0
    bearish = 0.0

    for i in range(
        1,
        len(candles)
    ):

        previous = candles[i - 1]
        current = candles[i]

        previous_close = float(
            previous["close"]
        )

        current_close = float(
            current["close"]
        )

        previous_high = float(
            previous["high"]
        )

        current_high = float(
            current["high"]
        )

        previous_low = float(
            previous["low"]
        )

        current_low = float(
            current["low"]
        )

        # Cierres
        if current_close > previous_close:
            bullish += 1

        elif current_close < previous_close:
            bearish += 1

        # Máximos
        if current_high > previous_high:
            bullish += 0.5

        elif current_high < previous_high:
            bearish += 0.5

        # Mínimos
        if current_low > previous_low:
            bullish += 0.5

        elif current_low < previous_low:
            bearish += 0.5

    if bullish > bearish + 2:
        return "BULLISH"

    if bearish > bullish + 2:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# ESTRUCTURA M5
# ============================================================

def analyze_m5_structure(
    candles_m5: List[Dict[str, Any]]
) -> Dict[str, Any]:

    result = {
        "direction": "NEUTRAL",
        "score": 0,
        "higher_lows": 0,
        "lower_highs": 0,
        "reason": "",
    }

    if len(candles_m5) < 10:
        return result

    trend = detect_trend(
        candles_m5
    )

    result["direction"] = trend

    pivot_lows = get_pivot_lows(
        candles_m5
    )

    pivot_highs = get_pivot_highs(
        candles_m5
    )

    # ========================================================
    # ESTRUCTURA ALCISTA
    # ========================================================

    if len(pivot_lows) >= 2:

        recent_lows = pivot_lows[-3:]

        higher_lows = 0

        for i in range(
            1,
            len(recent_lows)
        ):

            if (
                recent_lows[i][1]
                >
                recent_lows[i - 1][1]
            ):
                higher_lows += 1

        result["higher_lows"] = higher_lows

        if higher_lows >= 2:
            result["score"] += 40

        elif higher_lows == 1:
            result["score"] += 20

    # ========================================================
    # ESTRUCTURA BAJISTA
    # ========================================================

    if len(pivot_highs) >= 2:

        recent_highs = pivot_highs[-3:]

        lower_highs = 0

        for i in range(
            1,
            len(recent_highs)
        ):

            if (
                recent_highs[i][1]
                <
                recent_highs[i - 1][1]
            ):
                lower_highs += 1

        result["lower_highs"] = lower_highs

        if lower_highs >= 2:
            result["score"] += 40

        elif lower_highs == 1:
            result["score"] += 20

    # ========================================================
    # DIRECCIÓN
    # ========================================================

    if trend == "BULLISH":

        result["score"] += 30

        result["reason"] = (
            "M5 estructura alcista"
        )

    elif trend == "BEARISH":

        result["score"] += 30

        result["reason"] = (
            "M5 estructura bajista"
        )

    else:

        result["score"] = 0

        result["reason"] = (
            "M5 sin estructura clara"
        )

    result["score"] = min(
        int(result["score"]),
        100
    )

    return result


# ============================================================
# SOPORTE / RESISTENCIA
# ============================================================

def count_support_touches(
    candles: List[Dict[str, Any]],
    level: float
) -> int:

    tolerance = max(
        abs(level) *
        LEVEL_TOLERANCE_RATIO,
        0.000001
    )

    touches = 0

    for candle in candles:

        low = float(
            candle["low"]
        )

        if abs(low - level) <= tolerance:
            touches += 1

    return touches


def count_resistance_touches(
    candles: List[Dict[str, Any]],
    level: float
) -> int:

    tolerance = max(
        abs(level) *
        LEVEL_TOLERANCE_RATIO,
        0.000001
    )

    touches = 0

    for candle in candles:

        high = float(
            candle["high"]
        )

        if abs(high - level) <= tolerance:
            touches += 1

    return touches


# ============================================================
# RECHAZO
# ============================================================

def bullish_rejection(
    candle: Dict[str, Any]
) -> bool:

    body = candle_body(candle)

    wick = lower_wick(candle)

    if body <= 0:
        return wick > 0

    return (
        wick >= body * 0.5
        and is_bullish(candle)
    )


def bearish_rejection(
    candle: Dict[str, Any]
) -> bool:

    body = candle_body(candle)

    wick = upper_wick(candle)

    if body <= 0:
        return wick > 0

    return (
        wick >= body * 0.5
        and is_bearish(candle)
    )


# ============================================================
# SETUP HIGHER LOWS
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

    pivots = get_pivot_lows(
        candles
    )

    if len(pivots) < 2:
        return result

    # ========================================================
    # IMPULSO BAJISTA PREVIO
    # ========================================================

    early = candles[:5]

    bearish_impulse = 0

    for i in range(
        1,
        len(early)
    ):

        if (
            float(early[i]["close"])
            <
            float(early[i - 1]["close"])
        ):
            bearish_impulse += 1

    if bearish_impulse >= 3:
        result["score"] += 20

    # ========================================================
    # HIGHER LOWS
    # ========================================================

    last_pivots = pivots[-3:]

    higher_lows = 0

    for i in range(
        1,
        len(last_pivots)
    ):

        if (
            last_pivots[i][1]
            >
            last_pivots[i - 1][1]
        ):
            higher_lows += 1

    result["higher_lows"] = higher_lows

    if higher_lows >= 2:
        result["score"] += 30

    elif higher_lows >= 1:
        result["score"] += 15

    # ========================================================
    # REACCIÓN
    # ========================================================

    recent = candles[-5:]

    green_count = sum(
        1
        for candle in recent
        if is_bullish(candle)
    )

    result["green_count"] = green_count

    if green_count >= 3:
        result["score"] += 15

    elif green_count >= 2:
        result["score"] += 10

    # ========================================================
    # CONTINUIDAD
    # ========================================================

    last_three = candles[-3:]

    consecutive_green = 0

    for candle in reversed(last_three):

        if is_bullish(candle):
            consecutive_green += 1

        else:
            break

    if consecutive_green >= 2:
        result["score"] += 20

    # ========================================================
    # PROGRESIÓN
    # ========================================================

    if (
        float(candles[-1]["close"])
        >
        float(candles[-2]["close"])
        >
        float(candles[-3]["close"])
    ):
        result["score"] += 15

    # ========================================================
    # FUERZA
    # ========================================================

    if is_strong_bullish(
        candles[-1]
    ):
        result["score"] += 10

    # ========================================================
    # RECHAZO
    # ========================================================

    if bullish_rejection(
        candles[-1]
    ):
        result["score"] += 5

    # ========================================================
    # VALIDACIÓN
    # ========================================================

    if (
        higher_lows >= 2
        and consecutive_green >= 2
        and result["score"]
        >= MIN_SCORE_TO_TRADE
    ):

        result["valid"] = True

        result["reason"] = (
            "IMPULSO BAJISTA + REACCIÓN + "
            "2 HIGHER LOWS + CONTINUIDAD ALCISTA"
        )

    result["score"] = min(
        int(result["score"]),
        100
    )

    return result


# ============================================================
# SETUP LOWER HIGHS
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

    pivots = get_pivot_highs(
        candles
    )

    if len(pivots) < 2:
        return result

    # ========================================================
    # IMPULSO ALCISTA
    # ========================================================

    early = candles[:5]

    bullish_impulse = 0

    for i in range(
        1,
        len(early)
    ):

        if (
            float(early[i]["close"])
            >
            float(early[i - 1]["close"])
        ):
            bullish_impulse += 1

    if bullish_impulse >= 3:
        result["score"] += 20

    # ========================================================
    # LOWER HIGHS
    # ========================================================

    last_pivots = pivots[-3:]

    lower_highs = 0

    for i in range(
        1,
        len(last_pivots)
    ):

        if (
            last_pivots[i][1]
            <
            last_pivots[i - 1][1]
        ):
            lower_highs += 1

    result["lower_highs"] = lower_highs

    if lower_highs >= 2:
        result["score"] += 30

    elif lower_highs >= 1:
        result["score"] += 15

    # ========================================================
    # REACCIÓN
    # ========================================================

    recent = candles[-5:]

    red_count = sum(
        1
        for candle in recent
        if is_bearish(candle)
    )

    result["red_count"] = red_count

    if red_count >= 3:
        result["score"] += 15

    elif red_count >= 2:
        result["score"] += 10

    # ========================================================
    # CONTINUIDAD
    # ========================================================

    last_three = candles[-3:]

    consecutive_red = 0

    for candle in reversed(last_three):

        if is_bearish(candle):
            consecutive_red += 1

        else:
            break

    if consecutive_red >= 2:
        result["score"] += 20

    # ========================================================
    # PROGRESIÓN
    # ========================================================

    if (
        float(candles[-1]["close"])
        <
        float(candles[-2]["close"])
        <
        float(candles[-3]["close"])
    ):
        result["score"] += 15

    # ========================================================
    # FUERZA
    # ========================================================

    if is_strong_bearish(
        candles[-1]
    ):
        result["score"] += 10

    # ========================================================
    # RECHAZO
    # ========================================================

    if bearish_rejection(
        candles[-1]
    ):
        result["score"] += 5

    # ========================================================
    # VALIDACIÓN
    # ========================================================

    if (
        lower_highs >= 2
        and consecutive_red >= 2
        and result["score"]
        >= MIN_SCORE_TO_TRADE
    ):

        result["valid"] = True

        result["reason"] = (
            "IMPULSO ALCISTA + REACCIÓN + "
            "2 LOWER HIGHS + CONTINUIDAD BAJISTA"
        )

    result["score"] = min(
        int(result["score"]),
        100
    )

    return result


# ============================================================
# TREND + SUPPORT
# ============================================================

def detect_bullish_level_setup(
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

    recent = candles[-10:]

    pivots = get_pivot_lows(
        recent
    )

    if not pivots:
        return result

    level = pivots[-1][1]

    touches = count_support_touches(
        recent,
        level
    )

    result["touches"] = touches
    result["level_type"] = "SUPPORT"

    if not (
        1 <= touches <= MAX_LEVEL_TOUCHES
    ):
        return result

    result["score"] += 20

    last = recent[-1]
    previous = recent[-2]

    tolerance = max(
        abs(level) *
        LEVEL_TOLERANCE_RATIO,
        0.000001
    )

    near_support = (
        abs(
            float(last["low"]) - level
        )
        <= tolerance * 2
        or
        float(last["low"])
        <= level + tolerance * 2
    )

    if near_support:
        result["score"] += 20

    if bullish_rejection(last):
        result["score"] += 10

    if (
        is_bullish(last)
        and
        float(last["close"])
        >
        float(previous["close"])
    ):
        result["score"] += 25

    if is_strong_bullish(last):
        result["score"] += 15

    if (
        float(last["low"])
        <
        min(
            float(c["low"])
            for c in recent[-4:-1]
        )
    ):
        result["score"] -= 25

    result["score"] = min(
        max(int(result["score"]), 0),
        100
    )

    if (
        near_support
        and is_bullish(last)
        and result["score"]
        >= MIN_SCORE_TO_TRADE
    ):

        result["valid"] = True
        result["signal"] = "call"

        result["reason"] = (
            f"TENDENCIA ALCISTA + SOPORTE "
            f"+ TOQUE #{touches} + RECHAZO/CONTINUIDAD"
        )

    return result


# ============================================================
# TREND + RESISTANCE
# ============================================================

def detect_bearish_level_setup(
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

    recent = candles[-10:]

    pivots = get_pivot_highs(
        recent
    )

    if not pivots:
        return result

    level = pivots[-1][1]

    touches = count_resistance_touches(
        recent,
        level
    )

    result["touches"] = touches
    result["level_type"] = "RESISTANCE"

    if not (
        1 <= touches <= MAX_LEVEL_TOUCHES
    ):
        return result

    result["score"] += 20

    last = recent[-1]
    previous = recent[-2]

    tolerance = max(
        abs(level) *
        LEVEL_TOLERANCE_RATIO,
        0.000001
    )

    near_resistance = (
        abs(
            float(last["high"]) - level
        )
        <= tolerance * 2
        or
        float(last["high"])
        >= level - tolerance * 2
    )

    if near_resistance:
        result["score"] += 20

    if bearish_rejection(last):
        result["score"] += 10

    if (
        is_bearish(last)
        and
        float(last["close"])
        <
        float(previous["close"])
    ):
        result["score"] += 25

    if is_strong_bearish(last):
        result["score"] += 15

    if (
        float(last["high"])
        >
        max(
            float(c["high"])
            for c in recent[-4:-1]
        )
    ):
        result["score"] -= 25

    result["score"] = min(
        max(int(result["score"]), 0),
        100
    )

    if (
        near_resistance
        and is_bearish(last)
        and result["score"]
        >= MIN_SCORE_TO_TRADE
    ):

        result["valid"] = True
        result["signal"] = "put"

        result["reason"] = (
            f"TENDENCIA BAJISTA + RESISTENCIA "
            f"+ TOQUE #{touches} + RECHAZO/CONTINUIDAD"
        )

    return result


# ============================================================
# CONFIRMACIÓN M5
# ============================================================

def confirm_with_m5(
    signal: str,
    m5_result: Dict[str, Any]
) -> Dict[str, Any]:

    direction = m5_result.get(
        "direction",
        "NEUTRAL"
    )

    score = m5_result.get(
        "score",
        0
    )

    result = {
        "confirmed": False,
        "bonus": 0,
        "reason": "",
    }

    # CALL solamente con estructura M5 alcista
    if signal == "call":

        if direction == "BULLISH":

            result["confirmed"] = True
            result["bonus"] = 20
            result["reason"] = (
                "M5 confirma estructura alcista"
            )

        elif direction == "NEUTRAL":

            result["bonus"] = 0
            result["reason"] = (
                "M5 neutral: sin confirmación"
            )

        else:

            result["reason"] = (
                "M5 contradice CALL"
            )

    # PUT solamente con estructura M5 bajista
    elif signal == "put":

        if direction == "BEARISH":

            result["confirmed"] = True
            result["bonus"] = 20
            result["reason"] = (
                "M5 confirma estructura bajista"
            )

        elif direction == "NEUTRAL":

            result["bonus"] = 0
            result["reason"] = (
                "M5 neutral: sin confirmación"
            )

        else:

            result["reason"] = (
                "M5 contradice PUT"
            )

    return result


# ============================================================
# ANALIZAR MERCADO
# ============================================================

def analyze_market(
    candle_1m,
    previous_m1=None,
    candles_m5=None,
    pair: str = None
):

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "reason": "",
        "setup": None,
        "m5_confirmed": False,
        "m5_direction": "NEUTRAL",
    }

    # ========================================================
    # PAR OBLIGATORIO
    # ========================================================

    if not pair:
        return result

    # ========================================================
    # M1
    # ========================================================

    hist_m1 = safe_dataframe(
        previous_m1
    )

    if len(hist_m1) < 10:
        return result

    data_m1 = (
        hist_m1
        .tail(LOOKBACK_M1)
        .to_dict("records")
    )

    if len(data_m1) < 10:
        return result

    # ========================================================
    # M5
    # ========================================================

    hist_m5 = safe_dataframe(
        candles_m5
    )

    if len(hist_m5) < 10:
        return result

    data_m5 = (
        hist_m5
        .tail(LOOKBACK_M5)
        .to_dict("records")
    )

    if len(data_m5) < 10:
        return result

    # ========================================================
    # DIRECCIÓN M1
    # ========================================================

    direction_m1 = detect_trend(
        data_m1
    )

    result["direction"] = direction_m1

    # ========================================================
    # ESTRUCTURA M5
    # ========================================================

    m5_structure = analyze_m5_structure(
        data_m5
    )

    m5_direction = m5_structure[
        "direction"
    ]

    result["m5_direction"] = (
        m5_direction
    )

    # ========================================================
    # CANDIDATOS
    # ========================================================

    candidates = []

    # --------------------------------------------------------
    # HIGHER LOWS
    # --------------------------------------------------------

    bullish = detect_bullish_structure(
        data_m1
    )

    if bullish["valid"]:

        candidates.append({
            "signal": "call",
            "score": bullish["score"],
            "reason": bullish["reason"],
            "setup": "HIGHER_LOWS",
        })

    # --------------------------------------------------------
    # LOWER HIGHS
    # --------------------------------------------------------

    bearish = detect_bearish_structure(
        data_m1
    )

    if bearish["valid"]:

        candidates.append({
            "signal": "put",
            "score": bearish["score"],
            "reason": bearish["reason"],
            "setup": "LOWER_HIGHS",
        })

    # --------------------------------------------------------
    # SOPORTE
    # --------------------------------------------------------

    bullish_level = (
        detect_bullish_level_setup(
            data_m1
        )
    )

    if bullish_level["valid"]:

        candidates.append({
            "signal": "call",
            "score": bullish_level["score"],
            "reason": bullish_level["reason"],
            "setup": "SUPPORT",
        })

    # --------------------------------------------------------
    # RESISTENCIA
    # --------------------------------------------------------

    bearish_level = (
        detect_bearish_level_setup(
            data_m1
        )
    )

    if bearish_level["valid"]:

        candidates.append({
            "signal": "put",
            "score": bearish_level["score"],
            "reason": bearish_level["reason"],
            "setup": "RESISTANCE",
        })

    # ========================================================
    # NO HAY SEÑAL M1
    # ========================================================

    if not candidates:
        return result

    # ========================================================
    # MEJOR SETUP M1
    # ========================================================

    best = max(
        candidates,
        key=lambda x: x["score"]
    )

    signal = best["signal"]

    score = int(
        best["score"]
    )

    # ========================================================
    # CONFIRMACIÓN M5
    # ========================================================

    m5_confirmation = (
        confirm_with_m5(
            signal,
            m5_structure
        )
    )

    result["m5_confirmed"] = (
        m5_confirmation["confirmed"]
    )

    # ========================================================
    # M5 CONTRARIO = DESCARTAR
    # ========================================================

    if (
        signal == "call"
        and m5_direction == "BEARISH"
    ):
        return result

    if (
        signal == "put"
        and m5_direction == "BULLISH"
    ):
        return result

    # ========================================================
    # BONIFICACIÓN M5
    # ========================================================

    score += m5_confirmation[
        "bonus"
    ]

    # ========================================================
    # BONIFICACIÓN POR ALINEACIÓN M1/M5
    # ========================================================

    if (
        signal == "call"
        and direction_m1 == "BULLISH"
        and m5_direction == "BULLISH"
    ):

        score += 10

    elif (
        signal == "put"
        and direction_m1 == "BEARISH"
        and m5_direction == "BEARISH"
    ):

        score += 10

    # ========================================================
    # PENALIZAR M1 CONTRARIO
    # ========================================================

    if (
        signal == "call"
        and direction_m1 == "BEARISH"
    ):

        score -= 10

    elif (
        signal == "put"
        and direction_m1 == "BULLISH"
    ):

        score -= 10

    # ========================================================
    # SCORE FINAL
    # ========================================================

    score = min(
        max(int(score), 0),
        100
    )

    # ========================================================
    # FILTRO FINAL
    # ========================================================

    if score < MIN_SCORE_TO_TRADE:
        return result

    # ========================================================
    # RESULTADO
    # ========================================================

    result["valid"] = True

    result["signal"] = signal

    result["score"] = score

    result["setup"] = best["setup"]

    result["reason"] = (
        f'{best["reason"]} | '
        f'{m5_confirmation["reason"]}'
    )

    return result


# ============================================================
# FUNCIÓN DE COMPATIBILIDAD
# ============================================================

def get_signal(
    candle_1m,
    previous_m1=None,
    candles_m5=None,
    pair: str = None
):

    return analyze_market(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        candles_m5=candles_m5,
        pair=pair
    )
