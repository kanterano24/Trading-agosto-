"""
strategy.py

ESTRATEGIA ESTRUCTURAL DE RECHAZO + FASE DE IMPULSO
PARA BINARY OTC M1.

OBJETIVO PRINCIPAL
------------------

Evitar entradas tardías después de que el precio ya haya recorrido
gran parte del impulso.

La estrategia NO entra simplemente porque:

    "hay una vela verde"
    "hay una vela roja"
    "hay rechazo"
    "estamos cerca de un soporte"

Primero intenta determinar:

1. Estructura del mercado.
2. Últimos máximos y mínimos confirmados.
3. HH/HL o LH/LL.
4. Si existió consolidación antes del movimiento.
5. Dónde comenzó el impulso.
6. Cuántas velas lleva desarrollándose.
7. Cuánto ha recorrido el impulso en ATR.
8. Si el movimiento está extendido.
9. Si la vela N realmente rechaza una zona.
10. Si existe espacio suficiente hasta el extremo contrario.
11. Si la entrada representa continuación temprana/reacción
    estructural y no persecución del precio.

CALL:
    estructura alcista
    +
    retroceso/test de soporte o último mínimo
    +
    rechazo alcista
    +
    recuperación
    +
    impulso no agotado
    +
    espacio suficiente

PUT:
    estructura bajista
    +
    retroceso/test de resistencia o último máximo
    +
    rechazo bajista
    +
    recuperación
    +
    impulso no agotado
    +
    espacio suficiente

API compatible con bot.py:

    analyze_market(
        candle_1m=...,
        previous_m1=...,
        pair=...
    )

También acepta:

    analyze_market(df)

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

    # --------------------------------------------------------
    # API ANTIGUA:
    #
    # analyze_market(df)
    # --------------------------------------------------------

    if df is not None:

        clean = _normalize(df)

        return clean

    # --------------------------------------------------------
    # API NUEVA DEL BOT:
    #
    # candle_1m = N
    # previous_m1 = velas anteriores a N
    # --------------------------------------------------------

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

    combined = _normalize(
        combined
    )

    return combined


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

    previous_close = (
        close.shift(1)
    )

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

    # Consolidación:
    #
    # rango relativamente comprimido
    # y poco desplazamiento neto.

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

    # --------------------------------------------------------
    # Buscamos hacia atrás el punto donde comenzó el movimiento.
    #
    # No asumimos que la segunda vela sea automáticamente buena.
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # Ruptura local:
        #
        # Bullish:
        # cierre > máximo anterior.
        #
        # Bearish:
        # cierre < mínimo anterior.
        # ----------------------------------------------------

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

    # --------------------------------------------------------
    # Si no encontramos ruptura clara,
    # buscamos el inicio por cambio de dominancia.
    # --------------------------------------------------------

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

    # Utilizamos el inicio más reciente que cumpla las condiciones.
    start_index = candidates[-1][0]

    age = (
        len(data)
        - 1
        - start_index
    )

    # --------------------------------------------------------
    # Extensión del impulso
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Fase
    # --------------------------------------------------------

    if age <= 1:

        phase = "inicio"

    elif age <= 3:

        phase = "temprano"

    elif age <= MAX_IMPULSE_AGE:

        phase = "avanzado"

    else:

        phase = "tardío"

    # --------------------------------------------------------
    # Consolidación anterior
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Score
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Validez
    # --------------------------------------------------------

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

    # Un impulso avanzado puede ser válido solamente
    # si el precio regresó a una zona estructural.
    #
    # La función principal se encargará de comprobarlo.

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
    # IMPORTANTE:
    #
    # LA ÚLTIMA FILA ES N.
    #
    # N ES LA VELA QUE BOT.PY ACABA DE CERRAR.
    #
    # NO SE USA N+1.
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
    # MÉTRICAS VELA N
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
    # CUERPO DE N
    # ========================================================

    if not _body_is_valid(
        c,
        atr,
    ):

        result[
            "reason"
        ] = (
            "Vela N demasiado pequeña "
            "o demasiado grande"
        )

        result[
            "analysis"
        ][
            "blocked_reason"
        ] = "cuerpo inválido"

        return result

    # ========================================================
    # IMPULSO EXCESIVAMENTE EXTENDIDO
    # ========================================================

    if (
        impulse[
            "extension_atr"
        ]
        > MAX_IMPULSE_TOTAL_ATR
    ):

        result[
            "reason"
        ] = (
            "Entrada bloqueada: "
            "impulso excesivamente extendido"
        )

        result[
            "zone"
        ] = "impulso_extendido"

        return result

    # ========================================================
    # DEMASIADAS VELAS CONSECUTIVAS
    # ========================================================

    if (
        impulse[
            "consecutive"
        ]
        > MAX_CONSECUTIVE_DIRECTION_CANDLES
    ):

        result[
            "reason"
        ] = (
            "Entrada bloqueada: "
            "demasiadas velas consecutivas "
            "en la misma dirección"
        )

        result[
            "zone"
        ] = "late_trend"

        return result

    # ========================================================
    # CALL
    # ========================================================

    if structure == "bullish":

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if not _ema_alignment(
            current,
            "bullish",
        ):

            result[
                "reason"
            ] = (
                "EMA no confirma "
                "estructura alcista"
            )

            return result

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        if not (
            CALL_RSI_MIN
            <= rsi
            <= CALL_RSI_MAX
        ):

            result[
                "reason"
            ] = (
                f"RSI CALL fuera "
                f"de rango {rsi:.1f}"
            )

            return result

        # ----------------------------------------------------
        # EVITAR COMPRAR CERCA DEL MÁXIMO
        # ----------------------------------------------------

        room_ok, room_atr = (
            _room_to_opposite(
                price,
                last_high,
                atr,
                "bullish",
            )
        )

        if not room_ok:

            result[
                "reason"
            ] = (
                "CALL bloqueado: "
                "poco espacio hasta "
                "el último máximo"
            )

            result[
                "zone"
            ] = "extremo_opuesto"

            result[
                "analysis"
            ][
                "room_to_opposite_atr"
            ] = room_atr

            return result

        # ----------------------------------------------------
        # RECHAZO DEL SOPORTE
        # ----------------------------------------------------

        support_ok, distance, _ = (
            _zone_test(
                c,
                last_low,
                atr,
                "support",
            )
        )

        if not support_ok:

            result[
                "reason"
            ] = (
                "Esperando rechazo real "
                "del último mínimo/soporte"
            )

            result[
                "zone"
            ] = "soporte"

            return result

        # ----------------------------------------------------
        # NO DEBE ESTAR LEJOS DEL SOPORTE
        # ----------------------------------------------------

        if (
            distance
            > MAX_ENTRY_DISTANCE_ATR
        ):

            result[
                "reason"
            ] = (
                "CALL rechazado: "
                "cierre demasiado alejado "
                "del soporte"
            )

            return result

        # ----------------------------------------------------
        # RECUPERACIÓN
        # ----------------------------------------------------

        if price <= p["close"]:

            result[
                "reason"
            ] = (
                "CALL bloqueado: "
                "el rechazo no recuperó "
                "el cierre anterior"
            )

            return result

        # ----------------------------------------------------
        # FASE DEL IMPULSO
        # ----------------------------------------------------

        phase = impulse[
            "phase"
        ]

        # Si es tardío, no entra.
        if phase == "tardío":

            result[
                "reason"
            ] = (
                "CALL bloqueado: "
                "impulso tardío"
            )

            return result

        # Un impulso avanzado solo se permite si
        # realmente existe retroceso hasta estructura.
        if (
            phase == "avanzado"
            and distance
            > MAX_ENTRY_DISTANCE_ATR
        ):

            result[
                "reason"
            ] = (
                "CALL bloqueado: "
                "impulso avanzado "
                "sin retroceso suficiente"
            )

            return result

        # ----------------------------------------------------
        # CALIDAD
        # ----------------------------------------------------

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

        if impulse[
            "phase"
        ] == "inicio":

            quality += 10.0

        elif impulse[
            "phase"
        ] == "temprano":

            quality += 7.0

        elif impulse[
            "phase"
        ] == "avanzado":

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

        # Penalización si está cerca del extremo opuesto.
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

        if quality < MIN_ENTRY_SCORE:

            result[
                "reason"
            ] = (
                f"CALL rechazado: "
                f"calidad {quality}/100"
            )

            return result

        # ----------------------------------------------------
        # SCORE FINAL
        # ----------------------------------------------------

        final_score = int(
            round(
                quality
            )
        )

        result.update(
            {
                "signal": "call",
                "score": final_score,
                "reason": (
                    "CALL | "
                    "rechazo estructural "
                    "de soporte | "
                    f"calidad={quality}/100 | "
                    f"fase={phase} | "
                    f"impulso={impulse['age']} velas | "
                    f"extensión="
                    f"{impulse['extension_atr']:.2f} ATR"
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
    # PUT
    # ========================================================

    if structure == "bearish":

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if not _ema_alignment(
            current,
            "bearish",
        ):

            result[
                "reason"
            ] = (
                "EMA no confirma "
                "estructura bajista"
            )

            return result

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        if not (
            PUT_RSI_MIN
            <= rsi
            <= PUT_RSI_MAX
        ):

            result[
                "reason"
            ] = (
                f"RSI PUT fuera "
                f"de rango {rsi:.1f}"
            )

            return result

        # ----------------------------------------------------
        # EVITAR VENDER CERCA DEL MÍNIMO
        # ----------------------------------------------------

        room_ok, room_atr = (
            _room_to_opposite(
                price,
                last_low,
                atr,
                "bearish",
            )
        )

        if not room_ok:

            result[
                "reason"
            ] = (
                "PUT bloqueado: "
                "poco espacio hasta "
                "el último mínimo"
            )

            result[
                "zone"
            ] = "extremo_opuesto"

            result[
                "analysis"
            ][
                "room_to_opposite_atr"
            ] = room_atr

            return result

        # ----------------------------------------------------
        # RECHAZO RESISTENCIA
        # ----------------------------------------------------

        resistance_ok, distance, _ = (
            _zone_test(
                c,
                last_high,
                atr,
                "resistance",
            )
        )

        if not resistance_ok:

            result[
                "reason"
            ] = (
                "Esperando rechazo real "
                "del último máximo/resistencia"
            )

            result[
                "zone"
            ] = "resistencia"

            return result

        # ----------------------------------------------------
        # NO DEBE ESTAR LEJOS
        # ----------------------------------------------------

        if (
            distance
            > MAX_ENTRY_DISTANCE_ATR
        ):

            result[
                "reason"
            ] = (
                "PUT rechazado: "
                "cierre demasiado alejado "
                "de la resistencia"
            )

            return result

        # ----------------------------------------------------
        # RECUPERACIÓN BAJISTA
        # ----------------------------------------------------

        if price >= p["close"]:

            result[
                "reason"
            ] = (
                "PUT bloqueado: "
                "el rechazo no recuperó "
                "a la baja"
            )

            return result

        # ----------------------------------------------------
        # FASE
        # ----------------------------------------------------

        phase = impulse[
            "phase"
        ]

        if phase == "tardío":

            result[
                "reason"
            ] = (
                "PUT bloqueado: "
                "impulso tardío"
            )

            return result

        if (
            phase == "avanzado"
            and distance
            > MAX_ENTRY_DISTANCE_ATR
        ):

            result[
                "reason"
            ] = (
                "PUT bloqueado: "
                "impulso avanzado "
                "sin retroceso suficiente"
            )

            return result

        # ----------------------------------------------------
        # CALIDAD
        # ----------------------------------------------------

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

        if impulse[
            "phase"
        ] == "inicio":

            quality += 10.0

        elif impulse[
            "phase"
        ] == "temprano":

            quality += 7.0

        elif impulse[
            "phase"
        ] == "avanzado":

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

        if quality < MIN_ENTRY_SCORE:

            result[
                "reason"
            ] = (
                f"PUT rechazado: "
                f"calidad {quality}/100"
            )

            return result

        final_score = int(
            round(
                quality
            )
        )

        result.update(
            {
                "signal": "put",
                "score": final_score,
                "reason": (
                    "PUT | "
                    "rechazo estructural "
                    "de resistencia | "
                    f"calidad={quality}/100 | "
                    f"fase={phase} | "
                    f"impulso={impulse['age']} velas | "
                    f"extensión="
                    f"{impulse['extension_atr']:.2f} ATR"
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
    # SEGURIDAD
    # ========================================================

    result[
        "reason"
    ] = "Sin dirección válida"

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
