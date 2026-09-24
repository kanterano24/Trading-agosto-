"""strategy.py - Stochastic expansion + soporte/resistencia compatible con bot.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math

MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75

STOCH_K_PERIOD = 13
STOCH_D_PERIOD = 3
STOCH_SMOOTHING = 3

# Ventana utilizada para determinar la máxima separación reciente.
STOCH_EXPANSION_LOOKBACK = 5


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    open_price = _number(
        candle.get("open", candle.get("open_price"))
    )
    close_price = _number(
        candle.get("close", candle.get("close_price"))
    )
    low = _number(
        candle.get("min", candle.get("low"))
    )
    high = _number(
        candle.get("max", candle.get("high"))
    )

    return open_price, close_price, low, high


def _as_records(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []

    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
            return [dict(item) for item in records]
        except (TypeError, ValueError):
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


def _atr(
    candles: List[Dict[str, Any]],
    period: int = 14,
) -> float:
    if len(candles) < 2:
        return 0.0

    true_ranges: List[float] = []

    for index in range(1, len(candles)):
        _, _, low, high = _ohlc(candles[index])
        _, previous_close, _, _ = _ohlc(
            candles[index - 1]
        )

        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    values = true_ranges[-period:]

    return (
        sum(values) / len(values)
        if values
        else 0.0
    )


def _zones(
    candles: List[Dict[str, Any]],
    lookback: int = DEFAULT_LOOKBACK,
) -> Tuple[float, float]:

    sample = candles[-max(1, lookback):]

    lows = [
        _ohlc(candle)[2]
        for candle in sample
    ]

    highs = [
        _ohlc(candle)[3]
        for candle in sample
    ]

    return (
        (min(lows), max(highs))
        if lows and highs
        else (0.0, 0.0)
    )


def _stochastic_series(
    candles: List[Dict[str, Any]],
    k_period: int = STOCH_K_PERIOD,
    d_period: int = STOCH_D_PERIOD,
    slowing: int = STOCH_SMOOTHING,
) -> Tuple[List[float], List[float]]:
    """
    Calcula las series completas de Stochastic K y D.

    Solamente utiliza velas cerradas.
    """

    if len(candles) < max(
        k_period + d_period + slowing,
        5,
    ):
        return [], []

    highs = [
        _ohlc(c)[3]
        for c in candles
    ]

    lows = [
        _ohlc(c)[2]
        for c in candles
    ]

    closes = [
        _ohlc(c)[1]
        for c in candles
    ]

    raw_k: List[float] = []

    for i in range(
        k_period - 1,
        len(candles),
    ):
        window_high = max(
            highs[i - k_period + 1:i + 1]
        )

        window_low = min(
            lows[i - k_period + 1:i + 1]
        )

        span = window_high - window_low

        if span <= 0:
            raw_k.append(50.0)
        else:
            raw_k.append(
                100.0
                * (closes[i] - window_low)
                / span
            )

    if not raw_k:
        return [], []

    smoothed_k: List[float] = []

    for i in range(len(raw_k)):
        start = max(
            0,
            i - slowing + 1,
        )

        window = raw_k[start:i + 1]

        smoothed_k.append(
            sum(window) / len(window)
        )

    d_values: List[float] = []

    for i in range(len(smoothed_k)):
        start = max(
            0,
            i - d_period + 1,
        )

        window = smoothed_k[start:i + 1]

        d_values.append(
            sum(window) / len(window)
        )

    return (
        [
            max(0.0, min(100.0, value))
            for value in smoothed_k
        ],
        [
            max(0.0, min(100.0, value))
            for value in d_values
        ],
    )


def _stochastic(
    candles: List[Dict[str, Any]],
    k_period: int = STOCH_K_PERIOD,
    d_period: int = STOCH_D_PERIOD,
    slowing: int = STOCH_SMOOTHING,
) -> Tuple[float, float]:

    k_values, d_values = _stochastic_series(
        candles,
        k_period,
        d_period,
        slowing,
    )

    if not k_values or not d_values:
        return 50.0, 50.0

    return (
        k_values[-1],
        d_values[-1],
    )


def _stochastic_expansion(
    candles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Detecta expansión del Stochastic.

    CALL:
        K > D
        K está subiendo
        separación K-D está aumentando
        separación actual es la máxima de la ventana

    PUT:
        K < D
        K está bajando
        separación D-K está aumentando
        separación actual es la máxima de la ventana
    """

    k_values, d_values = _stochastic_series(
        candles
    )

    minimum = STOCH_EXPANSION_LOOKBACK + 2

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

    start = max(
        0,
        len(k_values)
        - STOCH_EXPANSION_LOOKBACK,
    )

    recent_separations = [
        abs(k_values[i] - d_values[i])
        for i in range(
            start,
            len(k_values),
        )
    ]

    maximum_separation = max(
        recent_separations
    )

    k_change = current_k - previous_k
    d_change = current_d - previous_d

    expanding = (
        current_separation
        > previous_separation
    )

    maximum = (
        current_separation
        >= maximum_separation - 0.000001
    )

    bullish = (
        current_k > current_d
        and k_change > 0
        and expanding
        and maximum
    )

    bearish = (
        current_k < current_d
        and k_change < 0
        and expanding
        and maximum
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
    }


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


def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
    stochastic_k: float = 50.0,
    stochastic_d: float = 50.0,
) -> Signal:

    records = _as_records(candles)

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
        close_price - open_price
    )

    candle_range = max(
        high - low,
        1e-12,
    )

    upper_wick = max(
        high - max(
            open_price,
            close_price,
        ),
        0.0,
    )

    lower_wick = max(
        min(
            open_price,
            close_price,
        ) - low,
        0.0,
    )

    atr = _atr(previous)

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

    near_support = (
        low <= support + tolerance
        and close_price > support
    )

    near_resistance = (
        high >= resistance - tolerance
        and close_price < resistance
    )

    stoch = _stochastic_expansion(
        records
    )

    # ==========================================================
    # CALL
    # ==========================================================

    bullish = (
        stoch["valid"]
        and stoch["direction"] == "call"
        and near_support
        and lower_wick >= max(
            body * 1.25,
            atr * 0.20,
        )
        and close_price > open_price
        and (
            (close_price - low)
            / candle_range
        ) >= 0.60
    )

    # ==========================================================
    # PUT
    # ==========================================================

    bearish = (
        stoch["valid"]
        and stoch["direction"] == "put"
        and near_resistance
        and upper_wick >= max(
            body * 1.25,
            atr * 0.20,
        )
        and close_price < open_price
        and (
            (high - close_price)
            / candle_range
        ) >= 0.60
    )

    if bullish:
        score = 90

        # Mayor separación = mayor puntuación.
        if stoch["separation"] >= 5:
            score += 3

        if stoch["separation"] >= 10:
            score += 3

        if stoch["separation"] >= 15:
            score += 4

        if lower_wick >= body * 2:
            score += 3

        if (
            close_price
            > support + tolerance * 0.25
        ):
            score += 2

        if score >= int(min_score):
            return Signal(
                "call",
                min(score, 100),
                "bullish_stochastic_expansion_support_rejection",
                support,
                resistance,
            )

    if bearish:
        score = 90

        if stoch["separation"] >= 5:
            score += 3

        if stoch["separation"] >= 10:
            score += 3

        if stoch["separation"] >= 15:
            score += 4

        if upper_wick >= body * 2:
            score += 3

        if (
            close_price
            < resistance - tolerance * 0.25
        ):
            score += 2

        if score >= int(min_score):
            return Signal(
                "put",
                min(score, 100),
                "bearish_stochastic_expansion_resistance_rejection",
                support,
                resistance,
            )

    return Signal(
        "none",
        0,
        "no_valid_stochastic_expansion",
        support,
        resistance,
    )


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    **_: Any,
) -> Dict[str, Any]:

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

            records.append(
                current_closed
            )

    elif df is not None:

        all_records = _as_records(df)

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

    result = _empty_result()

    if len(records) < MIN_CANDLES:
        result["reason"] = (
            "insufficient_candles"
        )
        return result

    stochastic_k, stochastic_d = _stochastic(
        records
    )

    stochastic_expansion = (
        _stochastic_expansion(records)
    )

    signal = analyze_rejection(
        records,
        min_score=min_score,
        stochastic_k=stochastic_k,
        stochastic_d=stochastic_d,
    )

    records_last = records[-1]

    timestamp = records_last.get(
        "from",
        records_last.get("timestamp"),
    )

    atr = (
        _atr(records[:-1])
        if len(records) > 1
        else 0.0
    )

    direction = (
        "bullish"
        if signal.action == "call"
        else
        "bearish"
        if signal.action == "put"
        else
        "range"
    )

    force = (
        signal.action in {"call", "put"}
        and signal.score >= int(min_score)
    )

    analysis = {
        "force": force,
        "structure": direction,
        "atr": atr,
        "support": signal.support,
        "resistance": signal.resistance,
        "tolerance": atr * 0.35,
        "entry_quality": signal.score,

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

        "last_swing_high": (
            signal.resistance
        ),

        "last_swing_low": (
            signal.support
        ),

        "rejection_timestamp": timestamp,

        "confirmation_timestamp": timestamp,

        "rejection_candle": records_last,

        "confirmation_candle": records_last,

        "pullback": {
            "valid": force,
            "previous_candle_confirmed": force,
            "extreme_confirmed": force,
        },
    }

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
            f"SEP={stochastic_expansion.get('separation', 0.0):.2f}"
        ),

        "analysis": analysis,

        "candle_timestamp": timestamp,

        "pair": pair,
    }


def get_signal(
    df: Any,
) -> Optional[str]:
    return analyze_market(
        df=df
    ).get("signal")


def signal(
    df: Any,
) -> Optional[str]:
    return get_signal(df)
