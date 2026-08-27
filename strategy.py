from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

MIN_CANDLES = 22

MIN_SCORE_TO_TRADE = 75

MIN_DIRECTION_ADVANTAGE = 10

MIN_BODY_RATIO = 0.30

MAX_DOJI_RATIO = 0.20

STRUCTURE_LOOKBACK = 10

LEVEL_LOOKBACK = 10

CONSOLIDATION_LOOKBACK = 8
CONSOLIDATION_MAX_RATIO = 0.55

REVERSAL_WICK_RATIO = 1.20

REJECTION_WICK_RATIO = 1.00

PULLBACK_LOOKBACK = 7

VOLATILITY_LOOKBACK = 10

EXHAUSTION_LOOKBACK = 4


# ============================================================
# PESOS
# ============================================================

WEIGHT_STRUCTURE = 15
WEIGHT_CANDLE = 20
WEIGHT_REJECTION = 15
WEIGHT_MOMENTUM = 10
WEIGHT_LEVEL = 15
WEIGHT_RECOVERY = 15
WEIGHT_VOLATILITY = 10


# ============================================================
# DATAFRAME SEGURO
# ============================================================

def safe_dataframe(
    df: Optional[pd.DataFrame],
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

    data = df.copy()

    for column in required:
        data[column] = pd.to_numeric(
            data[column],
            errors="coerce",
        )

    data.dropna(
        subset=list(required),
        inplace=True,
    )

    data.reset_index(
        drop=True,
        inplace=True,
    )

    return data


# ============================================================
# INFORMACIÓN DE UNA VELA
# ============================================================

def candle_info(
    candle: pd.Series,
) -> Dict[str, Any]:

    open_price = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])

    total_range = high - low

    body = abs(close - open_price)

    upper_wick = (
        high
        - max(open_price, close)
    )

    lower_wick = (
        min(open_price, close)
        - low
    )

    if total_range <= 0:

        return {
            "open": open_price,
            "close": close,
            "high": high,
            "low": low,
            "bull": False,
            "bear": False,
            "neutral": True,
            "range": 0.0,
            "body": 0.0,
            "body_ratio": 0.0,
            "upper_wick": 0.0,
            "lower_wick": 0.0,
            "upper_wick_ratio": 0.0,
            "lower_wick_ratio": 0.0,
            "close_position": 0.50,
        }

    body_ratio = body / total_range

    if body > 0:

        upper_wick_ratio = (
            upper_wick / body
        )

        lower_wick_ratio = (
            lower_wick / body
        )

    else:

        upper_wick_ratio = 0.0
        lower_wick_ratio = 0.0

    close_position = (
        close - low
    ) / total_range

    return {
        "open": open_price,
        "close": close,
        "high": high,
        "low": low,
        "bull": close > open_price,
        "bear": close < open_price,
        "neutral": close == open_price,
        "range": total_range,
        "body": body,
        "body_ratio": body_ratio,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "upper_wick_ratio": upper_wick_ratio,
        "lower_wick_ratio": lower_wick_ratio,
        "close_position": close_position,
    }


# ============================================================
# INFORMACIÓN DE VARIAS VELAS
# ============================================================

def get_candle_infos(
    df: pd.DataFrame,
) -> List[Dict[str, Any]]:

    result = []

    for _, candle in df.iterrows():

        result.append(
            candle_info(candle)
        )

    return result


# ============================================================
# ESTRUCTURA DEL MERCADO
# ============================================================

def market_structure(
    df: pd.DataFrame,
) -> str:

    if len(df) < STRUCTURE_LOOKBACK:
        return "NEUTRAL"

    data = df.tail(
        STRUCTURE_LOOKBACK
    )

    highs = data["high"].tolist()
    lows = data["low"].tolist()
    closes = data["close"].tolist()

    higher_highs = 0
    higher_lows = 0

    lower_highs = 0
    lower_lows = 0

    rising_closes = 0
    falling_closes = 0

    for i in range(
        1,
        len(data),
    ):

        if highs[i] > highs[i - 1]:
            higher_highs += 1

        if lows[i] > lows[i - 1]:
            higher_lows += 1

        if highs[i] < highs[i - 1]:
            lower_highs += 1

        if lows[i] < lows[i - 1]:
            lower_lows += 1

        if closes[i] > closes[i - 1]:
            rising_closes += 1

        elif closes[i] < closes[i - 1]:
            falling_closes += 1

    bullish_score = (
        higher_highs
        + higher_lows
        + rising_closes
    )

    bearish_score = (
        lower_highs
        + lower_lows
        + falling_closes
    )

    if (
        bullish_score >= 12
        and bullish_score > bearish_score
    ):
        return "BULLISH"

    if (
        bearish_score >= 12
        and bearish_score > bullish_score
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# FUERZA DIRECCIONAL
# ============================================================

def directional_candle_score(
    candles: pd.DataFrame,
    direction: str,
) -> int:

    score = 0

    for _, candle in candles.iterrows():

        info = candle_info(candle)

        if info["body_ratio"] < MIN_BODY_RATIO:
            continue

        if (
            direction == "BULLISH"
            and info["bull"]
        ):
            score += 1

        elif (
            direction == "BEARISH"
            and info["bear"]
        ):
            score += 1

    return score


# ============================================================
# MOMENTUM
# ============================================================

def momentum_score(
    candles: pd.DataFrame,
    direction: str,
) -> int:

    if len(candles) < 2:
        return 0

    score = 0

    closes = candles["close"].tolist()

    for i in range(
        1,
        len(candles),
    ):

        candle = candles.iloc[i]

        info = candle_info(candle)

        if info["body_ratio"] < 0.40:
            continue

        if direction == "BULLISH":

            if (
                info["bull"]
                and closes[i] > closes[i - 1]
            ):
                score += 1

        elif direction == "BEARISH":

            if (
                info["bear"]
                and closes[i] < closes[i - 1]
            ):
                score += 1

    return score


# ============================================================
# NIVELES RECIENTES
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = LEVEL_LOOKBACK,
) -> Dict[str, float]:

    if len(df) < 2:

        return {
            "support": 0.0,
            "resistance": 0.0,
            "range": 0.0,
        }

    data = df.iloc[:-1].tail(
        lookback
    )

    if data.empty:

        return {
            "support": 0.0,
            "resistance": 0.0,
            "range": 0.0,
        }

    support = float(
        data["low"].min()
    )

    resistance = float(
        data["high"].max()
    )

    total_range = (
        resistance - support
    )

    return {
        "support": support,
        "resistance": resistance,
        "range": total_range,
    }


# ============================================================
# PROXIMIDAD A NIVEL
# ============================================================

def is_near(
    price: float,
    level: float,
    market_range: float,
    tolerance_ratio: float = 0.15,
) -> bool:

    if market_range <= 0:
        return False

    tolerance = (
        market_range
        * tolerance_ratio
    )

    return (
        abs(price - level)
        <= tolerance
    )


# ============================================================
# ANÁLISIS DE LA VELA FINAL
# ============================================================

def analyze_final_candle(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "bullish_score": 0,
        "bearish_score": 0,
        "bullish_reason": [],
        "bearish_reason": [],
    }

    if df.empty:
        return result

    info = candle_info(
        df.iloc[-1]
    )

    if info["body_ratio"] <= MAX_DOJI_RATIO:
        return result

    if info["body_ratio"] >= 0.30:

        if info["bull"]:

            result["bullish_score"] += 5

            result["bullish_reason"].append(
                "cuerpo alcista válido"
            )

        elif info["bear"]:

            result["bearish_score"] += 5

            result["bearish_reason"].append(
                "cuerpo bajista válido"
            )

    if info["body_ratio"] >= 0.50:

        if info["bull"]:

            result["bullish_score"] += 5

            result["bullish_reason"].append(
                "cuerpo alcista fuerte"
            )

        elif info["bear"]:

            result["bearish_score"] += 5

            result["bearish_reason"].append(
                "cuerpo bajista fuerte"
            )

    if info["bull"]:

        if info["close_position"] >= 0.70:

            result["bullish_score"] += 5

            result["bullish_reason"].append(
                "cierre en zona superior"
            )

    elif info["bear"]:

        if info["close_position"] <= 0.30:

            result["bearish_score"] += 5

            result["bearish_reason"].append(
                "cierre en zona inferior"
            )

    if (
        info["lower_wick_ratio"]
        >= REVERSAL_WICK_RATIO
    ):

        result["bullish_score"] += 5

        result["bullish_reason"].append(
            "rechazo de precios bajos"
        )

    if (
        info["upper_wick_ratio"]
        >= REVERSAL_WICK_RATIO
    ):

        result["bearish_score"] += 5

        result["bearish_reason"].append(
            "rechazo de precios altos"
        )

    return result


# ============================================================
# ANÁLISIS DE NIVELES
# ============================================================

def analyze_levels(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "bullish_score": 0,
        "bearish_score": 0,
        "bullish_reason": [],
        "bearish_reason": [],
        "support": 0.0,
        "resistance": 0.0,
    }

    if len(df) < 5:
        return result

    levels = recent_levels(
        df,
        LEVEL_LOOKBACK,
    )

    support = levels["support"]
    resistance = levels["resistance"]
    market_range = levels["range"]

    result["support"] = support
    result["resistance"] = resistance

    if market_range <= 0:
        return result

    last = df.iloc[-1]

    info = candle_info(last)

    if is_near(
        info["low"],
        support,
        market_range,
        0.15,
    ):

        result["bullish_score"] += 8

        result["bullish_reason"].append(
            "precio reaccionando en soporte"
        )

    if (
        is_near(
            info["close"],
            support,
            market_range,
            0.15,
        )
        and info["bull"]
    ):

        result["bullish_score"] += 7

        result["bullish_reason"].append(
            "cierre alcista cerca de soporte"
        )

    if is_near(
        info["high"],
        resistance,
        market_range,
        0.15,
    ):

        result["bearish_score"] += 8

        result["bearish_reason"].append(
            "precio reaccionando en resistencia"
        )

    if (
        is_near(
            info["close"],
            resistance,
            market_range,
            0.15,
        )
        and info["bear"]
    ):

        result["bearish_score"] += 7

        result["bearish_reason"].append(
            "cierre bajista cerca de resistencia"
        )

    return result


# ============================================================
# RECUPERACIÓN / REVERSIÓN
# ============================================================

def detect_recovery(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "bullish_score": 0,
        "bearish_score": 0,
        "reason": "",
        "bullish_reason": [],
        "bearish_reason": [],
    }

    if len(df) < 8:
        return result

    previous = df.iloc[:-1].tail(8)

    last = df.iloc[-1]

    infos = get_candle_infos(
        previous
    )

    bullish_pressure = 0
    bearish_pressure = 0

    bullish_movement = 0.0
    bearish_movement = 0.0

    for i in range(
        1,
        len(infos),
    ):

        current = infos[i]

        previous_info = infos[i - 1]

        movement = (
            current["close"]
            - previous_info["close"]
        )

        if movement > 0:

            bullish_movement += movement

            if current["body_ratio"] >= 0.20:
                bullish_pressure += 1

        elif movement < 0:

            bearish_movement += abs(
                movement
            )

            if current["body_ratio"] >= 0.20:
                bearish_pressure += 1

    previous_high = float(
        previous["high"].max()
    )

    previous_low = float(
        previous["low"].min()
    )

    previous_range = (
        previous_high
        - previous_low
    )

    if previous_range <= 0:
        return result

    last_info = candle_info(last)

    last_close = last_info["close"]

    # ========================================================
    # RECUPERACIÓN ALCISTA
    # ========================================================

    bullish_score = 0

    if bearish_pressure > bullish_pressure:

        bullish_score += 20

        result["bullish_reason"].append(
            "presión bajista previa"
        )

    if bearish_movement > bullish_movement:

        bullish_score += 15

        result["bullish_reason"].append(
            "desplazamiento bajista previo"
        )

    recovery_up = (
        last_close
        - previous_low
    ) / previous_range

    if recovery_up >= 0.25:
        bullish_score += 5

    if recovery_up >= 0.40:
        bullish_score += 5

    if recovery_up >= 0.55:

        bullish_score += 5

        result["bullish_reason"].append(
            "recuperación profunda desde mínimos"
        )

    if last_info["bull"]:

        bullish_score += 10

        result["bullish_reason"].append(
            "última vela alcista"
        )

    if last_info["body_ratio"] >= 0.30:
        bullish_score += 5

    if last_info["body_ratio"] >= 0.50:

        bullish_score += 5

        result["bullish_reason"].append(
            "cuerpo de recuperación fuerte"
        )

    if (
        last_info["lower_wick"]
        > last_info["upper_wick"]
    ):

        bullish_score += 5

        result["bullish_reason"].append(
            "rechazo de mínimos"
        )

    if last_info["close_position"] >= 0.65:
        bullish_score += 5

    # ========================================================
    # RECUPERACIÓN BAJISTA
    # ========================================================

    bearish_score = 0

    if bullish_pressure > bearish_pressure:

        bearish_score += 20

        result["bearish_reason"].append(
            "presión alcista previa"
        )

    if bullish_movement > bearish_movement:

        bearish_score += 15

        result["bearish_reason"].append(
            "desplazamiento alcista previo"
        )

    recovery_down = (
        previous_high
        - last_close
    ) / previous_range

    if recovery_down >= 0.25:
        bearish_score += 5

    if recovery_down >= 0.40:
        bearish_score += 5

    if recovery_down >= 0.55:

        bearish_score += 5

        result["bearish_reason"].append(
            "recuperación profunda desde máximos"
        )

    if last_info["bear"]:

        bearish_score += 10

        result["bearish_reason"].append(
            "última vela bajista"
        )

    if last_info["body_ratio"] >= 0.30:
        bearish_score += 5

    if last_info["body_ratio"] >= 0.50:

        bearish_score += 5

        result["bearish_reason"].append(
            "cuerpo de recuperación fuerte"
        )

    if (
        last_info["upper_wick"]
        > last_info["lower_wick"]
    ):

        bearish_score += 5

        result["bearish_reason"].append(
            "rechazo de máximos"
        )

    if last_info["close_position"] <= 0.35:
        bearish_score += 5

    result["bullish_score"] = min(
        bullish_score,
        100,
    )

    result["bearish_score"] = min(
        bearish_score,
        100,
    )

    if (
        bullish_score >= 50
        and bullish_score > bearish_score
    ):

        result["detected"] = True
        result["signal"] = "call"
        result["score"] = min(
            bullish_score,
            100,
        )
        result["reason"] = (
            "Recuperación alcista"
        )

        return result

    if (
        bearish_score >= 50
        and bearish_score > bullish_score
    ):

        result["detected"] = True
        result["signal"] = "put"
        result["score"] = min(
            bearish_score,
            100,
        )
        result["reason"] = (
            "Recuperación bajista"
        )

    return result


# ============================================================
# VOLATILIDAD
# ============================================================

def volatility_analysis(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "bullish_score": 0,
        "bearish_score": 0,
        "score": 0,
        "range_ratio": 0.0,
        "reason": "",
    }

    if len(df) < VOLATILITY_LOOKBACK + 1:
        return result

    ranges = (
        df["high"]
        - df["low"]
    )

    previous = ranges.iloc[
        -VOLATILITY_LOOKBACK - 1:-1
    ]

    last_range = float(
        ranges.iloc[-1]
    )

    average_range = float(
        previous.mean()
    )

    if average_range <= 0:
        return result

    ratio = (
        last_range
        / average_range
    )

    result["range_ratio"] = ratio

    if ratio < 0.50:

        result["score"] = 0

        result["reason"] = (
            "volatilidad demasiado baja"
        )

        return result

    if 0.70 <= ratio <= 1.80:

        result["score"] = 10

        result["reason"] = (
            "volatilidad saludable"
        )

    elif ratio > 2.50:

        result["score"] = 3

        result["reason"] = (
            "volatilidad elevada"
        )

    else:

        result["score"] = 7

        result["reason"] = (
            "volatilidad aceptable"
        )

    return result


# ============================================================
# AGOTAMIENTO
# ============================================================

def exhaustion_check(
    df: pd.DataFrame,
    direction: str,
) -> bool:

    if len(df) < EXHAUSTION_LOOKBACK:
        return False

    last_four = df.tail(
        EXHAUSTION_LOOKBACK
    )

    same_direction = 0

    for _, candle in last_four.iterrows():

        info = candle_info(candle)

        if (
            direction == "BULLISH"
            and info["bull"]
            and info["body_ratio"] >= 0.60
        ):

            same_direction += 1

        elif (
            direction == "BEARISH"
            and info["bear"]
            and info["body_ratio"] >= 0.60
        ):

            same_direction += 1

    return same_direction >= 4


# ============================================================
# CONTEXTO ESTRUCTURAL
# ============================================================

def structure_confluence(
    df: pd.DataFrame,
    direction: str,
) -> Dict[str, Any]:

    result = {
        "score": 0,
        "reason": "",
    }

    structure = market_structure(df)

    if direction == "BULLISH":

        if structure == "BEARISH":

            result["score"] = 15

            result["reason"] = (
                "estructura bajista previa "
                "compatible con reversión alcista"
            )

        elif structure == "NEUTRAL":

            result["score"] = 8

            result["reason"] = (
                "estructura neutral"
            )

        elif structure == "BULLISH":

            result["score"] = 5

            result["reason"] = (
                "estructura alcista"
            )

    elif direction == "BEARISH":

        if structure == "BULLISH":

            result["score"] = 15

            result["reason"] = (
                "estructura alcista previa "
                "compatible con reversión bajista"
            )

        elif structure == "NEUTRAL":

            result["score"] = 8

            result["reason"] = (
                "estructura neutral"
            )

        elif structure == "BEARISH":

            result["score"] = 5

            result["reason"] = (
                "estructura bajista"
            )

    return result


# ============================================================
# MOMENTUM DE RECUPERACIÓN
# ============================================================

def recovery_momentum(
    df: pd.DataFrame,
    direction: str,
) -> Dict[str, Any]:

    result = {
        "score": 0,
        "reason": "",
    }

    if len(df) < 5:
        return result

    candles = df.tail(5)

    score = momentum_score(
        candles,
        direction,
    )

    if score >= 3:

        result["score"] = 10

        result["reason"] = (
            "momentum confirmado"
        )

    elif score >= 2:

        result["score"] = 7

        result["reason"] = (
            "momentum aceptable"
        )

    elif score >= 1:

        result["score"] = 3

        result["reason"] = (
            "momentum débil"
        )

    return result


# ============================================================
# PATRÓN DE RECHAZO
# ============================================================

def rejection_confluence(
    df: pd.DataFrame,
    direction: str,
) -> Dict[str, Any]:

    result = {
        "score": 0,
        "reason": "",
    }

    if df.empty:
        return result

    info = candle_info(
        df.iloc[-1]
    )

    if direction == "BULLISH":

        if info["lower_wick_ratio"] >= 2.0:

            result["score"] = 15

            result["reason"] = (
                "rechazo alcista fuerte"
            )

        elif (
            info["lower_wick_ratio"]
            >= REJECTION_WICK_RATIO
        ):

            result["score"] = 10

            result["reason"] = (
                "rechazo alcista"
            )

    elif direction == "BEARISH":

        if info["upper_wick_ratio"] >= 2.0:

            result["score"] = 15

            result["reason"] = (
                "rechazo bajista fuerte"
            )

        elif (
            info["upper_wick_ratio"]
            >= REJECTION_WICK_RATIO
        ):

            result["score"] = 10

            result["reason"] = (
                "rechazo bajista"
            )

    return result


# ============================================================
# EVALUAR DIRECCIÓN
# ============================================================

def evaluate_direction(
    df: pd.DataFrame,
    direction: str,
    recovery_score: int,
) -> Dict[str, Any]:

    structure_result = structure_confluence(
        df,
        direction,
    )

    candle_result = analyze_final_candle(
        df
    )

    level_result = analyze_levels(
        df
    )

    momentum_result = recovery_momentum(
        df,
        direction,
    )

    rejection_result = rejection_confluence(
        df,
        direction,
    )

    volatility_result = volatility_analysis(
        df
    )

    score = 0

    reasons = []

    recovery_component = min(
        recovery_score,
        WEIGHT_RECOVERY,
    )

    score += recovery_component

    if recovery_component >= 10:
        reasons.append(
            "recuperación confirmada"
        )

    structure_component = min(
        structure_result["score"],
        WEIGHT_STRUCTURE,
    )

    score += structure_component

    if structure_result["reason"]:
        reasons.append(
            structure_result["reason"]
        )

    if direction == "BULLISH":

        candle_component = min(
            candle_result["bullish_score"],
            WEIGHT_CANDLE,
        )

        candle_reasons = (
            candle_result[
                "bullish_reason"
            ]
        )

    else:

        candle_component = min(
            candle_result["bearish_score"],
            WEIGHT_CANDLE,
        )

        candle_reasons = (
            candle_result[
                "bearish_reason"
            ]
        )

    score += candle_component

    reasons.extend(
        candle_reasons
    )

    if direction == "BULLISH":

        level_component = min(
            level_result["bullish_score"],
            WEIGHT_LEVEL,
        )

        level_reasons = (
            level_result[
                "bullish_reason"
            ]
        )

    else:

        level_component = min(
            level_result["bearish_score"],
            WEIGHT_LEVEL,
        )

        level_reasons = (
            level_result[
                "bearish_reason"
            ]
        )

    score += level_component

    reasons.extend(
        level_reasons
    )

    rejection_component = min(
        rejection_result["score"],
        WEIGHT_REJECTION,
    )

    score += rejection_component

    if rejection_result["reason"]:
        reasons.append(
            rejection_result["reason"]
        )

    momentum_component = min(
        momentum_result["score"],
        WEIGHT_MOMENTUM,
    )

    score += momentum_component

    if momentum_result["reason"]:
        reasons.append(
            momentum_result["reason"]
        )

    volatility_component = min(
        volatility_result["score"],
        WEIGHT_VOLATILITY,
    )

    score += volatility_component

    if volatility_result["reason"]:
        reasons.append(
            volatility_result["reason"]
        )

    exhausted = exhaustion_check(
        df,
        direction,
    )

    if exhausted:

        score -= 20

        reasons.append(
            "penalización por agotamiento"
        )

    if not df.empty:

        last_info = candle_info(
            df.iloc[-1]
        )

        if (
            last_info["body_ratio"]
            <= MAX_DOJI_RATIO
        ):

            score -= 25

            reasons.append(
                "penalización por vela indecisa"
            )

    score = max(
        0,
        min(
            int(score),
            100,
        ),
    )

    return {
        "direction": direction,
        "signal": (
            "call"
            if direction == "BULLISH"
            else "put"
        ),
        "score": score,
        "structure_score": structure_component,
        "recovery_score": recovery_component,
        "candle_score": candle_component,
        "level_score": level_component,
        "rejection_score": rejection_component,
        "momentum_score": momentum_component,
        "volatility_score": volatility_component,
        "exhaustion": exhausted,
        "reasons": reasons,
    }


# ============================================================
# SELECCIONAR MEJOR DIRECCIÓN
# ============================================================

def select_best_direction(
    bullish: Dict[str, Any],
    bearish: Dict[str, Any],
) -> Optional[Dict[str, Any]]:

    bullish_score = int(
        bullish.get("score", 0)
    )

    bearish_score = int(
        bearish.get("score", 0)
    )

    if (
        bullish_score < MIN_SCORE_TO_TRADE
        and bearish_score < MIN_SCORE_TO_TRADE
    ):
        return None

    if (
        bullish_score >= MIN_SCORE_TO_TRADE
        and bullish_score > bearish_score
    ):

        advantage = (
            bullish_score
            - bearish_score
        )

        if advantage < MIN_DIRECTION_ADVANTAGE:
            return None

        return bullish

    if (
        bearish_score >= MIN_SCORE_TO_TRADE
        and bearish_score > bullish_score
    ):

        advantage = (
            bearish_score
            - bullish_score
        )

        if advantage < MIN_DIRECTION_ADVANTAGE:
            return None

        return bearish

    return None


# ============================================================
# ANÁLISIS M1 EN VIVO
# ============================================================

def analyze_live_candle(
    candle_1m: Any,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    elapsed_seconds: Optional[float] = None,
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "elapsed_seconds": elapsed_seconds,
        "open": 0.0,
        "close": 0.0,
        "high": 0.0,
        "low": 0.0,
        "range": 0.0,
        "body": 0.0,
        "body_ratio": 0.0,
        "upper_wick": 0.0,
        "lower_wick": 0.0,
        "upper_wick_ratio": 0.0,
        "lower_wick_ratio": 0.0,
        "close_position": 0.50,
        "structure": "NEUTRAL",
        "bullish_score": 0,
        "bearish_score": 0,
        "bullish_pressure": 0,
        "bearish_pressure": 0,
        "analysis": {},
    }

    if candle_1m is None:
        return result

    try:

        current_df = pd.DataFrame(
            [candle_1m]
        )

        current_df = safe_dataframe(
            current_df
        )

        if current_df.empty:
            return result

        current = current_df.iloc[-1]

        info = candle_info(current)

        result.update(
            {
                "open": info["open"],
                "close": info["close"],
                "high": info["high"],
                "low": info["low"],
                "range": info["range"],
                "body": info["body"],
                "body_ratio": info["body_ratio"],
                "upper_wick": info["upper_wick"],
                "lower_wick": info["lower_wick"],
                "upper_wick_ratio": info[
                    "upper_wick_ratio"
                ],
                "lower_wick_ratio": info[
                    "lower_wick_ratio"
                ],
                "close_position": info[
                    "close_position"
                ],
                "direction": (
                    "BULLISH"
                    if info["bull"]
                    else (
                        "BEARISH"
                        if info["bear"]
                        else "NEUTRAL"
                    )
                ),
            }
        )

        hist = safe_dataframe(
            previous_m1
        )

        if not hist.empty:

            structure_df = pd.concat(
                [
                    hist,
                    current_df,
                ],
                ignore_index=True,
            )

            structure = market_structure(
                structure_df
            )

            result["structure"] = structure

            recovery = detect_recovery(
                structure_df
            )

            bullish = evaluate_direction(
                structure_df,
                "BULLISH",
                int(
                    recovery.get(
                        "bullish_score",
                        0,
                    )
                ),
            )

            bearish = evaluate_direction(
                structure_df,
                "BEARISH",
                int(
                    recovery.get(
                        "bearish_score",
                        0,
                    )
                ),
            )

            result["bullish_score"] = int(
                bullish["score"]
            )

            result["bearish_score"] = int(
                bearish["score"]
            )

            result["bullish_pressure"] = int(
                recovery.get(
                    "bullish_score",
                    0,
                )
            )

            result["bearish_pressure"] = int(
                recovery.get(
                    "bearish_score",
                    0,
                )
            )

            provisional = select_best_direction(
                bullish,
                bearish,
            )

            if provisional is not None:

                result["signal"] = provisional[
                    "signal"
                ]

                result["score"] = provisional[
                    "score"
                ]

            result["analysis"] = {
                "bullish": bullish,
                "bearish": bearish,
                "recovery": recovery,
            }

        return result

    except Exception:
        return result


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def analyze_market(
    candle_1m,
    previous_m1=None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "pattern": None,
        "reason": "",
        "structure": "NEUTRAL",
        "support": 0.0,
        "resistance": 0.0,
        "bullish_score": 0,
        "bearish_score": 0,
        "confidence": 0,
        "analysis": {},
    }

    if not pair:

        result["reason"] = (
            "Par no especificado"
        )

        return result

    hist = safe_dataframe(
        previous_m1
    )

    if len(hist) < MIN_CANDLES:

        result["reason"] = (
            "Historial insuficiente"
        )

        return result

    if candle_1m is not None:

        try:

            current = pd.DataFrame(
                [candle_1m]
            )

            current = safe_dataframe(
                current
            )

            if not current.empty:

                current_last = (
                    current.iloc[-1]
                )

                hist_last = (
                    hist.iloc[-1]
                )

                current_close = float(
                    current_last["close"]
                )

                hist_close = float(
                    hist_last["close"]
                )

                if current_close != hist_close:

                    hist = pd.concat(
                        [
                            hist,
                            current,
                        ],
                        ignore_index=True,
                    )

        except Exception:
            pass

    structure = market_structure(
        hist
    )

    result["structure"] = structure

    recovery = detect_recovery(
        hist
    )

    recovery_bullish = int(
        recovery.get(
            "bullish_score",
            0,
        )
    )

    recovery_bearish = int(
        recovery.get(
            "bearish_score",
            0,
        )
    )

    bullish = evaluate_direction(
        hist,
        "BULLISH",
        recovery_bullish,
    )

    bearish = evaluate_direction(
        hist,
        "BEARISH",
        recovery_bearish,
    )

    result["bullish_score"] = bullish[
        "score"
    ]

    result["bearish_score"] = bearish[
        "score"
    ]

    levels = recent_levels(
        hist,
        LEVEL_LOOKBACK,
    )

    result["support"] = levels[
        "support"
    ]

    result["resistance"] = levels[
        "resistance"
    ]

    best = select_best_direction(
        bullish,
        bearish,
    )

    if best is None:

        difference = abs(
            bullish["score"]
            - bearish["score"]
        )

        result["score"] = max(
            bullish["score"],
            bearish["score"],
        )

        result["reason"] = (
            "Sin confluencia suficiente "
            f"(CALL {bullish['score']} / "
            f"PUT {bearish['score']} / "
            f"diferencia {difference})"
        )

        result["analysis"] = {
            "bullish": bullish,
            "bearish": bearish,
            "recovery": recovery,
            "levels": levels,
        }

        return result

    result["valid"] = True

    result["signal"] = best[
        "signal"
    ]

    result["score"] = best[
        "score"
    ]

    result["confidence"] = best[
        "score"
    ]

    result["direction"] = best[
        "direction"
    ]

    result["pattern"] = (
        "CONFLUENCIA_RECOVERY"
    )

    result["reason"] = (
        f"{best['signal'].upper()} "
        f"confirmada con "
        f"{best['score']}/100: "
        + " + ".join(
            best["reasons"][:6]
        )
    )

    result["analysis"] = {
        "bullish": bullish,
        "bearish": bearish,
        "recovery": recovery,
        "levels": levels,
    }

    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(
    candle_1m,
    previous_m1=None,
    pair: Optional[str] = None,
):

    result = analyze_market(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        pair=pair,
    )

    return result.get(
        "signal"
    )


def signal(
    candle_1m,
    previous_m1=None,
    pair: Optional[str] = None,
):

    return get_signal(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        pair=pair,
    )


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    print(
        "Estrategia M1 de CONFLUENCIA + ANÁLISIS EN VIVO cargada correctamente"
    )
