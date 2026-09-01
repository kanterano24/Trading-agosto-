from __future__ import annotations

import math
from typing import Any, Dict, Optional

import pandas as pd


# ============================================================
# ESTRATEGIA
# ============================================================
# M1 - DIVERGENCIA RSI ESTRUCTURAL
#
# La decisión se toma exclusivamente con la vela N cerrada.
#
# N:
#   - Apertura
#   - Máximo
#   - Mínimo
#   - Cierre
#   - Cuerpo
#   - Mechas
#   - Posición del cierre
#   - Estructura
#   - RSI
#   - Pivotes
#   - Divergencia
#   - Descanso
#   - Recuperación / rechazo
#   - Dominancia
#
# N+1:
#   - NO participa en el análisis.
#   - Solo se utiliza para ejecutar la operación.
#
# Señales:
#   CALL = reversión alcista
#   PUT  = reversión bajista
# ============================================================


# ============================================================
# CONFIGURACIÓN
# ============================================================

RSI_PERIOD = 14

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

MIN_HISTORY = 25

# Cuerpo
DOJI_BODY_RATIO = 0.10
INDECISION_BODY_RATIO = 0.25
REST_BODY_RATIO = 0.35
STRONG_BODY_RATIO = 0.55
FORCE_BODY_RATIO = 0.65

# Mechas
REJECTION_WICK_BODY_RATIO = 1.20
STRONG_REJECTION_WICK_BODY_RATIO = 1.50

# Divergencia
MIN_RSI_DIFFERENCE = 3.0

# Score mínimo para señal
MIN_SIGNAL_SCORE = 65


# ============================================================
# UTILIDADES
# ============================================================

def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None

        result = float(value)

        if not math.isfinite(result):
            return None

        return result

    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _get_ohlc(candle: Any) -> Optional[Dict[str, float]]:
    if candle is None:
        return None

    try:
        if isinstance(candle, pd.Series):
            candle = candle.to_dict()

        if not isinstance(candle, dict):
            return None

        open_price = _to_float(
            candle.get("open", candle.get("o"))
        )

        high_price = _to_float(
            candle.get(
                "high",
                candle.get(
                    "max",
                    candle.get("h")
                )
            )
        )

        low_price = _to_float(
            candle.get(
                "low",
                candle.get(
                    "min",
                    candle.get("l")
                )
            )
        )

        close_price = _to_float(
            candle.get("close", candle.get("c"))
        )

        if None in (
            open_price,
            high_price,
            low_price,
            close_price,
        ):
            return None

        if high_price < low_price:
            return None

        return {
            "open": float(open_price),
            "high": float(high_price),
            "low": float(low_price),
            "close": float(close_price),
        }

    except Exception:
        return None


def _normalize_history(
    previous_m1: Any,
) -> pd.DataFrame:

    if previous_m1 is None:
        return pd.DataFrame(
            columns=[
                "from",
                "open",
                "high",
                "low",
                "close",
            ]
        )

    try:

        if isinstance(previous_m1, pd.DataFrame):
            df = previous_m1.copy()

        elif isinstance(previous_m1, (list, tuple)):
            rows = []

            for candle in previous_m1:
                values = _get_ohlc(candle)

                if values:
                    row = dict(values)

                    if isinstance(candle, dict):
                        if "from" in candle:
                            row["from"] = candle["from"]

                    rows.append(row)

            df = pd.DataFrame(rows)

        else:
            df = pd.DataFrame(previous_m1)

    except Exception:
        return pd.DataFrame(
            columns=[
                "from",
                "open",
                "high",
                "low",
                "close",
            ]
        )

    if df.empty:
        return df

    rename_map = {
        "max": "high",
        "min": "low",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
    }

    df = df.rename(columns=rename_map)

    required = [
        "open",
        "high",
        "low",
        "close",
    ]

    if not all(col in df.columns for col in required):
        return pd.DataFrame()

    for col in required:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df.dropna(
        subset=required,
        inplace=True
    )

    if "from" in df.columns:
        df["from"] = pd.to_numeric(
            df["from"],
            errors="coerce"
        )

        df.dropna(
            subset=["from"],
            inplace=True
        )

        df["from"] = df["from"].astype(int)

        df = (
            df
            .drop_duplicates(
                "from",
                keep="last"
            )
            .sort_values("from")
        )

    return df.reset_index(drop=True)


# ============================================================
# RSI
# ============================================================

def calculate_rsi(
    closes: pd.Series,
    period: int = RSI_PERIOD,
) -> pd.Series:

    closes = pd.to_numeric(
        closes,
        errors="coerce"
    )

    delta = closes.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        float("nan")
    )

    rsi = 100 - (
        100 / (1 + rs)
    )

    # Casos extremos
    rsi = rsi.where(
        avg_loss != 0,
        100.0
    )

    rsi = rsi.where(
        avg_gain != 0,
        0.0
    )

    return rsi


# ============================================================
# ESTRUCTURA DE PIVOTES
# ============================================================

def detect_pivots(
    df: pd.DataFrame,
) -> tuple[list[int], list[int]]:

    highs: list[int] = []
    lows: list[int] = []

    if df is None or len(df) < (
        PIVOT_LEFT + PIVOT_RIGHT + 1
    ):
        return highs, lows

    high_values = df["high"].tolist()
    low_values = df["low"].tolist()

    start = PIVOT_LEFT
    end = len(df) - PIVOT_RIGHT

    for i in range(start, end):

        current_high = high_values[i]
        current_low = low_values[i]

        left_highs = high_values[
            i - PIVOT_LEFT:i
        ]

        right_highs = high_values[
            i + 1:i + 1 + PIVOT_RIGHT
        ]

        left_lows = low_values[
            i - PIVOT_LEFT:i
        ]

        right_lows = low_values[
            i + 1:i + 1 + PIVOT_RIGHT
        ]

        if (
            current_high >= max(left_highs)
            and current_high >= max(right_highs)
        ):
            highs.append(i)

        if (
            current_low <= min(left_lows)
            and current_low <= min(right_lows)
        ):
            lows.append(i)

    return highs, lows


# ============================================================
# ESTRUCTURA DEL MERCADO
# ============================================================

def analyze_structure(
    df: pd.DataFrame,
) -> Dict[str, Any]:

    result = {
        "trend": "NEUTRAL",
        "last_high": None,
        "previous_high": None,
        "last_low": None,
        "previous_low": None,
        "higher_high": False,
        "lower_high": False,
        "higher_low": False,
        "lower_low": False,
        "bullish_structure": False,
        "bearish_structure": False,
    }

    if df is None or len(df) < 8:
        return result

    highs, lows = detect_pivots(df)

    if len(highs) >= 2:

        previous_high_idx = highs[-2]
        last_high_idx = highs[-1]

        previous_high = float(
            df.iloc[previous_high_idx]["high"]
        )

        last_high = float(
            df.iloc[last_high_idx]["high"]
        )

        result["previous_high"] = previous_high
        result["last_high"] = last_high

        result["higher_high"] = (
            last_high > previous_high
        )

        result["lower_high"] = (
            last_high < previous_high
        )

    if len(lows) >= 2:

        previous_low_idx = lows[-2]
        last_low_idx = lows[-1]

        previous_low = float(
            df.iloc[previous_low_idx]["low"]
        )

        last_low = float(
            df.iloc[last_low_idx]["low"]
        )

        result["previous_low"] = previous_low
        result["last_low"] = last_low

        result["higher_low"] = (
            last_low > previous_low
        )

        result["lower_low"] = (
            last_low < previous_low
        )

    bullish = (
        result["higher_high"]
        and result["higher_low"]
    )

    bearish = (
        result["lower_high"]
        and result["lower_low"]
    )

    result["bullish_structure"] = bullish
    result["bearish_structure"] = bearish

    if bullish:
        result["trend"] = "BULLISH"

    elif bearish:
        result["trend"] = "BEARISH"

    else:
        # Determinación secundaria
        # mediante posición relativa de cierres.

        first_close = float(
            df["close"].iloc[-6]
        )

        last_close = float(
            df["close"].iloc[-1]
        )

        if last_close > first_close:
            result["trend"] = "BULLISH"

        elif last_close < first_close:
            result["trend"] = "BEARISH"

    return result


# ============================================================
# ANÁLISIS DE VELA
# ============================================================

def analyze_candle(
    candle: Dict[str, float],
) -> Dict[str, Any]:

    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    candle_range = max(
        h - l,
        0.0
    )

    body = abs(c - o)

    if candle_range > 0:
        body_ratio = body / candle_range
        close_position = (
            (c - l) / candle_range
        )
    else:
        body_ratio = 0.0
        close_position = 0.5

    upper_wick = max(
        h - max(o, c),
        0.0
    )

    lower_wick = max(
        min(o, c) - l,
        0.0
    )

    if c > o:
        direction = "BULLISH"

    elif c < o:
        direction = "BEARISH"

    else:
        direction = "NEUTRAL"

    doji = (
        body_ratio <= DOJI_BODY_RATIO
    )

    indecision = (
        body_ratio <= INDECISION_BODY_RATIO
    )

    rest = (
        not doji
        and body_ratio <= REST_BODY_RATIO
    )

    strong = (
        body_ratio >= STRONG_BODY_RATIO
    )

    force = (
        body_ratio >= FORCE_BODY_RATIO
    )

    bullish_rejection = (
        lower_wick >= body * REJECTION_WICK_BODY_RATIO
        and close_position >= 0.50
    )

    bearish_rejection = (
        upper_wick >= body * REJECTION_WICK_BODY_RATIO
        and close_position <= 0.50
    )

    strong_bullish_rejection = (
        lower_wick >= body * STRONG_REJECTION_WICK_BODY_RATIO
        and close_position >= 0.55
    )

    strong_bearish_rejection = (
        upper_wick >= body * STRONG_REJECTION_WICK_BODY_RATIO
        and close_position <= 0.45
    )

    if doji:
        state = "DOJI"

    elif rest:
        state = "DESCANSO"

    elif force:
        state = "FUERZA"

    elif strong:
        state = "MOVIMIENTO FUERTE"

    elif indecision:
        state = "INDECISIÓN"

    else:
        state = "MOVIMIENTO"

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": candle_range,
        "body": body,
        "body_ratio": body_ratio,
        "body_percent": body_ratio * 100.0,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "upper_wick_percent": (
            upper_wick / candle_range * 100
            if candle_range > 0 else 0
        ),
        "lower_wick_percent": (
            lower_wick / candle_range * 100
            if candle_range > 0 else 0
        ),
        "close_position": close_position,
        "close_position_percent": (
            close_position * 100
        ),
        "direction": direction,
        "doji": doji,
        "indecision": indecision,
        "rest": rest,
        "strong": strong,
        "force": force,
        "bullish_rejection": bullish_rejection,
        "bearish_rejection": bearish_rejection,
        "strong_bullish_rejection": strong_bullish_rejection,
        "strong_bearish_rejection": strong_bearish_rejection,
        "state": state,
    }


# ============================================================
# DIVERGENCIA RSI
# ============================================================

def find_structural_divergence(
    df: pd.DataFrame,
    current_candle: Dict[str, float],
) -> Dict[str, Any]:

    result: Dict[str, Any] = {
        "bullish": False,
        "bearish": False,
        "previous_low": None,
        "current_low": None,
        "previous_low_rsi": None,
        "current_low_rsi": None,
        "previous_high": None,
        "current_high": None,
        "previous_high_rsi": None,
        "current_high_rsi": None,
        "bull_rsi_difference": 0.0,
        "bear_rsi_difference": 0.0,
        "bullish_score": 0,
        "bearish_score": 0,
    }

    if df is None or len(df) < MIN_HISTORY:
        return result

    work = df.copy()

    current = _get_ohlc(
        current_candle
    )

    if current is None:
        return result

    # Agregamos la vela N al final.
    current_row = pd.DataFrame(
        [current]
    )

    work = pd.concat(
        [
            work[
                [
                    "open",
                    "high",
                    "low",
                    "close",
                ]
            ],
            current_row,
        ],
        ignore_index=True,
    )

    if len(work) < MIN_HISTORY:
        return result

    work["rsi"] = calculate_rsi(
        work["close"]
    )

    highs, lows = detect_pivots(
        work.iloc[:-1].copy()
    )

    # --------------------------------------------------------
    # DIVERGENCIA ALCISTA
    # --------------------------------------------------------
    #
    # Precio:
    #   mínimo actual inferior al mínimo anterior.
    #
    # RSI:
    #   RSI actual superior al RSI anterior.
    #
    # La vela N también debe mostrar capacidad
    # de recuperación desde mínimos.
    # --------------------------------------------------------

    previous_low_idx = (
        lows[-1]
        if lows
        else None
    )

    if previous_low_idx is not None:

        previous_low = float(
            work.iloc[previous_low_idx]["low"]
        )

        previous_low_rsi = _to_float(
            work.iloc[previous_low_idx]["rsi"]
        )

        current_low = float(
            current["low"]
        )

        current_rsi = _to_float(
            work.iloc[-1]["rsi"]
        )

        if (
            previous_low_rsi is not None
            and current_rsi is not None
        ):

            rsi_difference = (
                current_rsi
                - previous_low_rsi
            )

            price_lower_low = (
                current_low
                < previous_low
            )

            rsi_higher_low = (
                current_rsi
                > previous_low_rsi
            )

            if (
                price_lower_low
                and rsi_higher_low
                and rsi_difference
                >= MIN_RSI_DIFFERENCE
            ):

                result["bullish"] = True

                result["previous_low"] = previous_low
                result["current_low"] = current_low
                result["previous_low_rsi"] = previous_low_rsi
                result["current_low_rsi"] = current_rsi
                result["bull_rsi_difference"] = rsi_difference

                score = 60

                if rsi_difference >= 5:
                    score += 10

                if rsi_difference >= 8:
                    score += 10

                result["bullish_score"] = min(
                    score,
                    100
                )

    # --------------------------------------------------------
    # DIVERGENCIA BAJISTA
    # --------------------------------------------------------

    previous_high_idx = (
        highs[-1]
        if highs
        else None
    )

    if previous_high_idx is not None:

        previous_high = float(
            work.iloc[previous_high_idx]["high"]
        )

        previous_high_rsi = _to_float(
            work.iloc[previous_high_idx]["rsi"]
        )

        current_high = float(
            current["high"]
        )

        current_rsi = _to_float(
            work.iloc[-1]["rsi"]
        )

        if (
            previous_high_rsi is not None
            and current_rsi is not None
        ):

            rsi_difference = (
                previous_high_rsi
                - current_rsi
            )

            price_higher_high = (
                current_high
                > previous_high
            )

            rsi_lower_high = (
                current_rsi
                < previous_high_rsi
            )

            if (
                price_higher_high
                and rsi_lower_high
                and rsi_difference
                >= MIN_RSI_DIFFERENCE
            ):

                result["bearish"] = True

                result["previous_high"] = previous_high
                result["current_high"] = current_high
                result["previous_high_rsi"] = previous_high_rsi
                result["current_high_rsi"] = current_rsi
                result["bear_rsi_difference"] = rsi_difference

                score = 60

                if rsi_difference >= 5:
                    score += 10

                if rsi_difference >= 8:
                    score += 10

                result["bearish_score"] = min(
                    score,
                    100
                )

    return result


# ============================================================
# CONDICIONES DE CONFIRMACIÓN
# ============================================================

def evaluate_conditions(
    candle: Dict[str, Any],
    previous_candle: Optional[Dict[str, Any]],
    structure: Dict[str, Any],
    divergence: Dict[str, Any],
) -> Dict[str, Any]:

    conditions: Dict[str, Any] = {
        "previous_strong": False,
        "current_rest": False,
        "bull_recovery": False,
        "bear_recovery": False,
        "bull_dominance": False,
        "bear_dominance": False,
        "bull_structure": False,
        "bear_structure": False,
        "bull_rejection": False,
        "bear_rejection": False,
    }

    # --------------------------------------------------------
    # VELA ANTERIOR
    # --------------------------------------------------------

    if previous_candle:

        previous_range = max(
            previous_candle["high"]
            - previous_candle["low"],
            0.0
        )

        previous_body = abs(
            previous_candle["close"]
            - previous_candle["open"]
        )

        previous_body_ratio = (
            previous_body / previous_range
            if previous_range > 0
            else 0
        )

        conditions["previous_strong"] = (
            previous_body_ratio
            >= STRONG_BODY_RATIO
        )

    # --------------------------------------------------------
    # VELA DE DESCANSO
    # --------------------------------------------------------

    conditions["current_rest"] = bool(
        candle["rest"]
        or candle["indecision"]
    )

    # --------------------------------------------------------
    # RECHAZO
    # --------------------------------------------------------

    conditions["bull_rejection"] = bool(
        candle["bullish_rejection"]
    )

    conditions["bear_rejection"] = bool(
        candle["bearish_rejection"]
    )

    # --------------------------------------------------------
    # RECUPERACIÓN
    # --------------------------------------------------------

    conditions["bull_recovery"] = bool(
        (
            candle["direction"] == "BULLISH"
            and candle["close_position"] >= 0.55
        )
        or candle["strong_bullish_rejection"]
    )

    conditions["bear_recovery"] = bool(
        (
            candle["direction"] == "BEARISH"
            and candle["close_position"] <= 0.45
        )
        or candle["strong_bearish_rejection"]
    )

    # --------------------------------------------------------
    # DOMINANCIA
    # --------------------------------------------------------

    conditions["bull_dominance"] = bool(
        candle["direction"] == "BULLISH"
        and candle["close_position"] >= 0.60
    )

    conditions["bear_dominance"] = bool(
        candle["direction"] == "BEARISH"
        and candle["close_position"] <= 0.40
    )

    # --------------------------------------------------------
    # ESTRUCTURA
    # --------------------------------------------------------

    conditions["bull_structure"] = bool(
        structure.get("bullish_structure")
        or structure.get("trend") == "BULLISH"
    )

    conditions["bear_structure"] = bool(
        structure.get("bearish_structure")
        or structure.get("trend") == "BEARISH"
    )

    return conditions


# ============================================================
# SCORE
# ============================================================

def calculate_signal_score(
    direction: str,
    divergence: Dict[str, Any],
    conditions: Dict[str, Any],
    candle: Dict[str, Any],
) -> int:

    if direction == "call":

        score = int(
            divergence.get(
                "bullish_score",
                0
            )
        )

        if conditions.get(
            "current_rest"
        ):
            score += 5

        if conditions.get(
            "bull_recovery"
        ):
            score += 10

        if conditions.get(
            "bull_rejection"
        ):
            score += 5

        if conditions.get(
            "bull_dominance"
        ):
            score += 5

        if conditions.get(
            "bull_structure"
        ):
            score += 5

        if conditions.get(
            "previous_strong"
        ):
            score += 5

        if candle.get(
            "strong_bullish_rejection"
        ):
            score += 5

        return min(
            score,
            100
        )

    if direction == "put":

        score = int(
            divergence.get(
                "bearish_score",
                0
            )
        )

        if conditions.get(
            "current_rest"
        ):
            score += 5

        if conditions.get(
            "bear_recovery"
        ):
            score += 10

        if conditions.get(
            "bear_rejection"
        ):
            score += 5

        if conditions.get(
            "bear_dominance"
        ):
            score += 5

        if conditions.get(
            "bear_structure"
        ):
            score += 5

        if conditions.get(
            "previous_strong"
        ):
            score += 5

        if candle.get(
            "strong_bearish_rejection"
        ):
            score += 5

        return min(
            score,
            100
        )

    return 0


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def analyze_minute(
    candle_1m: Any,
    candles_5s: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    candle = _get_ohlc(
        candle_1m
    )

    if candle is None:

        return {
            "signal": None,
            "valid": False,
            "score": 0,
            "reason": "vela M1 inválida",
            "analysis": {},
        }

    candle_analysis = analyze_candle(
        candle
    )

    history = _normalize_history(
        previous_m1
    )

    # --------------------------------------------------------
    # Timestamp
    # --------------------------------------------------------

    minute_timestamp = None

    if isinstance(candle_1m, dict):

        minute_timestamp = (
            candle_1m.get("from")
            or candle_1m.get("timestamp")
            or candle_1m.get("time")
        )

    if minute_timestamp is not None:
        minute_timestamp = _safe_int(
            minute_timestamp
        )

    # --------------------------------------------------------
    # Vela anterior
    # --------------------------------------------------------

    previous_candle = None

    if (
        history is not None
        and not history.empty
    ):

        previous_row = history.iloc[-1]

        previous_candle = {
            "open": float(
                previous_row["open"]
            ),
            "high": float(
                previous_row["high"]
            ),
            "low": float(
                previous_row["low"]
            ),
            "close": float(
                previous_row["close"]
            ),
        }

    # --------------------------------------------------------
    # Estructura
    # --------------------------------------------------------

    structure = analyze_structure(
        history
    )

    # --------------------------------------------------------
    # Divergencia
    # --------------------------------------------------------

    divergence = find_structural_divergence(
        history,
        candle,
    )

    # --------------------------------------------------------
    # Condiciones
    # --------------------------------------------------------

    conditions = evaluate_conditions(
        candle_analysis,
        previous_candle,
        structure,
        divergence,
    )

    # --------------------------------------------------------
    # Determinar señal
    # --------------------------------------------------------

    bullish_divergence = bool(
        divergence.get("bullish")
    )

    bearish_divergence = bool(
        divergence.get("bearish")
    )

    signal = None
    score = 0

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if bullish_divergence:

        score = calculate_signal_score(
            "call",
            divergence,
            conditions,
            candle_analysis,
        )

        # La divergencia es obligatoria.
        #
        # Además buscamos confirmación de recuperación
        # o rechazo desde mínimos.

        confirmation = (
            conditions.get(
                "bull_recovery"
            )
            or conditions.get(
                "bull_rejection"
            )
        )

        if (
            confirmation
            and score >= MIN_SIGNAL_SCORE
        ):
            signal = "call"

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    elif bearish_divergence:

        score = calculate_signal_score(
            "put",
            divergence,
            conditions,
            candle_analysis,
        )

        confirmation = (
            conditions.get(
                "bear_recovery"
            )
            or conditions.get(
                "bear_rejection"
            )
        )

        if (
            confirmation
            and score >= MIN_SIGNAL_SCORE
        ):
            signal = "put"

    # --------------------------------------------------------
    # RAZÓN
    # --------------------------------------------------------

    if signal == "call":

        reason = (
            "CALL confirmada | "
            "divergencia RSI alcista + "
            "rechazo/recuperación + "
            f"score {score}/100"
        )

        state = "REVERSIÓN ALCISTA"

    elif signal == "put":

        reason = (
            "PUT confirmada | "
            "divergencia RSI bajista + "
            "rechazo/recuperación + "
            f"score {score}/100"
        )

        state = "REVERSIÓN BAJISTA"

    elif bullish_divergence:

        reason = (
            "Divergencia alcista detectada "
            "pero sin confirmación suficiente"
        )

        state = "DIVERGENCIA ALCISTA"

    elif bearish_divergence:

        reason = (
            "Divergencia bajista detectada "
            "pero sin confirmación suficiente"
        )

        state = "DIVERGENCIA BAJISTA"

    elif candle_analysis["doji"]:

        reason = "Sin señal: DOJI"

        state = "DOJI"

    elif candle_analysis["rest"]:

        reason = "Sin señal: vela de descanso"

        state = "DESCANSO"

    else:

        reason = (
            "Sin divergencia estructural "
            "confirmada"
        )

        state = candle_analysis["state"]

    # --------------------------------------------------------
    # RSI ACTUAL
    # --------------------------------------------------------

    current_rsi = None

    try:

        if history is not None and not history.empty:

            temp = history.copy()

            temp = pd.concat(
                [
                    temp[
                        [
                            "open",
                            "high",
                            "low",
                            "close",
                        ]
                    ],
                    pd.DataFrame(
                        [candle]
                    ),
                ],
                ignore_index=True,
            )

            rsi_series = calculate_rsi(
                temp["close"]
            )

            if not rsi_series.empty:
                current_rsi = _to_float(
                    rsi_series.iloc[-1]
                )

    except Exception:
        current_rsi = None

    # --------------------------------------------------------
    # RESULTADO
    # --------------------------------------------------------

    result = {
        "signal": signal,
        "valid": signal in (
            "call",
            "put",
        ),
        "score": int(score),
        "reason": reason,
        "minute_timestamp": minute_timestamp,
        "minute_open": candle["open"],
        "minute_close": candle["close"],
        "high": candle["high"],
        "low": candle["low"],
        "range": candle_analysis["range"],
        "body": candle_analysis["body"],
        "body_ratio": candle_analysis["body_ratio"],
        "upper_wick": candle_analysis["upper_wick"],
        "lower_wick": candle_analysis["lower_wick"],
        "close_position": candle_analysis["close_position"],
        "direction": candle_analysis["direction"],
        "state": state,
        "analysis": {
            "pair": pair,
            "candle": {
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "body": candle_analysis["body"],
                "body_percent": candle_analysis[
                    "body_percent"
                ],
                "upper_wick": candle_analysis[
                    "upper_wick"
                ],
                "lower_wick": candle_analysis[
                    "lower_wick"
                ],
                "close_position": candle_analysis[
                    "close_position_percent"
                ],
                "direction": candle_analysis[
                    "direction"
                ],
                "state": candle_analysis[
                    "state"
                ],
            },
            "rsi": {
                "current": current_rsi,
                "period": RSI_PERIOD,
            },
            "structure": structure,
            "divergence": divergence,
            "conditions": conditions,
        },
        "quality_checks": {
            "bullish_divergence": bullish_divergence,
            "bearish_divergence": bearish_divergence,
            "bull_recovery": conditions.get(
                "bull_recovery"
            ),
            "bear_recovery": conditions.get(
                "bear_recovery"
            ),
            "bull_rejection": conditions.get(
                "bull_rejection"
            ),
            "bear_rejection": conditions.get(
                "bear_rejection"
            ),
            "bull_structure": conditions.get(
                "bull_structure"
            ),
            "bear_structure": conditions.get(
                "bear_structure"
            ),
            "current_rest": conditions.get(
                "current_rest"
            ),
        },
    }

    return result


# ============================================================
# COMPATIBILIDAD CON BOT.PY
# ============================================================

def analyze_market(
    candle_1m: Any,
    candles_5s: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    return analyze_minute(
        candle_1m=candle_1m,
        candles_5s=candles_5s,
        previous_m1=previous_m1,
        pair=pair,
    )


def analyze_live_candle(
    candle_1m: Any,
    candles_5s: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    """
    Alias de compatibilidad.

    Evita:

    ImportError:
    cannot import name 'analyze_live_candle'
    from 'strategy'
    """

    return analyze_market(
        candle_1m=candle_1m,
        candles_5s=candles_5s,
        previous_m1=previous_m1,
        pair=pair,
    )


def build_n1_signal(
    candle_1m: Any,
    previous_m1: Any = None,
    candles_5s: Any = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    return analyze_market(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        candles_5s=candles_5s,
        pair=pair,
    )


def get_m1_direction(
    candle: Any,
) -> str:

    values = _get_ohlc(candle)

    if values is None:
        return "NEUTRAL"

    if values["close"] > values["open"]:
        return "BULLISH"

    if values["close"] < values["open"]:
        return "BEARISH"

    return "NEUTRAL"


def get_signal(
    candle_1m: Any,
    candles_5s: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Optional[str]:

    result = analyze_market(
        candle_1m=candle_1m,
        candles_5s=candles_5s,
        previous_m1=previous_m1,
        pair=pair,
    )

    return result.get("signal")


def signal(
    candle_1m: Any,
    candles_5s: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
) -> Optional[str]:

    return get_signal(
        candle_1m=candle_1m,
        candles_5s=candles_5s,
        previous_m1=previous_m1,
        pair=pair,
    )


def check_pattern(
    candles_5s: Any = None,
) -> None:

    # Las velas de 5 segundos NO generan señales.
    return None


# ============================================================
# PRUEBA DIRECTA
# ============================================================

if __name__ == "__main__":

    print(
        "strategy.py cargado correctamente."
    )

    print(
        "Funciones disponibles:"
    )

    print(
        " - analyze_market"
    )

    print(
        " - analyze_live_candle"
    )

    print(
        " - analyze_minute"
    )

    print(
        " - build_n1_signal"
    )

    print(
        " - get_signal"
    )
