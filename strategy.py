"""
strategy.py

ESTRATEGIA ESTRUCTURAL DE RECHAZO + FASE DE IMPULSO
+ CONTINUIDAD + DESCANSO + INDECISIÓN + FUERZA + DIVERGENCIA RSI
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
7. Extensión en ATR.
8. Agotamiento.
9. Rechazo estructural.
10. Soporte y resistencia.
11. Línea de tendencia dinámica.
12. Velas de continuidad.
13. Velas de descanso.
14. Velas de indecisión.
15. Velas de fuerza.
16. Divergencia RSI.
17. Confirmación N+1.

REGLAS PRINCIPALES
------------------

REVERSIÓN:
    Solo se permite cuando existe rechazo real
    en soporte o resistencia.

DESCANSO:
    Solo se opera a favor de la tendencia.

INDECISIÓN:
    La vela N no genera entrada.
    Se espera N+1.
    N+1 debe estar libre de:
        - reversión
        - soporte/resistencia
        - línea de tendencia.

CONTINUIDAD:
    Primero debe existir tendencia.
    La vela N debe ser de continuidad.
    La entrada se confirma en N+1.
    No CALL cerca de resistencia.
    No PUT cerca de soporte.

FUERZA:
    Vela N fuerte.
    Solo al comienzo del movimiento.
    No operar si está cerca de:
        - soporte
        - resistencia
        - línea de tendencia.

DIVERGENCIA:
    CALL:
        Precio LL + RSI HL
        y estructura alcista.

    PUT:
        Precio HH + RSI LH
        y estructura bajista.

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


# ============================================================
# NUEVAS REGLAS DE ENTRADA
# ============================================================

MIN_DISTANCE_FROM_ZONE_ATR = 0.35

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

    # --------------------------------------------------------
    # API ANTIGUA
    # --------------------------------------------------------

    if df is not None:

        clean = _normalize(df)

        return clean

    # --------------------------------------------------------
    # API NUEVA
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

    c = _safe_float
