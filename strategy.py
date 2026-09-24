"""strategy.py - Stochastic expansion + soporte/resistencia compatible con bot.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math


# ==========================================================
# CONFIGURACIÓN
# ==========================================================

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75

STOCH_K_PERIOD = 13
STOCH_D_PERIOD = 3
STOCH_SMOOTHING = 3

STOCH_EXPANSION_LOOKBACK = 5

# ==========================================================
# FILTROS / PUNTUACIÓN STOCHASTIC
# ==========================================================

# Estos valores YA NO son bloqueadores.
# Se utilizan para determinar la calidad de la entrada.

CALL_MAX_STOCH = 30.0
PUT_MIN_STOCH = 70.0

MIN_STOCH_SEPARATION = 2.0
STRONG_STOCH_SEPARATION = 8.0
VERY_STRONG_STOCH_SEPARATION = 12.0


# ==========================================================
# DATACLASS
# ==========================================================

@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


# ==========================================================
# UTILIDADES
# ==========================================================

def _number(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        value = float(value)

        return (
            value
            if math.isfinite(value)
            else default
        )

    except (TypeError, ValueError):
        return default


def _ohlc(
    candle: Dict[str, Any],
) -> Tuple[float, float, float, float]:

    open_price = _number(
        candle.get(
            "open",
            candle.get("open_price"),
        )
    )

    close_price = _number(
        candle.get(
            "close",
            candle.get("close_price"),
        )
    )

    low = _number(
        candle.get(
            "min",
            candle.get("low"),
        )
    )

    high = _number(
        candle.get(
            "max",
            candle.get("high"),
        )
    )

    return (
        open_price,
        close_price,
        low,
        high,
    )


def _as_records(
    value: Any,
) -> List[Dict[str, Any]]:

    if value is None:
        return []

    if hasattr(value, "to_dict"):

        try:
            records = value.to_dict(
                "records"
            )

            return [
                dict(item)
                for item in records
            ]

        except (
            TypeError,
            ValueError,
        ):
            return []

    if isinstance(value, dict):
        return [value]

    try:
        return [
            dict(item)
            for item in value
            if isinstance(item, dict)
        ]

    except TypeError:
        return []


# ==========================================================
# ATR
# ==========================================================

def _atr(
    candles: List[Dict[str, Any]],
    period: int = 14,
) -> float:

    if len(candles) < 2:
        return 0.0

    true_ranges: List[float] = []

    for index in range(
        1,
        len(candles),
    ):

        _, _, low, high = _ohlc(
            candles[index]
        )

        _, previous_close, _, _ = _ohlc(
            candles[index - 1]
        )

        true_ranges.append(
            max(
                high - low,
                abs(
                    high - previous_close
                ),
                abs(
                    low - previous_close
                ),
            )
        )

    values = true_ranges[-period:]

    if not values:
        return 0.0

    return sum(values) / len(values)


# ==========================================================
# SOPORTE / RESISTENCIA
# ==========================================================

def _zones(
    candles: List[Dict[str, Any]],
    lookback: int = DEFAULT_LOOKBACK,
) -> Tuple[float, float]:

    sample = candles[
        -max(1, lookback):
    ]

    lows = [
        _ohlc(candle)[2]
        for candle in sample
    ]

    highs = [
        _ohlc(candle)[3]
        for candle in sample
    ]

    if not lows or not highs:
        return 0.0, 0.0

    return (
        min(lows),
        max(highs),
    )


# ==========================================================
# STOCHASTIC SERIES
# ==========================================================

def _stochastic_series(
    candles: List[Dict[str, Any]],
    k_period: int = STOCH_K_PERIOD,
    d_period: int = STOCH_D_PERIOD,
    slowing: int = STOCH_SMOOTHING,
) -> Tuple[List[float], List[float]]:

    minimum_candles = max(
        k_period + d_period + slowing,
        5,
    )

    if len(candles) < minimum_candles:
        return [], []

    highs = [
        _ohlc(candle)[3]
        for candle in candles
    ]

    lows = [
        _ohlc(candle)[2]
        for candle in candles
    ]

    closes = [
        _ohlc(candle)[1]
        for candle in candles
    ]

    raw_k: List[float] = []

    for i in range(
        k_period - 1,
        len(candles),
    ):

        window_high = max(
            highs[
                i - k_period + 1:i + 1
            ]
        )

        window_low = min(
            lows[
                i - k_period + 1:i + 1
            ]
        )

        span = (
            window_high
            - window_low
        )

        if span <= 0:

            raw_k.append(50.0)

        else:

            raw_k.append(
                100.0
                * (
                    closes[i]
                    - window_low
                )
                / span
            )

    if not raw_k:
        return [], []

    # ------------------------------------------------------
    # K SUAVIZADO
    # ------------------------------------------------------

    smoothed_k: List[float] = []

    for i in range(
        len(raw_k)
    ):

        start = max(
            0,
            i - slowing + 1,
        )

        window = raw_k[
            start:i + 1
        ]

        smoothed_k.append(
            sum(window)
            / len(window)
        )

    # ------------------------------------------------------
    # D
    # ------------------------------------------------------

    d_values: List[float] = []

    for i in range(
        len(smoothed_k)
    ):

        start = max(
            0,
            i - d_period + 1,
        )

        window = smoothed_k[
            start:i + 1
        ]

        d_values.append(
            sum(window)
            / len(window)
        )

    return (
        [
            max(
                0.0,
                min(
                    100.0,
                    value,
                ),
            )
            for value in smoothed_k
        ],
        [
            max(
                0.0,
                min(
                    100.0,
                    value,
                ),
            )
            for value in d_values
        ],
    )


def _stochastic(
    candles: List[Dict[str, Any]],
    k_period: int = STOCH_K_PERIOD,
    d_period: int = STOCH_D_PERIOD,
    slowing: int = STOCH_SMOOTHING,
) -> Tuple[float, float]:

    k_values, d_values = (
        _stochastic_series(
            candles,
            k_period,
            d_period,
            slowing,
        )
    )

    if not k_values or not d_values:
        return 50.0, 50.0

    return (
        k_values[-1],
        d_values[-1],
    )


# ==========================================================
# STOCHASTIC EXPANSION
# ==========================================================

def _stochastic_expansion(
    candles: List[Dict[str, Any]],
) -> Dict[str, Any]:

    """
    Detecta expansión del Stochastic.

    IMPORTANTE:
    La máxima separación histórica NO incluye la vela actual.

    El Stochastic ayuda a determinar la calidad,
    pero NO bloquea automáticamente una entrada
    solamente porque K/D estén fuera de 30/70.
    """

    k_values, d_values = (
        _stochastic_series(candles)
    )

    minimum = (
        STOCH_EXPANSION_LOOKBACK
        + 2
    )

    if len(k_values) < minimum:

        return {
            "valid": False,
            "direction": None,
            "k": 50.0,
            "d": 50.0,
            "separation": 0.0,
            "previous_separation": 0.0,
            "maximum_separation": 0.0,
            "k_change": 0.0,
            "d_change": 0.0,
            "expanding": False,
            "maximum": False,
            "oversold": False,
            "overbought": False,
            "enough_separation": False,
        }

    current_k = k_values[-1]
    current_d = d_values[-1]

    previous_k = k_values[-2]
    previous_d = d_values[-2]

    current_separation = abs(
        current_k - current_d
    )

    previous_separation = abs(
        previous_k - previous_d
    )

    # ------------------------------------------------------
    # HISTORIAL SIN INCLUIR LA VELA ACTUAL
    # ------------------------------------------------------

    history_end = len(k_values) - 1

    history_start = max(
        0,
        history_end
        - STOCH_EXPANSION_LOOKBACK,
    )

    historical_separations = [
        abs(
            k_values[i]
            - d_values[i]
        )
        for i in range(
            history_start,
            history_end,
        )
    ]

    maximum_separation = (
        max(historical_separations)
        if historical_separations
        else 0.0
    )

    k_change = (
        current_k - previous_k
    )

    d_change = (
        current_d - previous_d
    )

    expanding = (
        current_separation
        > previous_separation
    )

    maximum = (
        current_separation
        >= maximum_separation
    )

    oversold = (
        current_k <= CALL_MAX_STOCH
        and current_d <= CALL_MAX_STOCH
    )

    overbought = (
        current_k >= PUT_MIN_STOCH
        and current_d >= PUT_MIN_STOCH
    )

    enough_separation = (
        current_separation
        >= MIN_STOCH_SEPARATION
    )

    # ------------------------------------------------------
    # DIRECCIÓN DEL STOCHASTIC
    # ------------------------------------------------------

    bullish = (
        current_k > current_d
        and k_change > 0
        and expanding
        and enough_separation
    )

    bearish = (
        current_k < current_d
        and k_change < 0
        and expanding
        and enough_separation
    )

    if bullish:
        direction = "call"

    elif bearish:
        direction = "put"

    else:
        direction = None

    return {
        "valid": direction is not None,
        "direction": direction,
        "k": current_k,
        "d": current_d,
        "separation": current_separation,
        "previous_separation": previous_separation,
        "maximum_separation": maximum_separation,
        "k_change": k_change,
        "d_change": d_change,
        "expanding": expanding,
        "maximum": maximum,
        "oversold": oversold,
        "overbought": overbought,
        "enough_separation": enough_separation,
    }


# ==========================================================
# RESULTADO VACÍO
# ==========================================================

def _empty_result(
    reason: str = "no_valid_rejection",
) -> Dict[str, Any]:

    return {
        "signal": None,
        "score": 0,
        "score_100": 0,
        "entry_quality": 0,
        "quality": 0,
        "entry_type": "none",
        "direction": "range",
        "reason": reason,
        "analysis": {
            "force": False,
            "structure": "range",
            "atr": 0.0,
            "support": None,
            "resistance": None,
            "tolerance": 0.0,
            "entry_quality": 0,
            "pullback": {
                "valid": False,
            },
        },
    }


# ==========================================================
# ANÁLISIS DE RECHAZO
# ==========================================================

def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
    stochastic_k: float = 50.0,
    stochastic_d: float = 50.0,
) -> Signal:

    records = _as_records(
        candles
    )

    if len(records) < max(
        MIN_CANDLES,
        lookback // 2,
    ):

        return Signal(
            "none",
            0,
            "insufficient_candles",
        )

    previous = records[:-1]
    candle = records[-1]

    (
        open_price,
        close_price,
        low,
        high,
    ) = _ohlc(candle)

    body = abs(
        close_price
        - open_price
    )

    candle_range = max(
        high - low,
        1e-12,
    )

    upper_wick = max(
        high
        - max(
            open_price,
            close_price,
        ),
        0.0,
    )

    lower_wick = max(
        min(
            open_price,
            close_price,
        )
        - low,
        0.0,
    )

    atr = _atr(
        previous
    )

    if atr <= 0:

        return Signal(
            "none",
            0,
            "invalid_atr",
        )

    support, resistance = _zones(
        previous,
        lookback,
    )

    tolerance = (
        atr
        * max(
            zone_atr_factor,
            0.01,
        )
    )

    # ======================================================
    # SOPORTE / RESISTENCIA
    # ======================================================

    near_support = (
        low <= support + tolerance
        and close_price > support
    )

    near_resistance = (
        high >= resistance - tolerance
        and close_price < resistance
    )

    # ======================================================
    # STOCHASTIC
    # ======================================================

    stoch = _stochastic_expansion(
        records
    )

    # ======================================================
    # POSICIÓN DEL CIERRE
    # ======================================================

    bullish_close_position = (
        close_price - low
    ) / candle_range

    bearish_close_position = (
        high - close_price
    ) / candle_range

    # ======================================================
    # MECHAS
    # ======================================================

    strong_lower_wick = (
        lower_wick
        >= max(
            body * 1.25,
            atr * 0.20,
        )
    )

    strong_upper_wick = (
        upper_wick
        >= max(
            body * 1.25,
            atr * 0.20,
        )
    )

    # ======================================================
    # DIRECCIÓN DE LA VELA
    # ======================================================

    bullish_body = (
        close_price > open_price
    )

    bearish_body = (
        close_price < open_price
    )

    # ======================================================
    # CALL
    # ======================================================

    bullish = (
        stoch["valid"]
        and stoch["direction"] == "call"

        # Rechazo de soporte
        and near_support

        # Mecha de rechazo
        and strong_lower_wick

        # Cierre alcista
        and bullish_body

        # Cierre en zona superior
        and bullish_close_position >= 0.60
    )

    # ======================================================
    # PUT
    # ======================================================

    bearish = (
        stoch["valid"]
        and stoch["direction"] == "put"

        # Rechazo de resistencia
        and near_resistance

        # Mecha de rechazo
        and strong_upper_wick

        # Cierre bajista
        and bearish_body

        # Cierre en zona inferior
        and bearish_close_position >= 0.60
    )

    # ======================================================
    # SCORE CALL
    # ======================================================

    if bullish:

        score = 0
        reasons: List[str] = []

        # --------------------------------------------------
        # 1. RECHAZO DE SOPORTE
        # --------------------------------------------------

        score += 25

        reasons.append(
            "support_rejection"
        )

        # --------------------------------------------------
        # 2. MECHA
        # --------------------------------------------------

        if lower_wick >= body * 2:

            score += 20

            reasons.append(
                "strong_lower_wick"
            )

        else:

            score += 15

            reasons.append(
                "lower_wick"
            )

        # --------------------------------------------------
        # 3. CUERPO / CIERRE
        # --------------------------------------------------

        if bullish_close_position >= 0.75:

            score += 15

            reasons.append(
                "strong_bullish_close"
            )

        else:

            score += 10

            reasons.append(
                "bullish_close"
            )

        # --------------------------------------------------
        # 4. STOCHASTIC
        # --------------------------------------------------

        if (
            stochastic_k <= 20
            and stochastic_d <= 20
        ):

            score += 15

            reasons.append(
                "deep_oversold"
            )

        elif (
            stochastic_k <= 30
            and stochastic_d <= 30
        ):

            score += 12

            reasons.append(
                "oversold"
            )

        elif stochastic_k <= 40:

            score += 8

            reasons.append(
                "stoch_favorable"
            )

        elif stochastic_k <= 55:

            score += 5

            reasons.append(
                "stoch_neutral_bullish"
            )

        else:

            score += 2

            reasons.append(
                "stoch_high"
            )

        # --------------------------------------------------
        # 5. EXPANSIÓN
        # --------------------------------------------------

        separation = (
            stoch["separation"]
        )

        if (
            separation
            >= VERY_STRONG_STOCH_SEPARATION
        ):

            score += 15

            reasons.append(
                "very_strong_stoch_expansion"
            )

        elif (
            separation
            >= STRONG_STOCH_SEPARATION
        ):

            score += 12

            reasons.append(
                "strong_stoch_expansion"
            )

        elif (
            separation
            >= MIN_STOCH_SEPARATION
        ):

            score += 8

            reasons.append(
                "stoch_expansion"
            )

        else:

            score += 2

            reasons.append(
                "weak_stoch_separation"
            )

        # --------------------------------------------------
        # 6. PROXIMIDAD AL SOPORTE
        # --------------------------------------------------

        if (
            close_price
            <= support
            + tolerance * 0.50
        ):

            score += 10

            reasons.append(
                "close_to_support"
            )

        else:

            score += 5

            reasons.append(
                "near_support"
            )

        score = min(
            score,
            100,
        )

        if score >= int(
            min_score
        ):

            return Signal(
                "call",
                score,
                (
                    "bullish_"
                    "stochastic_expansion_"
                    "support_rejection"
                    " | "
                    + ",".join(reasons)
                ),
                support,
                resistance,
            )

    # ======================================================
    # SCORE PUT
    # ======================================================

    if bearish:

        score = 0
        reasons = []

        # --------------------------------------------------
        # 1. RECHAZO DE RESISTENCIA
        # --------------------------------------------------

        score += 25

        reasons.append(
            "resistance_rejection"
        )

        # --------------------------------------------------
        # 2. MECHA
        # --------------------------------------------------

        if upper_wick >= body * 2:

            score += 20

            reasons.append(
                "strong_upper_wick"
            )

        else:

            score += 15

            reasons.append(
                "upper_wick"
            )

        # --------------------------------------------------
        # 3. CUERPO / CIERRE
        # --------------------------------------------------

        if bearish_close_position >= 0.75:

            score += 15

            reasons.append(
                "strong_bearish_close"
            )

        else:

            score += 10

            reasons.append(
                "bearish_close"
            )

        # --------------------------------------------------
        # 4. STOCHASTIC
        # --------------------------------------------------

        if (
            stochastic_k >= 80
            and stochastic_d >= 80
        ):

            score += 15

            reasons.append(
                "deep_overbought"
            )

        elif (
            stochastic_k >= 70
            and stochastic_d >= 70
        ):

            score += 12

            reasons.append(
                "overbought"
            )

        elif stochastic_k >= 60:

            score += 8

            reasons.append(
                "stoch_favorable"
            )

        elif stochastic_k >= 45:

            score += 5

            reasons.append(
                "stoch_neutral_bearish"
            )

        else:

            score += 2

            reasons.append(
                "stoch_low"
            )

        # --------------------------------------------------
        # 5. EXPANSIÓN
        # --------------------------------------------------

        separation = (
            stoch["separation"]
        )

        if (
            separation
            >= VERY_STRONG_STOCH_SEPARATION
        ):

            score += 15

            reasons.append(
                "very_strong_stoch_expansion"
            )

        elif (
            separation
            >= STRONG_STOCH_SEPARATION
        ):

            score += 12

            reasons.append(
                "strong_stoch_expansion"
            )

        elif (
            separation
            >= MIN_STOCH_SEPARATION
        ):

            score += 8

            reasons.append(
                "stoch_expansion"
            )

        else:

            score += 2

            reasons.append(
                "weak_stoch_separation"
            )

        # --------------------------------------------------
        # 6. PROXIMIDAD A RESISTENCIA
        # --------------------------------------------------

        if (
            close_price
            >= resistance
            - tolerance * 0.50
        ):

            score += 10

            reasons.append(
                "close_to_resistance"
            )

        else:

            score += 5

            reasons.append(
                "near_resistance"
            )

        score = min(
            score,
            100,
        )

        if score >= int(
            min_score
        ):

            return Signal(
                "put",
                score,
                (
                    "bearish_"
                    "stochastic_expansion_"
                    "resistance_rejection"
                    " | "
                    + ",".join(reasons)
                ),
                support,
                resistance,
            )

    # ======================================================
    # SIN SEÑAL
    # ======================================================

    return Signal(
        "none",
        0,
        "no_valid_stochastic_expansion",
        support,
        resistance,
    )


# ==========================================================
# ANALYZE MARKET
# ==========================================================

def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    **_: Any,
) -> Dict[str, Any]:

    # ======================================================
    # CONSTRUIR REGISTROS
    # ======================================================

    if previous_m1 is not None:

        records = _as_records(
            previous_m1
        )

        if candle_1m is not None:

            current_closed = (
                dict(candle_1m)
                if isinstance(
                    candle_1m,
                    dict,
                )
                else {}
            )

            if current_closed:
                records.append(
                    current_closed
                )

    elif df is not None:

        all_records = _as_records(
            df
        )

        records = (
            all_records[:-1]
            if len(all_records) > 1
            else []
        )

    else:

        all_records = _as_records(
            candle_1m
        )

        records = (
            all_records[:-1]
            if len(all_records) > 1
            else []
        )

    # ======================================================
    # RESULTADO VACÍO
    # ======================================================

    result = _empty_result()

    if len(records) < MIN_CANDLES:

        result["reason"] = (
            "insufficient_candles"
        )

        return result

    # ======================================================
    # STOCHASTIC
    # ======================================================

    stochastic_k, stochastic_d = (
        _stochastic(
            records
        )
    )

    stochastic_expansion = (
        _stochastic_expansion(
            records
        )
    )

    # ======================================================
    # ANÁLISIS
    # ======================================================

    signal = analyze_rejection(
        records,
        min_score=min_score,
        stochastic_k=stochastic_k,
        stochastic_d=stochastic_d,
    )

    records_last = records[-1]

    timestamp = records_last.get(
        "from",
        records_last.get(
            "timestamp"
        ),
    )

    # ======================================================
    # ATR
    # ======================================================

    atr = (
        _atr(
            records[:-1]
        )
        if len(records) > 1
        else 0.0
    )

    # ======================================================
    # DIRECCIÓN
    # ======================================================

    direction = (
        "bullish"
        if signal.action == "call"

        else
        "bearish"
        if signal.action == "put"

        else
        "range"
    )

    # ======================================================
    # FORCE
    # ======================================================

    force = (
        signal.action
        in {
            "call",
            "put",
        }
        and signal.score
        >= int(min_score)
    )

    # ======================================================
    # DATOS DE LA VELA
    # ======================================================

    (
        open_price,
        close_price,
        low,
        high,
    ) = _ohlc(
        records_last
    )

    body = abs(
        close_price
        - open_price
    )

    candle_range = max(
        high - low,
        1e-12,
    )

    upper_wick = max(
        high
        - max(
            open_price,
            close_price,
        ),
        0.0,
    )

    lower_wick = max(
        min(
            open_price,
            close_price,
        )
        - low,
        0.0,
    )

    # ======================================================
    # ANÁLISIS DETALLADO
    # ======================================================

    analysis = {

        "force": force,

        "structure": direction,

        "atr": atr,

        "support": signal.support,

        "resistance": signal.resistance,

        "tolerance": (
            atr * 0.35
        ),

        "entry_quality": signal.score,

        # --------------------------------------------------
        # VELA
        # --------------------------------------------------

        "candle_open": open_price,

        "candle_close": close_price,

        "candle_high": high,

        "candle_low": low,

        "candle_body": body,

        "candle_range": candle_range,

        "upper_wick": upper_wick,

        "lower_wick": lower_wick,

        "bullish_candle": (
            close_price > open_price
        ),

        "bearish_candle": (
            close_price < open_price
        ),

        "close_position": round(
            (
                close_price
                - low
            )
            / candle_range,
            4,
        ),

        # --------------------------------------------------
        # STOCHASTIC
        # --------------------------------------------------

        "stochastic_k": round(
            stochastic_k,
            2,
        ),

        "stochastic_d": round(
            stochastic_d,
            2,
        ),

        "stochastic_separation": round(
            stochastic_expansion.get(
                "separation",
                0.0,
            ),
            2,
        ),

        "stochastic_previous_separation": round(
            stochastic_expansion.get(
                "previous_separation",
                0.0,
            ),
            2,
        ),

        "stochastic_maximum_separation": round(
            stochastic_expansion.get(
                "maximum_separation",
                0.0,
            ),
            2,
        ),

        "stochastic_k_change": round(
            stochastic_expansion.get(
                "k_change",
                0.0,
            ),
            2,
        ),

        "stochastic_d_change": round(
            stochastic_expansion.get(
                "d_change",
                0.0,
            ),
            2,
        ),

        "stochastic_expanding": (
            stochastic_expansion.get(
                "expanding",
                False,
            )
        ),

        "stochastic_maximum": (
            stochastic_expansion.get(
                "maximum",
                False,
            )
        ),

        "stochastic_direction": (
            stochastic_expansion.get(
                "direction"
            )
        ),

        "stochastic_oversold": (
            stochastic_expansion.get(
                "oversold",
                False,
            )
        ),

        "stochastic_overbought": (
            stochastic_expansion.get(
                "overbought",
                False,
            )
        ),

        "stochastic_enough_separation": (
            stochastic_expansion.get(
                "enough_separation",
                False,
            )
        ),

        # --------------------------------------------------
        # ESTRUCTURA
        # --------------------------------------------------

        "last_swing_high": (
            signal.resistance
        ),

        "last_swing_low": (
            signal.support
        ),

        # --------------------------------------------------
        # TIEMPOS
        # --------------------------------------------------

        "rejection_timestamp": timestamp,

        "confirmation_timestamp": timestamp,

        # --------------------------------------------------
        # VELAS
        # --------------------------------------------------

        "rejection_candle": (
            records_last
        ),

        "confirmation_candle": (
            records_last
        ),

        # --------------------------------------------------
        # PULLBACK
        # --------------------------------------------------

        "pullback": {

            "valid": force,

            "previous_candle_confirmed": (
                force
            ),

            "extreme_confirmed": (
                force
            ),
        },
    }

    # ======================================================
    # RESULTADO
    # ======================================================

    return {

        "signal": (
            signal.action
            if force
            else None
        ),

        "score": (
            signal.score
            if force
            else 0
        ),

        "score_100": (
            signal.score
            if force
            else 0
        ),

        "entry_quality": (
            signal.score
            if force
            else 0
        ),

        "quality": (
            signal.score
            if force
            else 0
        ),

        "entry_type": (
            "force"
            if force
            else "none"
        ),

        "direction": direction,

        "reason": (
            f"{signal.reason} | "
            f"STOCH K={stochastic_k:.1f} "
            f"D={stochastic_d:.1f} | "
            f"SEP="
            f"{stochastic_expansion.get('separation', 0.0):.2f}"
        ),

        "analysis": analysis,

        "candle_timestamp": timestamp,

        "pair": pair,
    }


# ==========================================================
# GET SIGNAL
# ==========================================================

def get_signal(
    df: Any,
) -> Optional[str]:

    return analyze_market(
        df=df
    ).get(
        "signal"
    )


# ==========================================================
# SIGNAL
# ==========================================================

def signal(
    df: Any,
) -> Optional[str]:

    return get_signal(
        df
    )

Esta es la versión que usaría para la siguiente etapa de recopilación. No la haría más restrictiva todavía: primero necesitamos ver qué entradas produce y cuáles terminan en WIN/LOSS. Luego podremos ajustar los umbrales con datos reales en lugar de adivinarlos.

Importante: si con esta versión sigue sin ejecutar señales, entonces el problema probablemente ya no está en estos filtros de "strategy.py", sino en cómo "bot.py" llama a "analyze_market()" o en la revalidación N+1. En ese caso, pásame el "bot.py" actual y revisamos únicamente esa parte.
