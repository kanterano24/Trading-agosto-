"""
strategy.py

ESTRATEGIA ESTRUCTURAL DE RECHAZO + CONTINUIDAD + DESCANSO
+ FUERZA + INDECISIÓN + DIVERGENCIA RSI
PARA BINARY OTC M1.

OBJETIVO PRINCIPAL
------------------

Evitar entradas tardías después de que el precio ya haya recorrido
gran parte del impulso.

La estrategia analiza:

1. Estructura del mercado.
2. Últimos máximos y mínimos confirmados.
3. HH/HL o LH/LL.
4. Consolidación previa.
5. Inicio del impulso.
6. Edad del impulso.
7. Extensión del impulso en ATR.
8. Agotamiento.
9. Rechazo estructural.
10. Continuidad.
11. Descanso.
12. Indecisión.
13. Fuerza.
14. Divergencia RSI.
15. Distancia a soporte/resistencia.
16. Distancia a tendencia dinámica.

IMPORTANTE
----------

La vela N es la vela analizada.

La estrategia NO utiliza N+1 para decidir.

Cuando una configuración de N está preparada para N+1,
el bot puede ejecutar en N+1 mediante su flujo normal.

No ejecuta operaciones.
No decide expiración.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import math

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

MIN_BARS = 35
MAX_CANDLES = 80

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14


# ============================================================
# PIVOTES / ESTRUCTURA
# ============================================================

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

SWING_LOOKBACK = 35

MIN_STRUCTURE_GAP_ATR = 0.05


# ============================================================
# ZONAS
# ============================================================

ZONE_ATR = 0.28

MAX_ENTRY_DISTANCE_ATR = 0.55

MIN_ROOM_TO_OPPOSITE_ATR = 0.70


# ============================================================
# RECHAZO
# ============================================================

MIN_BODY_RATIO = 0.25

MIN_REJECTION_WICK_RATIO = 0.35

MIN_WICK_BODY_RATIO = 1.15

MIN_CLOSE_POSITION_CALL = 0.62

MAX_CLOSE_POSITION_PUT = 0.38


# ============================================================
# CUERPO DE VELA
# ============================================================

MIN_BODY_ATR = 0.12

MAX_BODY_ATR = 1.35


# ============================================================
# IMPULSO
# ============================================================

IMPULSE_LOOKBACK = 12

MAX_IMPULSE_AGE = 5

MAX_IMPULSE_TOTAL_ATR = 3.20

MAX_CONSECUTIVE_DIRECTION_CANDLES = 5

MIN_IMPULSE_BODY_RATIO = 0.45

MIN_IMPULSE_BODY_ATR = 0.35

BREAKOUT_LOOKBACK = 5


# ============================================================
# CONSOLIDACIÓN
# ============================================================

CONSOLIDATION_LOOKBACK = 8

MIN_CONSOLIDATION_CANDLES = 4

MAX_CONSOLIDATION_RANGE_ATR = 2.20

MAX_CONSOLIDATION_DRIFT_ATR = 1.20


# ============================================================
# RSI
# ============================================================

CALL_RSI_MIN = 38.0
CALL_RSI_MAX = 68.0

PUT_RSI_MIN = 32.0
PUT_RSI_MAX = 62.0


# ============================================================
# SCORE
# ============================================================

MIN_STRUCTURE_SCORE = 3

MIN_ENTRY_SCORE = 70


# ============================================================
# NUEVAS REGLAS
# ============================================================

# Distancia mínima para configuraciones que NO son rechazo.
MIN_DISTANCE_FROM_ZONE_ATR = 0.35

# Distancia mínima de la tendencia dinámica.
MIN_DISTANCE_FROM_TRENDLINE_ATR = 0.30


# ============================================================
# INDECISIÓN
# ============================================================

INDECISION_MAX_BODY_RATIO = 0.30

INDECISION_MIN_WICK_RATIO = 0.25


# ============================================================
# DESCANSO
# ============================================================

REST_MAX_BODY_RATIO = 0.50

REST_MIN_PREVIOUS_BODY_RATIO = 0.55


# ============================================================
# CONTINUIDAD
# ============================================================

CONTINUITY_MIN_BODY_RATIO = 0.45

CONTINUITY_MIN_BODY_ATR = 0.20


# ============================================================
# FUERZA
# ============================================================

FORCE_MIN_BODY_RATIO = 0.65

FORCE_MIN_BODY_ATR = 0.35


# ============================================================
# DIVERGENCIA
# ============================================================

DIVERGENCE_LOOKBACK = 25

DIVERGENCE_MIN_RSI_CHANGE = 2.0

DIVERGENCE_MIN_PRICE_CHANGE_ATR = 0.05


EPS = 1e-12


# ============================================================
# RESULTADO VACÍO
# ============================================================

def _empty_result(
    reason: str = "sin señal",
) -> Dict[str, Any]:

    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": None,
        "entry_type": None,
        "entry_quality": 0,
        "last_swing_high": None,
        "last_swing_low": None,
        "support": None,
        "resistance": None,
        "rsi": 50.0,
        "atr": 0.0,
        "candle_timestamp": None,
        "analysis": {},
    }


# ============================================================
# UTILIDADES
# ============================================================

def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:

    try:

        x = float(value)

        if math.isfinite(x):
            return x

    except Exception:
        pass

    return default


def _safe_int(
    value: Any,
    default: Optional[int] = None,
) -> Optional[int]:

    try:

        return int(float(value))

    except Exception:

        return default


# ============================================================
# NORMALIZACIÓN
# ============================================================

def _normalize(
    df: pd.DataFrame,
) -> pd.DataFrame:

    if (
        df is None
        or not isinstance(df, pd.DataFrame)
        or df.empty
    ):
        return pd.DataFrame()

    out = df.copy()

    rename: Dict[str, str] = {}

    if (
        "max" in out.columns
        and "high" not in out.columns
    ):
        rename["max"] = "high"

    if (
        "min" in out.columns
        and "low" not in out.columns
    ):
        rename["min"] = "low"

    rename.update(
        {
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
        }
    )

    out.rename(
        columns=rename,
        inplace=True,
    )

    required = [
        "open",
        "high",
        "low",
        "close",
    ]

    if any(
        c not in out.columns
        for c in required
    ):
        return pd.DataFrame()

    for col in required:

        out[col] = pd.to_numeric(
            out[col],
            errors="coerce",
        )

    if "from" in out.columns:

        out["from"] = pd.to_numeric(
            out["from"],
            errors="coerce",
        )

        out.dropna(
            subset=["from"],
            inplace=True,
        )

        out["from"] = (
            out["from"]
            .astype(int)
        )

        out.sort_values(
            "from",
            inplace=True,
        )

        out.drop_duplicates(
            subset=["from"],
            keep="last",
            inplace=True,
        )

    out.dropna(
        subset=required,
        inplace=True,
    )

    out.reset_index(
        drop=True,
        inplace=True,
    )

    if len(out) > MAX_CANDLES:

        out = (
            out
            .tail(MAX_CANDLES)
            .reset_index(drop=True)
        )

    return out


# ============================================================
# CONSTRUCCIÓN COMPATIBLE CON BOT.PY
# ============================================================

def _build_analysis_dataframe(
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:

    if df is not None:

        return _normalize(df)

    history = _normalize(
        previous_m1
        if isinstance(
            previous_m1,
            pd.DataFrame,
        )
        else pd.DataFrame()
    )

    if isinstance(
        candle_1m,
        pd.Series,
    ):

        current = candle_1m.to_dict()

    elif isinstance(
        candle_1m,
        dict,
    ):

        current = dict(candle_1m)

    else:

        current = None

    if current is None:
        return history

    current_df = pd.DataFrame(
        [current]
    )

    current_df = _normalize(
        current_df
    )

    if current_df.empty:
        return history

    combined = pd.concat(
        [
            history,
            current_df,
        ],
        ignore_index=True,
    )

    return _normalize(
        combined
    )


# ============================================================
# INDICADORES
# ============================================================

def add_indicators(
    df: pd.DataFrame,
) -> pd.DataFrame:

    out = _normalize(df)

    if out.empty:
        return out

    close = out["close"]
    high = out["high"]
    low = out["low"]

    out["ema9"] = (
        close
        .ewm(
            span=EMA_FAST,
            adjust=False,
        )
        .mean()
    )

    out["ema21"] = (
        close
        .ewm(
            span=EMA_MID,
            adjust=False,
        )
        .mean()
    )

    out["ema50"] = (
        close
        .ewm(
            span=EMA_SLOW,
            adjust=False,
        )
        .mean()
    )

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (
                high
                - previous_close
            ).abs(),
            (
                low
                - previous_close
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["tr"] = tr

    out["atr"] = (
        tr
        .rolling(
            ATR_PERIOD,
            min_periods=ATR_PERIOD,
        )
        .mean()
    )

    delta = close.diff()

    gain = delta.clip(
        lower=0.0
    )

    loss = -delta.clip(
        upper=0.0
    )

    avg_gain = (
        gain
        .ewm(
            alpha=1 / RSI_PERIOD,
            adjust=False,
            min_periods=RSI_PERIOD,
        )
        .mean()
    )

    avg_loss = (
        loss
        .ewm(
            alpha=1 / RSI_PERIOD,
            adjust=False,
            min_periods=RSI_PERIOD,
        )
        .mean()
    )

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan,
        )
    )

    out["rsi"] = (
        100.0
        - (
            100.0
            / (1.0 + rs)
        )
    )

    out.loc[
        (
            avg_loss == 0
        )
        & (
            avg_gain > 0
        ),
        "rsi",
    ] = 100.0

    out.loc[
        (
            avg_gain == 0
        )
        & (
            avg_loss > 0
        ),
        "rsi",
    ] = 0.0

    return out


def _atr(
    history: pd.DataFrame,
) -> float:

    if (
        history is None
        or history.empty
    ):
        return 0.0

    if "tr" in history.columns:

        value = (
            history["tr"]
            .tail(ATR_PERIOD)
            .mean()
        )

    else:

        value = np.nan

    if (
        pd.isna(value)
        or value <= 0
    ):

        value = (
            history["high"]
            - history["low"]
        ).tail(
            ATR_PERIOD
        ).mean()

    if (
        pd.isna(value)
        or value <= 0
    ):

        value = (
            abs(
                float(
                    history[
                        "close"
                    ].iloc[-1]
                )
            )
            * 0.0001
        )

    return float(
        max(
            value,
            EPS,
        )
    )


# ============================================================
# VELAS
# ============================================================

def candle_direction(
    candle: pd.Series,
) -> str:

    o = _safe_float(
        candle.get("open")
    )

    c = _safe_float(
        candle.get("close")
    )

    if c > o:
        return "bull"

    if c < o:
        return "bear"

    return "neutral"


def candle_metrics(
    candle: pd.Series,
) -> Dict[str, float]:

    o = _safe_float(
        candle.get("open")
    )

    h = _safe_float(
        candle.get("high")
    )

    l = _safe_float(
        candle.get("low")
    )

    c = _safe_float(
        candle.get("close")
    )

    rng = max(
        h - l,
        EPS,
    )

    body = abs(
        c - o
    )

    upper = max(
        h - max(o, c),
        0.0,
    )

    lower = max(
        min(o, c) - l,
        0.0,
    )

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "upper": upper,
        "lower": lower,
        "body_ratio": (
            body / rng
        ),
        "upper_ratio": (
            upper / rng
        ),
        "lower_ratio": (
            lower / rng
        ),
        "close_position": (
            (c - l) / rng
        ),
    }


# ============================================================
# PIVOTES CONFIRMADOS
# ============================================================

def _confirmed_swings(
    history: pd.DataFrame,
    left: int = PIVOT_LEFT,
    right: int = PIVOT_RIGHT,
) -> Tuple[
    list[Tuple[int, float]],
    list[Tuple[int, float]],
]:

    highs: list[
        Tuple[int, float]
    ] = []

    lows: list[
        Tuple[int, float]
    ] = []

    if (
        history is None
        or len(history)
        < left + right + 3
    ):
        return highs, lows

    start = max(
        left,
        len(history)
        - SWING_LOOKBACK,
    )

    end = (
        len(history)
        - right
    )

    for i in range(
        start,
        end,
    ):

        h = float(
            history[
                "high"
            ].iloc[i]
        )

        l = float(
            history[
                "low"
            ].iloc[i]
        )

        left_highs = (
            history[
                "high"
            ].iloc[
                i - left:i
            ]
        )

        right_highs = (
            history[
                "high"
            ].iloc[
                i + 1:
                i + right + 1
            ]
        )

        left_lows = (
            history[
                "low"
            ].iloc[
                i - left:i
            ]
        )

        right_lows = (
            history[
                "low"
            ].iloc[
                i + 1:
                i + right + 1
            ]
        )

        if (
            h >= float(
                left_highs.max()
            )
            and
            h >= float(
                right_highs.max()
            )
        ):

            highs.append(
                (i, h)
            )

        if (
            l <= float(
                left_lows.min()
            )
            and
            l <= float(
                right_lows.min()
            )
        ):

            lows.append(
                (i, l)
            )

    return highs, lows


def _last_swing_levels(
    history: pd.DataFrame,
) -> Dict[str, Any]:

    highs, lows = (
        _confirmed_swings(
            history
        )
    )

    last_high = (
        highs[-1]
        if highs
        else None
    )

    last_low = (
        lows[-1]
        if lows
        else None
    )

    w = history.tail(
        min(
            SWING_LOOKBACK,
            len(history),
        )
    )

    if (
        last_high is None
        and not w.empty
    ):

        idx = int(
            w["high"].idxmax()
        )

        last_high = (
            idx,
            float(
                w.loc[
                    idx,
                    "high",
                ]
            ),
        )

    if (
        last_low is None
        and not w.empty
    ):

        idx = int(
            w["low"].idxmin()
        )

        last_low = (
            idx,
            float(
                w.loc[
                    idx,
                    "low",
                ]
            ),
        )

    return {
        "highs": highs,
        "lows": lows,
        "last_high": last_high,
        "last_low": last_low,
    }


# ============================================================
# ESTRUCTURA
# ============================================================

def detect_structure(
    df: pd.DataFrame,
) -> str:

    work = _normalize(df)

    if len(work) < 12:
        return "range"

    swings = (
        _last_swing_levels(
            work
        )
    )

    highs = swings["highs"]
    lows = swings["lows"]

    if (
        len(highs) >= 2
        and len(lows) >= 2
    ):

        h1 = highs[-2][1]
        h2 = highs[-1][1]

        l1 = lows[-2][1]
        l2 = lows[-1][1]

        atr = _atr(
            add_indicators(
                work
            )
        )

        min_gap = max(
            atr
            * MIN_STRUCTURE_GAP_ATR,
            EPS,
        )

        if (
            h2
            > h1 + min_gap
            and
            l2
            > l1 + min_gap
        ):

            return "bullish"

        if (
            h2
            < h1 - min_gap
            and
            l2
            < l1 - min_gap
        ):

            return "bearish"

    ind = add_indicators(
        work
    )

    if ind.empty:
        return "range"

    last = ind.iloc[-1]

    e9 = _safe_float(
        last.get("ema9")
    )

    e21 = _safe_float(
        last.get("ema21")
    )

    e50 = _safe_float(
        last.get("ema50")
    )

    if (
        e9 > e21 > e50
    ):
        return "bullish"

    if (
        e9 < e21 < e50
    ):
        return "bearish"

    return "range"


def structure_score(
    df: pd.DataFrame,
) -> int:

    work = _normalize(df)

    if len(work) < 12:
        return 0

    swings = (
        _last_swing_levels(
            work
        )
    )

    highs = swings["highs"]
    lows = swings["lows"]

    score = 0

    if len(highs) >= 2:

        if (
            highs[-1][1]
            != highs[-2][1]
        ):
            score += 1

    if len(lows) >= 2:

        if (
            lows[-1][1]
            != lows[-2][1]
        ):
            score += 1

    structure = (
        detect_structure(
            work
        )
    )

    if structure in (
        "bullish",
        "bearish",
    ):
        score += 2

    ind = add_indicators(
        work
    )

    if len(ind) >= 3:

        if (
            structure
            == "bullish"
            and
            ind[
                "ema9"
            ].iloc[-1]
            >
            ind[
                "ema21"
            ].iloc[-1]
        ):
            score += 1

        elif (
            structure
            == "bearish"
            and
            ind[
                "ema9"
            ].iloc[-1]
            <
            ind[
                "ema21"
            ].iloc[-1]
        ):
            score += 1

    return min(
        score,
        5,
    )


# ============================================================
# CONSOLIDACIÓN PREVIA
# ============================================================

def detect_consolidation(
    history: pd.DataFrame,
    direction: str,
    atr: float,
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "score": 0,
        "range_atr": 0.0,
        "drift_atr": 0.0,
        "candles": 0,
        "reason": "sin consolidación clara",
    }

    if (
        history is None
        or len(history)
        < MIN_CONSOLIDATION_CANDLES
        or atr <= 0
    ):
        return result

    look = history.tail(
        CONSOLIDATION_LOOKBACK
    ).copy()

    if len(look) < 4:
        return result

    high = float(
        look["high"].max()
    )

    low = float(
        look["low"].min()
    )

    rng = high - low

    range_atr = (
        rng / atr
    )

    first_close = float(
        look["close"].iloc[0]
    )

    last_close = float(
        look["close"].iloc[-1]
    )

    drift_atr = (
        abs(
            last_close
            - first_close
        )
        / atr
    )

    result[
        "range_atr"
    ] = range_atr

    result[
        "drift_atr"
    ] = drift_atr

    result[
        "candles"
    ] = len(look)

    compressed = (
        range_atr
        <= MAX_CONSOLIDATION_RANGE_ATR
    )

    low_drift = (
        drift_atr
        <= MAX_CONSOLIDATION_DRIFT_ATR
    )

    if compressed and low_drift:

        result["valid"] = True

        result["score"] = 10

        result[
            "reason"
        ] = (
            "consolidación previa "
            "detectada"
        )

        if (
            range_atr
            <= 1.50
        ):
            result["score"] += 5

    return result


# ============================================================
# IMPULSO
# ============================================================

def _direction_of_row(
    row: pd.Series,
) -> int:

    o = _safe_float(
        row.get("open")
    )

    c = _safe_float(
        row.get("close")
    )

    if c > o:
        return 1

    if c < o:
        return -1

    return 0


def _consecutive_direction(
    history: pd.DataFrame,
    direction: str,
) -> int:

    if (
        history is None
        or history.empty
    ):
        return 0

    target = (
        1
        if direction
        == "bullish"
        else -1
    )

    count = 0

    for i in range(
        len(history) - 1,
        -1,
        -1,
    ):

        value = (
            _direction_of_row(
                history.iloc[i]
            )
        )

        if value != target:
            break

        count += 1

    return count


def analyze_impulse_phase(
    history: pd.DataFrame,
    current: pd.Series,
    direction: str,
    atr: float,
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "phase": "unknown",
        "score": 0,
        "age": 999,
        "extension_atr": 999.0,
        "start_index": None,
        "start_price": None,
        "impulse_high": None,
        "impulse_low": None,
        "consecutive": 0,
        "consolidation": False,
        "reason": "sin impulso válido",
    }

    if (
        history is None
        or history.empty
        or atr <= 0
    ):
        return result

    data = history.copy()

    if len(data) < 8:
        return result

    target = (
        1
        if direction
        == "bullish"
        else -1
    )

    start = max(
        1,
        len(data)
        - IMPULSE_LOOKBACK,
    )

    candidates: list[
        Tuple[int, int, float]
    ] = []

    for i in range(
        start,
        len(data),
    ):

        row = data.iloc[i]

        metrics = candle_metrics(
            row
        )

        direction_value = (
            _direction_of_row(
                row
            )
        )

        if (
            direction_value
            != target
        ):
            continue

        body_atr = (
            metrics["body"]
            / atr
        )

        left_start = max(
            0,
            i - BREAKOUT_LOOKBACK,
        )

        previous_window = (
            data.iloc[
                left_start:i
            ]
        )

        if previous_window.empty:
            continue

        previous_high = float(
            previous_window[
                "high"
            ].max()
        )

        previous_low = float(
            previous_window[
                "low"
            ].min()
        )

        breakout = False

        if direction == "bullish":

            breakout = (
                float(
                    row["close"]
                )
                > previous_high
            )

        else:

            breakout = (
                float(
                    row["close"]
                )
                < previous_low
            )

        strong_body = (
            metrics[
                "body_ratio"
            ]
            >= MIN_IMPULSE_BODY_RATIO
            and
            body_atr
            >= MIN_IMPULSE_BODY_ATR
        )

        if breakout and strong_body:

            candidates.append(
                (
                    i,
                    i,
                    float(
                        row["close"]
                    ),
                )
            )

    if not candidates:

        for i in range(
            max(
                1,
                len(data)
                - IMPULSE_LOOKBACK,
            ),
            len(data),
        ):

            if i < 2:
                continue

            row = data.iloc[i]

            direction_value = (
                _direction_of_row(
                    row
                )
            )

            if (
                direction_value
                != target
            ):
                continue

            previous_rows = (
                data.iloc[
                    max(0, i - 3):i
                ]
            )

            previous_directional = sum(
                1
                for j in range(
                    len(
                        previous_rows
                    )
                )
                if (
                    _direction_of_row(
                        previous_rows.iloc[j]
                    )
                    == target
                )
            )

            if previous_directional <= 1:

                candidates.append(
                    (
                        i,
                        i,
                        float(
                            row["close"]
                        ),
                    )
                )

    if not candidates:

        result[
            "reason"
        ] = (
            "no se encontró inicio "
            "claro del impulso"
        )

        return result

    start_index = candidates[-1][0]

    age = (
        len(data)
        - 1
        - start_index
    )

    segment = data.iloc[
        start_index:
    ]

    impulse_high = float(
        segment["high"].max()
    )

    impulse_low = float(
        segment["low"].min()
    )

    impulse_range = (
        impulse_high
        - impulse_low
    )

    extension_atr = (
        impulse_range
        / atr
    )

    consecutive = (
        _consecutive_direction(
            data,
            direction,
        )
    )

    if age <= 1:

        phase = "inicio"

    elif age <= 3:

        phase = "temprano"

    elif age <= MAX_IMPULSE_AGE:

        phase = "avanzado"

    else:

        phase = "tardío"

    before_start = data.iloc[
        max(
            0,
            start_index
            - CONSOLIDATION_LOOKBACK,
        ):start_index
    ]

    consolidation = detect_consolidation(
        before_start,
        direction,
        atr,
    )

    score = 0

    if phase == "inicio":
        score += 25

    elif phase == "temprano":
        score += 20

    elif phase == "avanzado":
        score += 8

    else:
        score -= 15

    if (
        extension_atr
        <= 1.50
    ):
        score += 20

    elif (
        extension_atr
        <= 2.20
    ):
        score += 12

    elif (
        extension_atr
        <= MAX_IMPULSE_TOTAL_ATR
    ):
        score += 4

    else:
        score -= 20

    if consecutive <= 2:
        score += 15

    elif consecutive <= 3:
        score += 8

    elif consecutive <= MAX_CONSECUTIVE_DIRECTION_CANDLES:
        score += 0

    else:
        score -= 15

    if consolidation["valid"]:
        score += consolidation[
            "score"
        ]

    else:
        score -= 3

    valid = True

    if age > MAX_IMPULSE_AGE:
        valid = False

    if (
        extension_atr
        > MAX_IMPULSE_TOTAL_ATR
    ):
        valid = False

    if (
        consecutive
        > MAX_CONSECUTIVE_DIRECTION_CANDLES
    ):
        valid = False

    result.update(
        {
            "valid": valid,
            "phase": phase,
            "score": max(
                -50,
                min(
                    100,
                    int(score),
                ),
            ),
            "age": age,
            "extension_atr": extension_atr,
            "start_index": start_index,
            "start_price": float(
                data[
                    "close"
                ].iloc[
                    start_index
                ]
            ),
            "impulse_high": impulse_high,
            "impulse_low": impulse_low,
            "consecutive": consecutive,
            "consolidation": bool(
                consolidation["valid"]
            ),
            "consolidation_score": consolidation[
                "score"
            ],
            "reason": (
                f"impulso {phase} | "
                f"edad={age} | "
                f"extensión={extension_atr:.2f} ATR | "
                f"consecutivas={consecutive}"
            ),
        }
    )

    return result


# ============================================================
# ZONAS
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = SWING_LOOKBACK,
) -> Tuple[
    float,
    float,
]:

    work = _normalize(df)

    if work.empty:
        return 0.0, 0.0

    x = work.tail(
        lookback
    )

    return (
        float(x["low"].min()),
        float(x["high"].max()),
    )


def _zone_test(
    candle: Dict[str, float],
    level: float,
    atr: float,
    side: str,
) -> Tuple[
    bool,
    float,
    str,
]:

    zone = max(
        atr * ZONE_ATR,
        EPS,
    )

    if side == "support":

        touched = (
            candle["low"]
            <= level + zone
        )

        closed_above = (
            candle["close"]
            > level
        )

        wick_ok = (
            candle["lower"]
            / candle["range"]
            >= MIN_REJECTION_WICK_RATIO
            or
            candle["lower"]
            >= candle["body"]
            * MIN_WICK_BODY_RATIO
        )

        close_ok = (
            candle[
                "close_position"
            ]
            >= MIN_CLOSE_POSITION_CALL
        )

        valid = (
            touched
            and closed_above
            and wick_ok
            and close_ok
        )

        distance = (
            abs(
                candle["close"]
                - level
            )
            / atr
        )

        return (
            valid,
            distance,
            "rechazo de soporte",
        )

    touched = (
        candle["high"]
        >= level - zone
    )

    closed_below = (
        candle["close"]
        < level
    )

    wick_ok = (
        candle["upper"]
        / candle["range"]
        >= MIN_REJECTION_WICK_RATIO
        or
        candle["upper"]
        >= candle["body"]
        * MIN_WICK_BODY_RATIO
    )

    close_ok = (
        candle[
            "close_position"
        ]
        <= MAX_CLOSE_POSITION_PUT
    )

    valid = (
        touched
        and closed_below
        and wick_ok
        and close_ok
    )

    distance = (
        abs(
            candle["close"]
            - level
        )
        / atr
    )

    return (
        valid,
        distance,
        "rechazo de resistencia",
    )


# ============================================================
# EXTREMO OPUESTO
# ============================================================

def _room_to_opposite(
    price: float,
    opposite_level: float,
    atr: float,
    direction: str,
) -> Tuple[
    bool,
    float,
]:

    if atr <= 0:
        return False, 0.0

    if direction == "bullish":

        room = (
            opposite_level
            - price
        )

    else:

        room = (
            price
            - opposite_level
        )

    room_atr = (
        room / atr
    )

    return (
        room_atr
        >= MIN_ROOM_TO_OPPOSITE_ATR,
        room_atr,
    )


# ============================================================
# ALINEACIÓN EMA
# ============================================================

def _ema_alignment(
    last: pd.Series,
    direction: str,
) -> bool:

    e9 = _safe_float(
        last.get("ema9")
    )

    e21 = _safe_float(
        last.get("ema21")
    )

    e50 = _safe_float(
        last.get("ema50")
    )

    close = _safe_float(
        last.get("close")
    )

    if direction == "bullish":

        return (
            e9 >= e21
            and
            e21 >= e50
            and
            close >= e21
        )

    return (
        e9 <= e21
        and
        e21 <= e50
        and
        close <= e21
    )


# ============================================================
# CUERPO
# ============================================================

def _body_is_valid(
    candle: Dict[str, float],
    atr: float,
) -> bool:

    if atr <= 0:
        return False

    body_atr = (
        candle["body"]
        / atr
    )

    return (
        candle["body_ratio"]
        >= MIN_BODY_RATIO
        and
        MIN_BODY_ATR
        <= body_atr
        <= MAX_BODY_ATR
    )


# ============================================================
# NUEVO:
# DISTANCIA A TENDENCIA DINÁMICA
# ============================================================

def _dynamic_trendline_distance(
    history: pd.DataFrame,
    current_price: float,
    direction: str,
    atr: float,
) -> Dict[str, Any]:

    result = {
        "distance_atr": 999.0,
        "line_price": None,
        "valid": True,
        "points": 0,
        "reason": "sin tendencia dinámica suficiente",
    }

    if (
        history is None
        or len(history) < 10
        or atr <= 0
    ):
        return result

    swings = _last_swing_levels(
        history
    )

    if direction == "bullish":

        points = swings["lows"]

    else:

        points = swings["highs"]

    if len(points) < 2:
        return result

    p1 = points[-2]
    p2 = points[-1]

    i1, v1 = p1
    i2, v2 = p2

    if i2 == i1:
        return result

    current_index = len(history)

    slope = (
        (v2 - v1)
        / float(i2 - i1)
    )

    projected = (
        v2
        + slope
        * (
            current_index
            - i2
        )
    )

    distance = abs(
        current_price
        - projected
    ) / atr

    result.update(
        {
            "distance_atr": float(
                distance
            ),
            "line_price": float(
                projected
            ),
            "points": 2,
            "valid": (
                distance
                >= MIN_DISTANCE_FROM_TRENDLINE_ATR
            ),
            "reason": (
                "distancia a tendencia "
                f"{distance:.2f} ATR"
            ),
        }
    )

    return result


# ============================================================
# NUEVO:
# DISTANCIAS A SOPORTE / RESISTENCIA
# ============================================================

def _level_distances(
    price: float,
    support: Optional[float],
    resistance: Optional[float],
    atr: float,
) -> Dict[str, float]:

    if atr <= 0:
        return {
            "support_atr": 999.0,
            "resistance_atr": 999.0,
        }

    support_distance = 999.0

    resistance_distance = 999.0

    if support is not None:

        support_distance = (
            abs(
                price
                - support
            )
            / atr
        )

    if resistance is not None:

        resistance_distance = (
            abs(
                resistance
                - price
            )
            / atr
        )

    return {
        "support_atr": support_distance,
        "resistance_atr": resistance_distance,
    }


# ============================================================
# NUEVO:
# LEJOS DE ZONA DE REVERSIÓN
# ============================================================

def _away_from_reversal_zone(
    price: float,
    support: Optional[float],
    resistance: Optional[float],
    atr: float,
    direction: str,
) -> Tuple[
    bool,
    float,
]:

    if atr <= 0:
        return False, 0.0

    distances = _level_distances(
        price,
        support,
        resistance,
        atr,
    )

    if direction == "bullish":

        distance = distances[
            "resistance_atr"
        ]

    else:

        distance = distances[
            "support_atr"
        ]

    return (
        distance
        >= MIN_DISTANCE_FROM_ZONE_ATR,
        distance,
    )


# ============================================================
# NUEVO:
# VELA DE CONTINUIDAD
# ============================================================

def _is_continuity_candle(
    c: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:

    if atr <= 0:
        return False

    body_atr = (
        c["body"]
        / atr
    )

    if (
        c["body_ratio"]
        < CONTINUITY_MIN_BODY_RATIO
    ):
        return False

    if (
        body_atr
        < CONTINUITY_MIN_BODY_ATR
    ):
        return False

    if direction == "bullish":

        return (
            c["close"]
            > c["open"]
            and
            c["close_position"]
            >= 0.60
        )

    return (
        c["close"]
        < c["open"]
        and
        c["close_position"]
        <= 0.40
    )


# ============================================================
# NUEVO:
# VELA DE DESCANSO
# ============================================================

def _is_rest_candle(
    c: Dict[str, float],
    previous: Dict[str, float],
    direction: str,
) -> bool:

    if (
        c["body_ratio"]
        > REST_MAX_BODY_RATIO
    ):
        return False

    if (
        previous["body_ratio"]
        < REST_MIN_PREVIOUS_BODY_RATIO
    ):
        return False

    if direction == "bullish":

        return (
            previous["close"]
            > previous["open"]
        )

    return (
        previous["close"]
        < previous["open"]
    )


# ============================================================
# NUEVO:
# VELA DE INDECISIÓN
# ============================================================

def _is_indecision_candle(
    c: Dict[str, float],
) -> bool:

    if (
        c["body_ratio"]
        > INDECISION_MAX_BODY_RATIO
    ):
        return False

    return (
        c["upper_ratio"]
        >= INDECISION_MIN_WICK_RATIO
        or
        c["lower_ratio"]
        >= INDECISION_MIN_WICK_RATIO
    )


# ============================================================
# NUEVO:
# VELA DE FUERZA
# ============================================================

def _is_force_candle(
    c: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:

    if atr <= 0:
        return False

    body_atr = (
        c["body"]
        / atr
    )

    if (
        c["body_ratio"]
        < FORCE_MIN_BODY_RATIO
    ):
        return False

    if (
        body_atr
        < FORCE_MIN_BODY_ATR
    ):
        return False

    if direction == "bullish":

        return (
            c["close"]
            > c["open"]
            and
            c["close_position"]
            >= 0.65
        )

    return (
        c["close"]
        < c["open"]
        and
        c["close_position"]
        <= 0.35
    )


# ============================================================
# NUEVO:
# DIVERGENCIA RSI
# ============================================================

def _detect_rsi_divergence(
    history: pd.DataFrame,
    current: pd.Series,
    direction: str,
    atr: float,
) -> Dict[str, Any]:

    result = {
        "valid": False,
        "type": None,
        "price_previous": None,
        "price_current": None,
        "rsi_previous": None,
        "rsi_current": None,
        "price_change_atr": 0.0,
        "rsi_change": 0.0,
        "reason": "sin divergencia",
    }

    if (
        history is None
        or len(history) < 8
        or atr <= 0
    ):
        return result

    data = history.tail(
        DIVERGENCE_LOOKBACK
    ).copy()

    if "rsi" not in data.columns:
        return result

    rsi_current = _safe_float(
        current.get("rsi"),
        50.0,
    )

    price_current = _safe_float(
        current.get("close")
    )

    if direction == "bullish":

        # Buscar un mínimo anterior relevante
        # inferior al mínimo actual.

        previous_low_index = None
        previous_low_price = None
        previous_low_rsi = None

        for i in range(
            len(data) - 1,
            -1,
            -1,
        ):

            row = data.iloc[i]

            low = _safe_float(
                row.get("low")
            )

            if (
                low
                < price_current
            ):

                if (
                    previous_low_price
                    is None
                    or low
                    < previous_low_price
                ):

                    previous_low_index = i
                    previous_low_price = low
                    previous_low_rsi = _safe_float(
                        row.get("rsi"),
                        50.0,
                    )

        if (
            previous_low_price
            is None
            or previous_low_rsi
            is None
        ):
            return result

        price_change = (
            previous_low_price
            - price_current
        )

        price_change_atr = (
            abs(price_change)
            / atr
        )

        rsi_change = (
            rsi_current
            - previous_low_rsi
        )

        # Precio hace LL y RSI hace HL.
        valid = (
            price_current
            < previous_low_price
            and
            rsi_current
            > previous_low_rsi
            and
            rsi_change
            >= DIVERGENCE_MIN_RSI_CHANGE
            and
            price_change_atr
            >= DIVERGENCE_MIN_PRICE_CHANGE_ATR
        )

        if valid:

            result.update(
                {
                    "valid": True,
                    "type": "bullish",
                    "price_previous": previous_low_price,
                    "price_current": price_current,
                    "rsi_previous": previous_low_rsi,
                    "rsi_current": rsi_current,
                    "price_change_atr": price_change_atr,
                    "rsi_change": rsi_change,
                    "reason": (
                        "divergencia alcista "
                        "precio LL + RSI HL"
                    ),
                }
            )

        return result

    # --------------------------------------------------------
    # DIVERGENCIA BAJISTA
    # --------------------------------------------------------

    previous_high_price = None
    previous_high_rsi = None

    for i in range(
        len(data) - 1,
        -1,
        -1,
    ):

        row = data.iloc[i]

        high = _safe_float(
            row.get("high")
        )

        if (
            high
            > price_current
        ):

            if (
                previous_high_price
                is None
                or high
                > previous_high_price
            ):

                previous_high_price = high

                previous_high_rsi = _safe_float(
                    row.get("rsi"),
                    50.0,
                )

    if (
        previous_high_price
        is None
        or previous_high_rsi
        is None
    ):
        return result

    price_change = (
        price_current
        - previous_high_price
    )

    price_change_atr = (
        abs(price_change)
        / atr
    )

    rsi_change = (
        previous_high_rsi
        - rsi_current
    )

    # Precio hace HH y RSI hace LH.
    valid = (
        price_current
        > previous_high_price
        and
        rsi_current
        < previous_high_rsi
        and
        rsi_change
        >= DIVERGENCE_MIN_RSI_CHANGE
        and
        price_change_atr
        >= DIVERGENCE_MIN_PRICE_CHANGE_ATR
    )

    if valid:

        result.update(
            {
                "valid": True,
                "type": "bearish",
                "price_previous": previous_high_price,
                "price_current": price_current,
                "rsi_previous": previous_high_rsi,
                "rsi_current": rsi_current,
                "price_change_atr": price_change_atr,
                "rsi_change": rsi_change,
                "reason": (
                    "divergencia bajista "
                    "precio HH + RSI LH"
                ),
            }
        )

    return result


# ============================================================
# NUEVO:
# FASE SALUDABLE PARA CONTINUIDAD / FUERZA
# ============================================================

def _healthy_continuation_phase(
    impulse: Dict[str, Any],
) -> bool:

    phase = impulse.get(
        "phase",
        "unknown",
    )

    extension = _safe_float(
        impulse.get(
            "extension_atr",
            999.0,
        ),
        999.0,
    )

    consecutive = int(
        impulse.get(
            "consecutive",
            999,
        )
    )

    if phase in (
        "inicio",
        "temprano",
    ):
        return True

    if phase == "avanzado":

        return (
            extension
            <= 2.20
            and
            consecutive
            <= 3
        )

    return False


# ============================================================
# NUEVO:
# CALIDAD BASE DE SEÑALES ALTERNATIVAS
# ============================================================

def _alternative_quality(
    structure_score_value: int,
    impulse: Dict[str, Any],
    candle: Dict[str, float],
    distance_zone: float,
    trendline_distance: float,
    room_atr: float,
) -> int:

    score = 60.0

    score += min(
        10.0,
        structure_score_value
        * 2.0,
    )

    if impulse.get(
        "phase"
    ) == "inicio":

        score += 10.0

    elif impulse.get(
        "phase"
    ) == "temprano":

        score += 8.0

    elif impulse.get(
        "phase"
    ) == "avanzado":

        score += 2.0

    if (
        impulse.get(
            "extension_atr",
            999.0,
        )
        <= 1.50
    ):

        score += 8.0

    elif (
        impulse.get(
            "extension_atr",
            999.0,
        )
        <= 2.20
    ):

        score += 4.0

    if distance_zone >= 0.70:
        score += 5.0

    elif distance_zone >= 0.35:
        score += 2.0

    if trendline_distance >= 0.60:
        score += 5.0

    elif trendline_distance >= 0.30:
        score += 2.0

    if room_atr >= 1.20:
        score += 5.0

    elif room_atr >= 0.70:
        score += 2.0

    if candle[
        "body_ratio"
    ] >= 0.60:

        score += 3.0

    return int(
        max(
            0,
            min(
                100,
                round(
                    score
                ),
            ),
        )
    )


# ============================================================
# API PRINCIPAL
# ============================================================

def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:

    result = _empty_result()

    # ========================================================
    # CONSTRUIR DATAFRAME
    # ========================================================

    clean = _build_analysis_dataframe(
        candle_1m=candle_1m,
        previous_m1=previous_m1,
        df=df,
    )

    if len(clean) < MIN_BARS:

        result["reason"] = (
            "Historial insuficiente "
            f"{len(clean)}/{MIN_BARS}"
        )

        return result

    data = add_indicators(
        clean
    )

    if (
        data.empty
        or len(data) < MIN_BARS
    ):

        result[
            "reason"
        ] = "Indicadores insuficientes"

        return result

    # ========================================================
    # N ES LA ÚLTIMA VELA CERRADA
    # ========================================================

    current = data.iloc[-1]

    history = data.iloc[:-1].copy()

    if len(history) < MIN_BARS - 1:

        result[
            "reason"
        ] = "Historial cerrado insuficiente"

        return result

    # ========================================================
    # ATR / RSI
    # ========================================================

    atr = _atr(
        history
    )

    rsi = _safe_float(
        current.get("rsi"),
        50.0,
    )

    if (
        atr <= 0
        or not math.isfinite(atr)
    ):

        result[
            "reason"
        ] = "ATR inválido"

        return result

    # ========================================================
    # ESTRUCTURA
    # ========================================================

    structure = detect_structure(
        history
    )

    s_score = structure_score(
        history
    )

    swings = _last_swing_levels(
        history
    )

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

    # ========================================================
    # MÉTRICAS
    # ========================================================

    c = candle_metrics(
        current
    )

    previous = history.iloc[-1]

    p = candle_metrics(
        previous
    )

    price = c["close"]

    candle_ts = None

    if (
        "from" in data.columns
        and not pd.isna(
            current["from"]
        )
    ):

        candle_ts = int(
            current["from"]
        )

    # ========================================================
    # IMPULSO
    # ========================================================

    impulse = analyze_impulse_phase(
        history,
        current,
        structure,
        atr,
    )

    # ========================================================
    # TENDENCIA DINÁMICA
    # ========================================================

    trendline = (
        _dynamic_trendline_distance(
            history,
            price,
            structure,
            atr,
        )
    )

    # ========================================================
    # DISTANCIAS S/R
    # ========================================================

    distances = _level_distances(
        price,
        last_low,
        last_high,
        atr,
    )

    # ========================================================
    # DIVERGENCIA
    # ========================================================

    divergence = (
        _detect_rsi_divergence(
            history,
            current,
            structure,
            atr,
        )
    )

    # ========================================================
    # RESULTADO BASE
    # ========================================================

    result.update(
        {
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
            "candle": candle_direction(
                current
            ),
            "candle_timestamp": candle_ts,
            "analysis": {
                "structure": structure,
                "structure_score": s_score,
                "rsi": rsi,
                "atr": atr,
                "last_swing_high": last_high,
                "last_swing_low": last_low,
                "support": last_low,
                "resistance": last_high,
                "impulse_phase": impulse[
                    "phase"
                ],
                "impulse_age": impulse[
                    "age"
                ],
                "impulse_extension_atr": impulse[
                    "extension_atr"
                ],
                "impulse_start_index": impulse[
                    "start_index"
                ],
                "impulse_start_price": impulse[
                    "start_price"
                ],
                "impulse_high": impulse[
                    "impulse_high"
                ],
                "impulse_low": impulse[
                    "impulse_low"
                ],
                "consecutive_direction_candles": impulse[
                    "consecutive"
                ],
                "pre_impulse_consolidation": impulse[
                    "consolidation"
                ],
                "candle": c,
                "trendline": trendline,
                "distance_support_atr": distances[
                    "support_atr"
                ],
                "distance_resistance_atr": distances[
                    "resistance_atr"
                ],
                "divergence": divergence,
            },
        }
    )

    # ========================================================
    # ESTRUCTURA NO VÁLIDA
    # ========================================================

    if structure not in (
        "bullish",
        "bearish",
    ):

        result[
            "reason"
        ] = (
            "Estructura lateral/ambigua"
        )

        result[
            "analysis"
        ][
            "blocked_reason"
        ] = (
            "sin HH/HL o LH/LL "
            "suficientemente claro"
        )

        return result

    if s_score < MIN_STRUCTURE_SCORE:

        result[
            "reason"
        ] = (
            "Estructura insuficiente"
        )

        return result

    if (
        last_high is None
        or last_low is None
    ):

        result[
            "reason"
        ] = (
            "No hay niveles estructurales"
        )

        return result

    # ========================================================
    # ROOM AL EXTREMO CONTRARIO
    # ========================================================

    if structure == "bullish":

        room_ok, room_atr = (
            _room_to_opposite(
                price,
                last_high,
                atr,
                "bullish",
            )
        )

    else:

        room_ok, room_atr = (
            _room_to_opposite(
                price,
                last_low,
                atr,
                "bearish",
            )
        )

    result[
        "analysis"
    ][
        "room_to_opposite_atr"
    ] = room_atr

    # ========================================================
    # ========================================================
    # PRIMERA PRIORIDAD:
    # RECHAZO ESTRUCTURAL EXISTENTE
    # ========================================================
    # ========================================================

    # ========================================================
    # CALL — RECHAZO
    # ========================================================

    if structure == "bullish":

        if _ema_alignment(
            current,
            "bullish",
        ):

            if (
                CALL_RSI_MIN
                <= rsi
                <= CALL_RSI_MAX
            ):

                support_ok, distance, _ = (
                    _zone_test(
                        c,
                        last_low,
                        atr,
                        "support",
                    )
                )

                if support_ok:

                    if (
                        distance
                        <= MAX_ENTRY_DISTANCE_ATR
                    ):

                        if (
                            price
                            > p["close"]
                        ):

                            phase = impulse[
                                "phase"
                            ]

                            impulse_extension = (
                                impulse[
                                    "extension_atr"
                                ]
                            )

                            consecutive = (
                                impulse[
                                    "consecutive"
                                ]
                            )

                            if (
                                impulse_extension
                                <= MAX_IMPULSE_TOTAL_ATR
                                and
                                consecutive
                                <= MAX_CONSECUTIVE_DIRECTION_CANDLES
                                and
                                phase
                                != "tardío"
                            ):

                                wick_strength = (
                                    c["lower"]
                                    / max(
                                        c["range"],
                                        EPS,
                                    )
                                )

                                recovery_strength = max(
                                    0.0,
                                    min(
                                        1.0,
                                        (
                                            price
                                            - p["close"]
                                        )
                                        / max(
                                            atr,
                                            EPS,
                                        ),
                                    ),
                                )

                                quality = 45.0

                                quality += min(
                                    15.0,
                                    wick_strength
                                    * 30.0,
                                )

                                quality += min(
                                    10.0,
                                    s_score
                                    * 2.0,
                                )

                                quality += min(
                                    10.0,
                                    recovery_strength
                                    * 10.0,
                                )

                                if phase == "inicio":
                                    quality += 10.0

                                elif phase == "temprano":
                                    quality += 7.0

                                elif phase == "avanzado":
                                    quality -= 5.0

                                if impulse[
                                    "consolidation"
                                ]:
                                    quality += 5.0

                                if (
                                    impulse[
                                        "extension_atr"
                                    ]
                                    <= 1.50
                                ):
                                    quality += 5.0

                                if room_atr < 1.0:
                                    quality -= 10.0

                                quality = int(
                                    max(
                                        0,
                                        min(
                                            100,
                                            round(
                                                quality
                                            ),
                                        ),
                                    )
                                )

                                if (
                                    quality
                                    >= MIN_ENTRY_SCORE
                                ):

                                    result.update(
                                        {
                                            "signal": "call",
                                            "score": quality,
                                            "reason": (
                                                "CALL | "
                                                "rechazo estructural "
                                                "de soporte | "
                                                f"calidad={quality}/100 | "
                                                f"fase={phase} | "
                                                f"impulso="
                                                f"{impulse['age']} velas | "
                                                f"extensión="
                                                f"{impulse_extension:.2f} ATR"
                                            ),
                                            "continuity": True,
                                            "blocked": False,
                                            "zone": "soporte_rechazado",
                                            "entry_type": (
                                                "rejection_support"
                                            ),
                                            "entry_quality": quality,
                                            "signal_price": price,
                                            "candle_open": c["open"],
                                            "candle_close": c["close"],
                                            "distance_to_zone_atr": distance,
                                            "analysis": {
                                                **result[
                                                    "analysis"
                                                ],
                                                "zone": (
                                                    "soporte_rechazado"
                                                ),
                                                "entry_quality": quality,
                                                "room_to_opposite_atr": room_atr,
                                                "distance_to_zone_atr": distance,
                                                "impulse_reason": impulse[
                                                    "reason"
                                                ],
                                            },
                                        }
                                    )

                                    return result

    # ========================================================
    # PUT — RECHAZO
    # ========================================================

    if structure == "bearish":

        if _ema_alignment(
            current,
            "bearish",
        ):

            if (
                PUT_RSI_MIN
                <= rsi
                <= PUT_RSI_MAX
            ):

                resistance_ok, distance, _ = (
                    _zone_test(
                        c,
                        last_high,
                        atr,
                        "resistance",
                    )
                )

                if resistance_ok:

                    if (
                        distance
                        <= MAX_ENTRY_DISTANCE_ATR
                    ):

                        if (
                            price
                            < p["close"]
                        ):

                            phase = impulse[
                                "phase"
                            ]

                            impulse_extension = (
                                impulse[
                                    "extension_atr"
                                ]
                            )

                            consecutive = (
                                impulse[
                                    "consecutive"
                                ]
                            )

                            if (
                                impulse_extension
                                <= MAX_IMPULSE_TOTAL_ATR
                                and
                                consecutive
                                <= MAX_CONSECUTIVE_DIRECTION_CANDLES
                                and
                                phase
                                != "tardío"
                            ):

                                wick_strength = (
                                    c["upper"]
                                    / max(
                                        c["range"],
                                        EPS,
                                    )
                                )

                                recovery_strength = max(
                                    0.0,
                                    min(
                                        1.0,
                                        (
                                            p["close"]
                                            - price
                                        )
                                        / max(
                                            atr,
                                            EPS,
                                        ),
                                    ),
                                )

                                quality = 45.0

                                quality += min(
                                    15.0,
                                    wick_strength
                                    * 30.0,
                                )

                                quality += min(
                                    10.0,
                                    s_score
                                    * 2.0,
                                )

                                quality += min(
                                    10.0,
                                    recovery_strength
                                    * 10.0,
                                )

                                if phase == "inicio":
                                    quality += 10.0

                                elif phase == "temprano":
                                    quality += 7.0

                                elif phase == "avanzado":
                                    quality -= 5.0

                                if impulse[
                                    "consolidation"
                                ]:
                                    quality += 5.0

                                if (
                                    impulse[
                                        "extension_atr"
                                    ]
                                    <= 1.50
                                ):
                                    quality += 5.0

                                if room_atr < 1.0:
                                    quality -= 10.0

                                quality = int(
                                    max(
                                        0,
                                        min(
                                            100,
                                            round(
                                                quality
                                            ),
                                        ),
                                    )
                                )

                                if (
                                    quality
                                    >= MIN_ENTRY_SCORE
                                ):

                                    result.update(
                                        {
                                            "signal": "put",
                                            "score": quality,
                                            "reason": (
                                                "PUT | "
                                                "rechazo estructural "
                                                "de resistencia | "
                                                f"calidad={quality}/100 | "
                                                f"fase={phase} | "
                                                f"impulso="
                                                f"{impulse['age']} velas | "
                                                f"extensión="
                                                f"{impulse_extension:.2f} ATR"
                                            ),
                                            "continuity": True,
                                            "blocked": False,
                                            "zone": "resistencia_rechazada",
                                            "entry_type": (
                                                "rejection_resistance"
                                            ),
                                            "entry_quality": quality,
                                            "signal_price": price,
                                            "candle_open": c["open"],
                                            "candle_close": c["close"],
                                            "distance_to_zone_atr": distance,
                                            "analysis": {
                                                **result[
                                                    "analysis"
                                                ],
                                                "zone": (
                                                    "resistencia_rechazada"
                                                ),
                                                "entry_quality": quality,
                                                "room_to_opposite_atr": room_atr,
                                                "distance_to_zone_atr": distance,
                                                "impulse_reason": impulse[
                                                    "reason"
                                                ],
                                            },
                                        }
                                    )

                                    return result

    # ========================================================
    # DESDE AQUÍ:
    # SEÑALES ALTERNATIVAS
    #
    # Estas NO sustituyen el rechazo.
    #
    # Permiten:
    # CONTINUIDAD
    # DESCANSO
    # INDECISIÓN
    # FUERZA
    # DIVERGENCIA
    # ========================================================

    # --------------------------------------------------------
    # NO APLICAR EL FILTRO DE CUERPO ORIGINAL AQUÍ.
    #
    # DESCANSO E INDECISIÓN necesitan cuerpos pequeños.
    # --------------------------------------------------------

    healthy_phase = (
        _healthy_continuation_phase(
            impulse
        )
    )

    # --------------------------------------------------------
    # DISTANCIA A LA ZONA CONTRARIA
    # --------------------------------------------------------

    away_from_zone, zone_distance = (
        _away_from_reversal_zone(
            price,
            last_low,
            last_high,
            atr,
            structure,
        )
    )

    # --------------------------------------------------------
    # DISTANCIA A TRENDLINE
    # --------------------------------------------------------

    trendline_distance = _safe_float(
        trendline.get(
            "distance_atr",
            999.0,
        ),
        999.0,
    )

    away_from_trendline = (
        trendline_distance
        >= MIN_DISTANCE_FROM_TRENDLINE_ATR
    )

    # ========================================================
    # REGLA DE SEGURIDAD PARA CONTINUACIÓN
    # ========================================================

    if (
        not room_ok
        and
        structure in (
            "bullish",
            "bearish",
        )
    ):

        result[
            "analysis"
        ][
            "alternative_blocked"
        ] = (
            "poco espacio hasta "
            "extremo contrario"
        )

    # ========================================================
    # 1. DIVERGENCIA
    # ========================================================
    #
    # CALL:
    # precio LL + RSI HL + estructura alcista
    #
    # PUT:
    # precio HH + RSI LH + estructura bajista
    #
    # La divergencia no se permite demasiado extendida.
    # ========================================================

    if divergence[
        "valid"
    ]:

        divergence_direction = (
            divergence[
                "type"
            ]
        )

        direction_matches = (
            (
                structure
                == "bullish"
                and
                divergence_direction
                == "bullish"
            )
            or
            (
                structure
                == "bearish"
                and
                divergence_direction
                == "bearish"
            )
        )

        if direction_matches:

            divergence_phase_ok = (
                impulse[
                    "extension_atr"
                ]
                <= MAX_IMPULSE_TOTAL_ATR
                and
                impulse[
                    "phase"
                ]
                != "tardío"
            )

            if (
                divergence_phase_ok
                and
                room_ok
                and
                away_from_zone
                and
                away_from_trendline
            ):

                quality = (
                    72
                    + min(
                        10,
                        s_score * 2,
                    )
                )

                if (
                    divergence[
                        "rsi_change"
                    ]
                    >= 4.0
                ):
                    quality += 5

                if (
                    divergence[
                        "price_change_atr"
                    ]
                    >= 0.15
                ):
                    quality += 5

                quality = min(
                    100,
                    quality,
                )

                if (
                    quality
                    >= 70
                ):

                    if (
                        divergence_direction
                        == "bullish"
                    ):

                        signal = "call"
                        zone = "divergencia_alcista"

                    else:

                        signal = "put"
                        zone = "divergencia_bajista"

                    result.update(
                        {
                            "signal": signal,
                            "score": quality,
                            "reason": (
                                f"{signal.upper()} | "
                                f"{divergence['reason']} | "
                                "estructura confirmada | "
                                f"calidad={quality}/100"
                            ),
                            "continuity": True,
                            "blocked": False,
                            "zone": zone,
                            "entry_type": (
                                "rsi_divergence"
                            ),
                            "entry_quality": quality,
                            "signal_price": price,
                            "candle_open": c["open"],
                            "candle_close": c["close"],
                            "analysis": {
                                **result[
                                    "analysis"
                                ],
                                "divergence": divergence,
                                "entry_quality": quality,
                                "zone_distance_atr": zone_distance,
                                "trendline_distance_atr": trendline_distance,
                            },
                        }
                    )

                    return result

    # ========================================================
    # 2. FUERZA
    # ========================================================
    #
    # Solo al comienzo o fase temprana.
    # Nunca cerca de soporte/resistencia.
    # Nunca cerca de trendline.
    # ========================================================

    if (
        healthy_phase
        and
        room_ok
        and
        away_from_zone
        and
        away_from_trendline
        and
        _is_force_candle(
            c,
            structure,
            atr,
        )
    ):

        quality = _alternative_quality(
            s_score,
            impulse,
            c,
            zone_distance,
            trendline_distance,
            room_atr,
        )

        quality += 8

        quality = min(
            100,
            quality,
        )

        if quality >= 70:

            if structure == "bullish":

                signal = "call"

                zone = (
                    "fuerza_alcista"
                )

            else:

                signal = "put"

                zone = (
                    "fuerza_bajista"
                )

            result.update(
                {
                    "signal": signal,
                    "score": quality,
                    "reason": (
                        f"{signal.upper()} | "
                        "vela de FUERZA | "
                        "inicio/continuación "
                        "temprana del impulso | "
                        f"calidad={quality}/100"
                    ),
                    "continuity": True,
                    "blocked": False,
                    "zone": zone,
                    "entry_type": "force",
                    "entry_quality": quality,
                    "signal_price": price,
                    "candle_open": c["open"],
                    "candle_close": c["close"],
                    "analysis": {
                        **result[
                            "analysis"
                        ],
                        "entry_quality": quality,
                        "zone_distance_atr": zone_distance,
                        "trendline_distance_atr": trendline_distance,
                    },
                }
            )

            return result

    # ========================================================
    # 3. CONTINUIDAD
    # ========================================================
    #
    # Debe existir tendencia.
    #
    # N es vela de continuidad.
    #
    # CALL:
    # no cerca de resistencia.
    #
    # PUT:
    # no cerca de soporte.
    #
    # Se prepara la entrada para N+1.
    # ========================================================

    if (
        healthy_phase
        and
        room_ok
        and
        away_from_zone
        and
        away_from_trendline
        and
        _is_continuity_candle(
            c,
            structure,
            atr,
        )
    ):

        quality = _alternative_quality(
            s_score,
            impulse,
            c,
            zone_distance,
            trendline_distance,
            room_atr,
        )

        if structure == "bullish":

            # No comprar cerca de resistencia.

            if (
                distances[
                    "resistance_atr"
                ]
                < MIN_DISTANCE_FROM_ZONE_ATR
            ):

                quality = 0

        else:

            # No vender cerca de soporte.

            if (
                distances[
                    "support_atr"
                ]
                < MIN_DISTANCE_FROM_ZONE_ATR
            ):

                quality = 0

        if quality >= 70:

            if structure == "bullish":

                signal = "call"

                zone = (
                    "continuidad_alcista"
                )

            else:

                signal = "put"

                zone = (
                    "continuidad_bajista"
                )

            result.update(
                {
                    "signal": signal,
                    "score": quality,
                    "reason": (
                        f"{signal.upper()} | "
                        "CONTINUIDAD | "
                        "vela N confirma "
                        "dirección | "
                        "entrada preparada para N+1 | "
                        f"calidad={quality}/100"
                    ),
                    "continuity": True,
                    "blocked": False,
                    "zone": zone,
                    "entry_type": "continuity",
                    "entry_quality": quality,
                    "signal_price": price,
                    "candle_open": c["open"],
                    "candle_close": c["close"],
                    "analysis": {
                        **result[
                            "analysis"
                        ],
                        "entry_quality": quality,
                        "zone_distance_atr": zone_distance,
                        "trendline_distance_atr": trendline_distance,
                        "entry_for_next_candle": True,
                    },
                }
            )

            return result

    # ========================================================
    # 4. DESCANSO
    # ========================================================
    #
    # N es una vela pequeña de descanso.
    #
    # Solo se permite en dirección de tendencia.
    #
    # La vela anterior debe demostrar fuerza/dirección.
    #
    # Se prepara N+1.
    # ========================================================

    if (
        room_ok
        and
        away_from_zone
        and
        away_from_trendline
        and
        _is_rest_candle(
            c,
            p,
            structure,
        )
    ):

        # La vela anterior debe ir en dirección
        # de la tendencia.

        previous_direction = (
            candle_direction(
                previous
            )
        )

        correct_previous_direction = (
            (
                structure == "bullish"
                and
                previous_direction
                == "bull"
            )
            or
            (
                structure == "bearish"
                and
                previous_direction
                == "bear"
            )
        )

        if correct_previous_direction:

            quality = (
                _alternative_quality(
                    s_score,
                    impulse,
                    c,
                    zone_distance,
                    trendline_distance,
                    room_atr,
                )
            )

            quality += 3

            quality = min(
                100,
                quality,
            )

            if quality >= 70:

                if structure == "bullish":

                    signal = "call"

                    zone = (
                        "descanso_alcista"
                    )

                else:

                    signal = "put"

                    zone = (
                        "descanso_bajista"
                    )

                result.update(
                    {
                        "signal": signal,
                        "score": quality,
                        "reason": (
                            f"{signal.upper()} | "
                            "DESCANSO | "
                            "vela N descansa "
                            "dentro de tendencia | "
                            "entrada preparada para N+1 | "
                            f"calidad={quality}/100"
                        ),
                        "continuity": True,
                        "blocked": False,
                        "zone": zone,
                        "entry_type": "rest",
                        "entry_quality": quality,
                        "signal_price": price,
                        "candle_open": c["open"],
                        "candle_close": c["close"],
                        "analysis": {
                            **result[
                                "analysis"
                            ],
                            "entry_quality": quality,
                            "zone_distance_atr": zone_distance,
                            "trendline_distance_atr": trendline_distance,
                            "entry_for_next_candle": True,
                        },
                    }
                )

                return result

    # ========================================================
    # 5. INDECISIÓN
    # ========================================================
    #
    # La vela N NO es una entrada directa.
    #
    # Se utiliza para preparar N+1.
    #
    # N+1 debe quedar alejada de:
    #
    # - soporte
    # - resistencia
    # - punto de reversión
    # - trendline
    #
    # Como bot.py ejecuta la señal en N+1 después de analizar N,
    # devolvemos la preparación como señal.
    # ========================================================

    if (
        room_ok
        and
        away_from_zone
        and
        away_from_trendline
        and
        _is_indecision_candle(
            c
        )
    ):

        quality = (
            _alternative_quality(
                s_score,
                impulse,
                c,
                zone_distance,
                trendline_distance,
                room_atr,
            )
        )

        # Indecisión requiere mayor confirmación
        # estructural.

        if s_score >= 3:

            quality += 2

        quality = min(
            100,
            quality,
        )

        if quality >= 70:

            if structure == "bullish":

                signal = "call"

                zone = (
                    "indecision_alcista"
                )

            else:

                signal = "put"

                zone = (
                    "indecision_bajista"
                )

            result.update(
                {
                    "signal": signal,
                    "score": quality,
                    "reason": (
                        f"{signal.upper()} | "
                        "INDECISIÓN | "
                        "N no entra directamente | "
                        "preparada para N+1 | "
                        "lejos de S/R y tendencia | "
                        f"calidad={quality}/100"
                    ),
                    "continuity": True,
                    "blocked": False,
                    "zone": zone,
                    "entry_type": "indecision",
                    "entry_quality": quality,
                    "signal_price": price,
                    "candle_open": c["open"],
                    "candle_close": c["close"],
                    "analysis": {
                        **result[
                            "analysis"
                        ],
                        "entry_quality": quality,
                        "zone_distance_atr": zone_distance,
                        "trendline_distance_atr": trendline_distance,
                        "entry_for_next_candle": True,
                        "indecision_requires_n_plus_1": True,
                    },
                }
            )

            return result

    # ========================================================
    # NINGUNA CONFIGURACIÓN
    # ========================================================

    result[
        "reason"
    ] = (
        "Sin configuración válida: "
        "rechazo, continuidad, descanso, "
        "fuerza, indecisión o divergencia"
    )

    result[
        "analysis"
    ][
        "alternative_blocked"
    ] = True

    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(
    df: pd.DataFrame,
) -> Optional[str]:

    return analyze_market(
        df
    ).get(
        "signal"
    )


def signal(
    df: pd.DataFrame,
) -> Optional[str]:

    return get_signal(
        df
    )


# ============================================================
# PRUEBA DIRECTA
# ============================================================

if __name__ == "__main__":

    print(
        "strategy.py cargado correctamente."
    )

    print(
        "API compatible:"
    )

    print(
        "analyze_market(df)"
    )

    print(
        "analyze_market("
        "candle_1m=..., "
        "previous_m1=..., "
        "pair=..."
        ")"
    )
