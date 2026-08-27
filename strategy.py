from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

MIN_CANDLES = 20

# Score mínimo absoluto para permitir una operación.
MIN_SCORE_TO_TRADE = 75

# Tamaño mínimo del cuerpo para considerar una vela con dirección.
MIN_BODY_RATIO = 0.30

# Una vela por debajo de este ratio se considera pequeña / indecisa.
MAX_DOJI_RATIO = 0.20

# Tendencia / estructura.
STRUCTURE_LOOKBACK = 8

# Consolidación.
CONSOLIDATION_LOOKBACK = 6
CONSOLIDATION_MAX_RATIO = 0.55

# Pullback.
PULLBACK_LOOKBACK = 5

# Reversión.
REVERSAL_WICK_RATIO = 1.20

# Rechazo.
REJECTION_WICK_RATIO = 1.00


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

    open_price = float(
        candle["open"]
    )

    close = float(
        candle["close"]
    )

    high = float(
        candle["high"]
    )

    low = float(
        candle["low"]
    )

    total_range = high - low

    body = abs(
        close - open_price
    )

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
        }

    body_ratio = (
        body / total_range
    )

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

        # --------------------------------------------
        # HIGHER HIGHS
        # --------------------------------------------

        if highs[i] >= highs[i - 1]:

            higher_highs += 1

        # --------------------------------------------
        # HIGHER LOWS
        # --------------------------------------------

        if lows[i] >= lows[i - 1]:

            higher_lows += 1

        # --------------------------------------------
        # LOWER HIGHS
        # --------------------------------------------

        if highs[i] <= highs[i - 1]:

            lower_highs += 1

        # --------------------------------------------
        # LOWER LOWS
        # --------------------------------------------

        if lows[i] <= lows[i - 1]:

            lower_lows += 1

        # --------------------------------------------
        # CLOSES
        # --------------------------------------------

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

    # Máximo posible aproximado:
    # 7 + 7 + 7 = 21

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
# CONTAR VELAS EN UNA DIRECCIÓN
# ============================================================

def directional_candle_score(
    candles: pd.DataFrame,
    direction: str,
) -> int:

    score = 0

    for _, candle in candles.iterrows():

        info = candle_info(
            candle
        )

        if (
            info["body_ratio"]
            < MIN_BODY_RATIO
        ):
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
# IMPULSO
# ============================================================

def momentum_score(
    candles: pd.DataFrame,
    direction: str,
) -> int:

    if len(candles) < 2:

        return 0

    score = 0

    closes = candles[
        "close"
    ].tolist()

    for i in range(
        1,
        len(candles),
    ):

        candle = candles.iloc[i]

        info = candle_info(
            candle
        )

        if (
            info["body_ratio"]
            < 0.40
        ):
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
# SOPORTE Y RESISTENCIA RECIENTE
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = 10,
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
# PROXIMIDAD A UN NIVEL
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
# DETECTAR CONTINUIDAD
# ============================================================

def detect_continuity(
    df: pd.DataFrame,
    direction: str,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "reason": "",
    }

    if len(df) < 6:

        return result

    candles = df.tail(5)

    directional = (
        directional_candle_score(
            candles,
            direction,
        )
    )

    momentum = momentum_score(
        candles,
        direction,
    )

    last_info = candle_info(
        df.iloc[-1]
    )

    score = 0

    # --------------------------------------------------------
    # CONTINUIDAD DE VELAS
    # --------------------------------------------------------

    score += min(
        directional * 12,
        36,
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    score += min(
        momentum * 10,
        30,
    )

    # --------------------------------------------------------
    # ÚLTIMA VELA
    # --------------------------------------------------------

    if (
        direction == "BULLISH"
        and last_info["bull"]
    ):

        score += 20

        if (
            last_info["body_ratio"]
            >= 0.60
        ):

            score += 10

    elif (
        direction == "BEARISH"
        and last_info["bear"]
    ):

        score += 20

        if (
            last_info["body_ratio"]
            >= 0.60
        ):

            score += 10

    # --------------------------------------------------------
    # DETECCIÓN
    # --------------------------------------------------------

    if (
        directional >= 4
        and momentum >= 3
    ):

        result["detected"] = True
        result["score"] = min(
            score,
            100,
        )

        if direction == "BULLISH":

            result["signal"] = "call"

        else:

            result["signal"] = "put"

        result["reason"] = (
            "Continuidad confirmada"
        )

    return result


# ============================================================
# DETECTAR RECHAZO
# ============================================================

def detect_rejection(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "reason": "",
    }

    if len(df) < 10:

        return result

    last = df.iloc[-1]

    info = candle_info(
        last
    )

    if info["body"] <= 0:

        return result

    levels = recent_levels(
        df,
        10,
    )

    last_close = float(
        last["close"]
    )

    support = levels[
        "support"
    ]

    resistance = levels[
        "resistance"
    ]

    market_range = levels[
        "range"
    ]

    # --------------------------------------------------------
    # RECHAZO ALCISTA EN SOPORTE
    # --------------------------------------------------------

    if (
        info["lower_wick_ratio"]
        >= REJECTION_WICK_RATIO
        and info["bull"]
        and is_near(
            last_close,
            support,
            market_range,
        )
    ):

        score = 70

        if (
            info["body_ratio"]
            >= 0.40
        ):

            score += 15

        if (
            info["lower_wick_ratio"]
            >= 2.0
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "call",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Rechazo alcista "
                    "en soporte"
                ),
            }
        )

        return result

    # --------------------------------------------------------
    # RECHAZO BAJISTA EN RESISTENCIA
    # --------------------------------------------------------

    if (
        info["upper_wick_ratio"]
        >= REJECTION_WICK_RATIO
        and info["bear"]
        and is_near(
            last_close,
            resistance,
            market_range,
        )
    ):

        score = 70

        if (
            info["body_ratio"]
            >= 0.40
        ):

            score += 15

        if (
            info["upper_wick_ratio"]
            >= 2.0
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "put",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Rechazo bajista "
                    "en resistencia"
                ),
            }
        )

    return result


# ============================================================
# DETECTAR REVERSIÓN
# ============================================================

def detect_reversal(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "reason": "",
    }

    if len(df) < 6:

        return result

    last = df.iloc[-1]

    info = candle_info(
        last
    )

    previous = df.iloc[
        -4:-1
    ]

    previous_bears = (
        directional_candle_score(
            previous,
            "BEARISH",
        )
    )

    previous_bulls = (
        directional_candle_score(
            previous,
            "BULLISH",
        )
    )

    # --------------------------------------------------------
    # REVERSIÓN ALCISTA
    #
    # Varias velas bajistas antes
    # + mecha inferior dominante
    # + cierre alcista
    # --------------------------------------------------------

    if (
        previous_bears >= 2
        and info["bull"]
        and info["lower_wick_ratio"]
        >= REVERSAL_WICK_RATIO
    ):

        score = 60

        score += min(
            previous_bears * 8,
            24,
        )

        if (
            info["body_ratio"]
            >= 0.35
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "call",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Reversión alcista "
                    "confirmada"
                ),
            }
        )

        return result

    # --------------------------------------------------------
    # REVERSIÓN BAJISTA
    # --------------------------------------------------------

    if (
        previous_bulls >= 2
        and info["bear"]
        and info["upper_wick_ratio"]
        >= REVERSAL_WICK_RATIO
    ):

        score = 60

        score += min(
            previous_bulls * 8,
            24,
        )

        if (
            info["body_ratio"]
            >= 0.35
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "put",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Reversión bajista "
                    "confirmada"
                ),
            }
        )

    return result


# ============================================================
# DETECTAR PULLBACK
# ============================================================

def detect_pullback(
    df: pd.DataFrame,
    direction: str,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "reason": "",
    }

    if len(df) < 8:

        return result

    last = df.iloc[-1]

    last_info = candle_info(
        last
    )

    previous = df.iloc[
        -5:-1
    ]

    # --------------------------------------------------------
    # PULLBACK EN TENDENCIA ALCISTA
    #
    # Estructura general alcista
    # + velas anteriores de retroceso
    # + última vela vuelve a subir
    # --------------------------------------------------------

    if direction == "BULLISH":

        bearish_pullback = (
            directional_candle_score(
                previous.tail(3),
                "BEARISH",
            )
        )

        bullish_before = (
            directional_candle_score(
                df.iloc[-8:-4],
                "BULLISH",
            )
        )

        if (
            bullish_before >= 2
            and bearish_pullback >= 1
            and last_info["bull"]
            and last_info["body_ratio"]
            >= MIN_BODY_RATIO
        ):

            score = 60

            score += min(
                bullish_before * 7,
                14,
            )

            score += min(
                bearish_pullback * 6,
                12,
            )

            score += 15

            result.update(
                {
                    "detected": True,
                    "signal": "call",
                    "score": min(
                        score,
                        100,
                    ),
                    "reason": (
                        "Pullback alcista "
                        "y continuación"
                    ),
                }
            )

            return result

    # --------------------------------------------------------
    # PULLBACK EN TENDENCIA BAJISTA
    # --------------------------------------------------------

    elif direction == "BEARISH":

        bullish_pullback = (
            directional_candle_score(
                previous.tail(3),
                "BULLISH",
            )
        )

        bearish_before = (
            directional_candle_score(
                df.iloc[-8:-4],
                "BEARISH",
            )
        )

        if (
            bearish_before >= 2
            and bullish_pullback >= 1
            and last_info["bear"]
            and last_info["body_ratio"]
            >= MIN_BODY_RATIO
        ):

            score = 60

            score += min(
                bearish_before * 7,
                14,
            )

            score += min(
                bullish_pullback * 6,
                12,
            )

            score += 15

            result.update(
                {
                    "detected": True,
                    "signal": "put",
                    "score": min(
                        score,
                        100,
                    ),
                    "reason": (
                        "Pullback bajista "
                        "y continuación"
                    ),
                }
            )

    return result


# ============================================================
# DETECTAR CONSOLIDACIÓN
# ============================================================

def detect_consolidation(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "detected": False,
        "signal": None,
        "score": 0,
        "reason": "",
    }

    if len(df) < 12:

        return result

    zone = df.iloc[
        -7:-1
    ]

    last = df.iloc[-1]

    last_info = candle_info(
        last
    )

    zone_high = float(
        zone["high"].max()
    )

    zone_low = float(
        zone["low"].min()
    )

    zone_range = (
        zone_high
        - zone_low
    )

    previous_market = df.iloc[
        -12:-7
    ]

    previous_high = float(
        previous_market["high"].max()
    )

    previous_low = float(
        previous_market["low"].min()
    )

    previous_range = (
        previous_high
        - previous_low
    )

    if previous_range <= 0:

        return result

    # La zona reciente debe tener
    # menor rango que el movimiento anterior.
    if (
        zone_range
        > previous_range
        * CONSOLIDATION_MAX_RATIO
    ):

        return result

    last_close = float(
        last["close"]
    )

    # --------------------------------------------------------
    # RUPTURA ALCISTA
    # --------------------------------------------------------

    if (
        last_close > zone_high
        and last_info["bull"]
        and last_info["body_ratio"]
        >= 0.40
    ):

        score = 75

        if (
            last_info["body_ratio"]
            >= 0.60
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "call",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Ruptura alcista "
                    "de consolidación"
                ),
            }
        )

        return result

    # --------------------------------------------------------
    # RUPTURA BAJISTA
    # --------------------------------------------------------

    if (
        last_close < zone_low
        and last_info["bear"]
        and last_info["body_ratio"]
        >= 0.40
    ):

        score = 75

        if (
            last_info["body_ratio"]
            >= 0.60
        ):

            score += 10

        result.update(
            {
                "detected": True,
                "signal": "put",
                "score": min(
                    score,
                    100,
                ),
                "reason": (
                    "Ruptura bajista "
                    "de consolidación"
                ),
            }
        )

    return result


# ============================================================
# EVITAR AGOTAMIENTO
# ============================================================

def exhaustion_check(
    df: pd.DataFrame,
    direction: str,
) -> bool:

    if len(df) < 5:

        return False

    last_four = df.tail(4)

    same_direction = 0

    for _, candle in last_four.iterrows():

        info = candle_info(
            candle
        )

        if (
            direction == "BULLISH"
            and info["bull"]
            and info["body_ratio"]
            >= 0.60
        ):

            same_direction += 1

        elif (
            direction == "BEARISH"
            and info["bear"]
            and info["body_ratio"]
            >= 0.60
        ):

            same_direction += 1

    # Cuatro velas muy fuertes consecutivas
    # pueden indicar entrada demasiado tarde.

    return (
        same_direction >= 4
    )


# ============================================================
# SELECCIONAR MEJOR PATRÓN
# ============================================================

def select_best_pattern(
    patterns: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:

    valid_patterns = []

    for pattern in patterns:

        if not pattern.get(
            "detected",
            False,
        ):
            continue

        signal = pattern.get(
            "signal"
        )

        score = pattern.get(
            "score",
            0,
        )

        if signal not in (
            "call",
            "put",
        ):
            continue

        if score <= 0:
            continue

        valid_patterns.append(
            pattern
        )

    if not valid_patterns:

        return None

    valid_patterns.sort(
        key=lambda x: x.get(
            "score",
            0,
        ),
        reverse=True,
    )

    return valid_patterns[0]


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def analyze_market(
    candle_1m,
    previous_m1=None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    # --------------------------------------------------------
    # RESULTADO BASE
    # --------------------------------------------------------

    result = {
        "valid": False,
        "signal": None,
        "score": 0,
        "direction": "NEUTRAL",
        "pair": pair,
        "pattern": None,
        "reason": "",
    }

    # --------------------------------------------------------
    # VALIDAR PAR
    # --------------------------------------------------------

    if not pair:

        result["reason"] = (
            "Par no especificado"
        )

        return result

    # --------------------------------------------------------
    # PREPARAR HISTORIAL
    # --------------------------------------------------------

    hist = safe_dataframe(
        previous_m1
    )

    if len(hist) < MIN_CANDLES:

        result["reason"] = (
            "Historial insuficiente"
        )

        return result

    # --------------------------------------------------------
    # ASEGURAR QUE LA ÚLTIMA VELA
    # SEA LA VELA QUE ESTAMOS ANALIZANDO
    # --------------------------------------------------------

    if candle_1m is not None:

        try:

            current = pd.DataFrame(
                [candle_1m]
            )

            current = safe_dataframe(
                current
            )

            if not current.empty:

                current_last = current.iloc[-1]

                hist_last = hist.iloc[-1]

                if (
                    float(
                        current_last["close"]
                    )
                    != float(
                        hist_last["close"]
                    )
                ):

                    hist = pd.concat(
                        [
                            hist,
                            current,
                        ],
                        ignore_index=True,
                    )

        except Exception:
            pass

    # --------------------------------------------------------
    # ESTRUCTURA
    # --------------------------------------------------------

    direction = market_structure(
        hist
    )

    result[
        "direction"
    ] = direction

    # --------------------------------------------------------
    # DETECTAR LOS 5 PATRONES
    # --------------------------------------------------------

    patterns = []

    # REVERSIÓN
    reversal = detect_reversal(
        hist
    )

    reversal["pattern"] = (
        "REVERSAL"
    )

    patterns.append(
        reversal
    )

    # RECHAZO
    rejection = detect_rejection(
        hist
    )

    rejection["pattern"] = (
        "REJECTION"
    )

    patterns.append(
        rejection
    )

    # CONSOLIDACIÓN
    consolidation = (
        detect_consolidation(
            hist
        )
    )

    consolidation["pattern"] = (
        "CONSOLIDATION"
    )

    patterns.append(
        consolidation
    )

    # CONTINUIDAD Y PULLBACK
    # necesitan una estructura direccional.

    if direction in (
        "BULLISH",
        "BEARISH",
    ):

        continuity = (
            detect_continuity(
                hist,
                direction,
            )
        )

        continuity["pattern"] = (
            "CONTINUITY"
        )

        patterns.append(
            continuity
        )

        pullback = detect_pullback(
            hist,
            direction,
        )

        pullback["pattern"] = (
            "PULLBACK"
        )

        patterns.append(
            pullback
        )

    # --------------------------------------------------------
    # MEJOR PATRÓN
    # --------------------------------------------------------

    best = select_best_pattern(
        patterns
    )

    if best is None:

        result["reason"] = (
            "No se detectó patrón válido"
        )

        return result

    # --------------------------------------------------------
    # PENALIZACIÓN POR AGOTAMIENTO
    #
    # No aplicamos la misma penalización
    # a reversión, rechazo o consolidación,
    # porque esos patrones pueden aparecer
    # precisamente después de un movimiento.
    # --------------------------------------------------------

    score = int(
        best["score"]
    )

    pattern = best[
        "pattern"
    ]

    signal = best[
        "signal"
    ]

    if pattern in (
        "CONTINUITY",
        "PULLBACK",
    ):

        signal_direction = (
            "BULLISH"
            if signal == "call"
            else "BEARISH"
        )

        if exhaustion_check(
            hist,
            signal_direction,
        ):

            score -= 15

    # --------------------------------------------------------
    # ÚLTIMA VELA
    # --------------------------------------------------------

    last_info = candle_info(
        hist.iloc[-1]
    )

    # Penalizamos velas demasiado pequeñas
    # en patrones que requieren confirmación.

    if (
        pattern in (
            "CONTINUITY",
            "PULLBACK",
            "CONSOLIDATION",
        )
        and last_info["body_ratio"]
        < MAX_DOJI_RATIO
    ):

        score -= 20

    # --------------------------------------------------------
    # LIMITAR SCORE
    # --------------------------------------------------------

    score = max(
        0,
        min(
            score,
            100,
        ),
    )

    result["score"] = score
    result["pattern"] = pattern
    result["signal"] = signal
    result["reason"] = best.get(
        "reason",
        "",
    )

    # --------------------------------------------------------
    # VALIDACIÓN FINAL
    # --------------------------------------------------------

    if score < MIN_SCORE_TO_TRADE:

        result["valid"] = False

        result["reason"] = (
            f"{result['reason']} | "
            f"Score insuficiente: "
            f"{score}/{MIN_SCORE_TO_TRADE}"
        )

        return result

    result["valid"] = True

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
        "Estrategia cargada correctamente"
    )
