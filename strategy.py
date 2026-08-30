"""
strategy.py

Estrategia estructural de RECHAZO para BINARY OTC 1M.

OBJETIVO:
- Evitar entradas de continuidad tardías cerca del último máximo/mínimo.
- Identificar dinámicamente el último máximo y último mínimo confirmados.
- Buscar CALL en rechazo de soporte / último mínimo.
- Buscar PUT en rechazo de resistencia / último máximo.
- Confirmar estructura HH/HL o LH/LL antes de permitir la entrada.
- Exigir extensión estructural entre 1.00 ATR y 1.60 ATR.
- Evitar operar cuando la extensión sea menor a 1.00 ATR.
- Evitar operar cuando la extensión sea mayor a 1.60 ATR.
- Mantener CALL/PUT.
- No ejecuta operaciones.
- Compatible con bot.py mediante:
      analyze_market(candle_1m=..., previous_m1=..., pair=...)

REGLA ATR:
    1.00 ATR <= extensión <= 1.60 ATR

Por debajo de 1.00 ATR:
    NO OPERA.

Por encima de 1.60 ATR:
    NO OPERA.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import math

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURACIÓN
# ============================================================

MIN_BARS = 35
MAX_CANDLES = 80

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50

RSI_PERIOD = 14
ATR_PERIOD = 14


# ============================================================
# PIVOTES
# ============================================================

PIVOT_LEFT = 2
PIVOT_RIGHT = 2

SWING_LOOKBACK = 35


# ============================================================
# ZONAS DINÁMICAS
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
# VELA
# ============================================================

MIN_BODY_ATR = 0.12

MAX_BODY_ATR = 1.35


# ============================================================
# ESTRUCTURA
# ============================================================

MIN_STRUCTURE_GAP_ATR = 0.05


# ============================================================
# EXTENSIÓN ATR
# ============================================================
#
# ESTA ES LA ÚNICA ZONA NUEVA DE FILTRO.
#
# La entrada solamente puede pasar cuando la extensión
# respecto al último swing estructural está entre:
#
#       1.00 ATR y 1.60 ATR
#
# 0.99 ATR  -> BLOQUEADO
# 1.00 ATR  -> PERMITIDO
# 1.30 ATR  -> PERMITIDO
# 1.60 ATR  -> PERMITIDO
# 1.61 ATR  -> BLOQUEADO
#
# ============================================================

MIN_IMPULSE_EXTENSION_ATR = 1.00

MAX_IMPULSE_EXTENSION_ATR = 1.60


# ============================================================
# RSI
# ============================================================
#
# RSI NO ES EL DISPARADOR.
# Solamente evita operar en extremos demasiado agresivos.
# ============================================================

CALL_RSI_MIN = 38.0
CALL_RSI_MAX = 68.0

PUT_RSI_MIN = 32.0
PUT_RSI_MAX = 62.0


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

        "rsi": 0.0,

        "atr": 0.0,

        # ====================================================
        # EXTENSIÓN ATR
        # ====================================================

        "impulse_extension_atr": 0.0,

        "impulse_extension_valid": False,

        "candle_timestamp": None,

        "structure": "range",

        "structure_score": 0,

        "candle": None,

        "signal_price": None,

        "candle_open": None,

        "candle_close": None,

        "distance_to_zone_atr": 0.0,

        # Compatibilidad con bot.py
        "analysis": {},
    }


# ============================================================
# FLOAT SEGURO
# ============================================================

def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:

    try:

        x = float(value)

        return (
            x
            if math.isfinite(x)
            else default
        )

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

    rename = {}

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

    rename.update({
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
    })

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


    # ========================================================
    # EMA
    # ========================================================

    out["ema9"] = close.ewm(
        span=EMA_FAST,
        adjust=False,
    ).mean()

    out["ema21"] = close.ewm(
        span=EMA_MID,
        adjust=False,
    ).mean()

    out["ema50"] = close.ewm(
        span=EMA_SLOW,
        adjust=False,
    ).mean()


    # ========================================================
    # ATR
    # ========================================================

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,

            (
                high - prev_close
            ).abs(),

            (
                low - prev_close
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


    # ========================================================
    # RSI
    # ========================================================

    delta = close.diff()

    gain = delta.clip(
        lower=0.0
    )

    loss = -delta.clip(
        upper=0.0
    )

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD,
    ).mean()

    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            np.nan,
        )
    )

    out["rsi"] = (
        100.0
        -
        (
            100.0
            /
            (1.0 + rs)
        )
    )

    out.loc[
        (avg_loss == 0)
        &
        (avg_gain > 0),
        "rsi",
    ] = 100.0

    out.loc[
        (avg_gain == 0)
        &
        (avg_loss > 0),
        "rsi",
    ] = 0.0

    return out


# ============================================================
# ATR
# ============================================================

def _atr(
    history: pd.DataFrame,
) -> float:

    if (
        history is None
        or history.empty
    ):
        return 0.0

    value = np.nan

    if "tr" in history.columns:

        value = (
            history["tr"]
            .tail(ATR_PERIOD)
            .mean()
        )

    if (
        pd.isna(value)
        or value <= 0
    ):

        value = (
            history["high"]
            -
            history["low"]
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
            *
            0.0001
        )

    return float(
        max(
            value,
            EPS,
        )
    )


# ============================================================
# DIRECCIÓN DE VELA
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


# ============================================================
# MÉTRICAS DE VELA
# ============================================================

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
        <
        left + right + 3
    ):
        return highs, lows

    start = max(
        left,
        len(history)
        -
        SWING_LOOKBACK,
    )

    end = (
        len(history)
        -
        right
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
            h
            >=
            float(
                left_highs.max()
            )
            and
            h
            >=
            float(
                right_highs.max()
            )
        ):

            highs.append(
                (
                    i,
                    h,
                )
            )

        if (
            l
            <=
            float(
                left_lows.min()
            )
            and
            l
            <=
            float(
                right_lows.min()
            )
        ):

            lows.append(
                (
                    i,
                    l,
                )
            )

    return highs, lows


# ============================================================
# ÚLTIMOS SWINGS
# ============================================================

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


    # ========================================================
    # FALLBACK
    # ========================================================

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

    swings = _last_swing_levels(
        work
    )

    highs = swings[
        "highs"
    ]

    lows = swings[
        "lows"
    ]

    if (
        len(highs) >= 2
        and
        len(lows) >= 2
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
            *
            MIN_STRUCTURE_GAP_ATR,
            EPS,
        )

        if (
            h2
            >
            h1
            +
            min_gap
            and
            l2
            >
            l1
            +
            min_gap
        ):

            return "bullish"

        if (
            h2
            <
            h1
            -
            min_gap
            and
            l2
            <
            l1
            -
            min_gap
        ):

            return "bearish"


    # ========================================================
    # FALLBACK EMA
    # ========================================================

    ind = add_indicators(
        work
    )

    last = ind.iloc[-1]

    if (
        last["ema9"]
        >
        last["ema21"]
        >
        last["ema50"]
    ):

        return "bullish"

    if (
        last["ema9"]
        <
        last["ema21"]
        <
        last["ema50"]
    ):

        return "bearish"

    return "range"


# ============================================================
# SCORE DE ESTRUCTURA
# ============================================================

def structure_score(
    df: pd.DataFrame,
) -> int:

    work = _normalize(df)

    if len(work) < 12:
        return 0

    swings = _last_swing_levels(
        work
    )

    score = 0

    highs = swings[
        "highs"
    ]

    lows = swings[
        "lows"
    ]

    if len(highs) >= 2:

        if (
            highs[-1][1]
            !=
            highs[-2][1]
        ):

            score += 1

    if len(lows) >= 2:

        if (
            lows[-1][1]
            !=
            lows[-2][1]
        ):

            score += 1

    structure = detect_structure(
        work
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
            ==
            "bullish"
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
            ==
            "bearish"
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
# NIVELES RECIENTES
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = SWING_LOOKBACK,
) -> Tuple[float, float]:

    work = _normalize(df)

    if work.empty:
        return 0.0, 0.0

    x = work.tail(
        lookback
    )

    return (
        float(
            x["low"].min()
        ),

        float(
            x["high"].max()
        ),
    )


# ============================================================
# TEST DE ZONA
# ============================================================

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


    # ========================================================
    # SOPORTE / CALL
    # ========================================================

    if side == "support":

        touched = (
            candle["low"]
            <=
            level + zone
        )

        closed_above = (
            candle["close"]
            >
            level
        )

        wick_ok = (
            candle["lower"]
            /
            candle["range"]
            >=
            MIN_REJECTION_WICK_RATIO
            or
            candle["lower"]
            >=
            candle["body"]
            *
            MIN_WICK_BODY_RATIO
        )

        close_ok = (
            candle[
                "close_position"
            ]
            >=
            MIN_CLOSE_POSITION_CALL
        )

        valid = (
            touched
            and
            closed_above
            and
            wick_ok
            and
            close_ok
        )

        distance = (
            abs(
                candle["close"]
                -
                level
            )
            /
            atr
        )

        return (
            valid,
            distance,
            "rechazo de soporte",
        )


    # ========================================================
    # RESISTENCIA / PUT
    # ========================================================

    touched = (
        candle["high"]
        >=
        level - zone
    )

    closed_below = (
        candle["close"]
        <
        level
    )

    wick_ok = (
        candle["upper"]
        /
        candle["range"]
        >=
        MIN_REJECTION_WICK_RATIO
        or
        candle["upper"]
        >=
        candle["body"]
        *
        MIN_WICK_BODY_RATIO
    )

    close_ok = (
        candle[
            "close_position"
        ]
        <=
        MAX_CLOSE_POSITION_PUT
    )

    valid = (
        touched
        and
        closed_below
        and
        wick_ok
        and
        close_ok
    )

    distance = (
        abs(
            candle["close"]
            -
            level
        )
        /
        atr
    )

    return (
        valid,
        distance,
        "rechazo de resistencia",
    )


# ============================================================
# COMPATIBILIDAD
# ============================================================

def is_near_sr(
    df: pd.DataFrame,
    tolerance: float = 0.0,
) -> bool:

    work = _normalize(df)

    if len(work) < 5:
        return True

    atr = _atr(
        add_indicators(
            work
        )
    )

    tol = (
        tolerance
        if tolerance > 0
        else
        atr * ZONE_ATR
    )

    low, high = recent_levels(
        work
    )

    price = float(
        work[
            "close"
        ].iloc[-1]
    )

    return (
        abs(
            price - low
        )
        <=
        tol
        or
        abs(
            high - price
        )
        <=
        tol
    )


# ============================================================
# ESPACIO AL NIVEL OPUESTO
# ============================================================

def _room_to_opposite(
    price: float,
    opposite_level: float,
    atr: float,
    direction: str,
) -> bool:

    if atr <= 0:
        return False

    if direction == "bullish":

        room = (
            opposite_level
            -
            price
        )

    else:

        room = (
            price
            -
            opposite_level
        )

    return (
        room
        >=
        atr
        *
        MIN_ROOM_TO_OPPOSITE_ATR
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
# EVITAR EXTREMO OPUESTO
# ============================================================

def _not_overextended(
    price: float,
    last_high: float,
    last_low: float,
    atr: float,
    direction: str,
) -> bool:

    if atr <= 0:
        return False

    if direction == "bullish":

        return (
            last_high
            -
            price
        )
        >= (
            atr
            *
            MIN_ROOM_TO_OPPOSITE_ATR
        )

    return (
        price
        -
        last_low
    ) >= (
        atr
        *
        MIN_ROOM_TO_OPPOSITE_ATR
    )


# ============================================================
# VALIDACIÓN DEL CUERPO
# ============================================================

def _body_is_valid(
    candle: Dict[str, float],
    atr: float,
) -> bool:

    if atr <= 0:
        return False

    body_atr = (
        candle["body"]
        /
        atr
    )

    return (
        candle[
            "body_ratio"
        ]
        >=
        MIN_BODY_RATIO
        and
        MIN_BODY_ATR
        <=
        body_atr
        <=
        MAX_BODY_ATR
    )


# ============================================================
# ANÁLISIS PRINCIPAL
# ============================================================

def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Optional[Any] = None,
    candles_5s: Optional[pd.DataFrame] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analiza una vela cerrada.

    COMPATIBILIDAD:

    1. Puede recibir un DataFrame directamente:

        analyze_market(df)

    2. Puede recibir la forma utilizada por bot.py:

        analyze_market(
            candle_1m=...,
            previous_m1=...,
            pair=...
        )

    La última vela recibida es la vela analizada.
    """

    result = _empty_result()


    # ========================================================
    # CONSTRUIR DATAFRAME
    # ========================================================

    if (
        previous_m1 is not None
        and isinstance(
            previous_m1,
            pd.DataFrame,
        )
    ):

        history_input = (
            previous_m1.copy()
        )

        if candle_1m is not None:

            if isinstance(
                candle_1m,
                pd.Series,
            ):

                current_row = (
                    candle_1m
                    .to_dict()
                )

            elif isinstance(
                candle_1m,
                dict,
            ):

                current_row = (
                    dict(candle_1m)
                )

            else:

                current_row = None

            if current_row is not None:

                current_df = pd.DataFrame(
                    [current_row]
                )

                clean_previous = (
                    _normalize(
                        history_input
                    )
                )

                combined = pd.concat(
                    [
                        clean_previous,
                        current_df,
                    ],
                    ignore_index=True,
                )

                clean = _normalize(
                    combined
                )

            else:

                clean = _normalize(
                    history_input
                )

        else:

            clean = _normalize(
                history_input
            )

    elif (
        df is not None
        and isinstance(
            df,
            pd.DataFrame,
        )
    ):

        clean = _normalize(df)

    else:

        result["reason"] = (
            "No hay datos de mercado"
        )

        return result


    # ========================================================
    # HISTORIAL MÍNIMO
    # ========================================================

    if len(clean) < MIN_BARS:

        result["reason"] = (
            f"Historial insuficiente "
            f"{len(clean)}/{MIN_BARS}"
        )

        return result


    # ========================================================
    # INDICADORES
    # ========================================================

    data = add_indicators(
        clean
    )

    if (
        data.empty
        or
        len(data) < MIN_BARS
    ):

        result["reason"] = (
            "Indicadores insuficientes"
        )

        return result


    # ========================================================
    # VELA ANALIZADA
    # ========================================================

    live = data.iloc[-1]

    previous = data.iloc[-2]

    history = (
        data
        .iloc[:-1]
        .copy()
    )


    # ========================================================
    # ATR
    # ========================================================

    atr = _atr(
        history
    )


    # ========================================================
    # RSI
    # ========================================================

    rsi = _safe_float(
        live.get(
            "rsi"
        ),
        50.0,
    )


    # ========================================================
    # ESTRUCTURA
    # ========================================================

    structure = detect_structure(
        history
    )

    s_score = structure_score(
        history
    )


    # ========================================================
    # SWINGS
    # ========================================================

    swings = _last_swing_levels(
        history
    )

    last_high = (
        swings[
            "last_high"
        ][1]
        if swings[
            "last_high"
        ]
        else None
    )

    last_low = (
        swings[
            "last_low"
        ][1]
        if swings[
            "last_low"
        ]
        else None
    )


    # ========================================================
    # RESULTADO BASE
    # ========================================================

    result.update({

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
            live
        ),

        "candle_timestamp": (
            int(
                live["from"]
            )
            if
            "from" in data.columns
            and
            not pd.isna(
                live["from"]
            )
            else None
        ),
    })


    # ========================================================
    # ESTRUCTURA VÁLIDA
    # ========================================================

    if structure not in (
        "bullish",
        "bearish",
    ):

        result["reason"] = (
            "Estructura lateral/ambigua"
        )

        return result


    # ========================================================
    # ATR VÁLIDO
    # ========================================================

    if (
        atr <= 0
        or
        not math.isfinite(atr)
    ):

        result["reason"] = (
            "ATR inválido"
        )

        return result


    # ========================================================
    # SWINGS VÁLIDOS
    # ========================================================

    if (
        last_high is None
        or
        last_low is None
    ):

        result["reason"] = (
            "No hay máximo/mínimo estructural"
        )

        return result


    # ========================================================
    # MÉTRICAS
    # ========================================================

    c = candle_metrics(
        live
    )

    p = candle_metrics(
        previous
    )

    price = c["close"]


    # ========================================================
    # FILTRO DEL CUERPO
    # ========================================================

    if not _body_is_valid(
        c,
        atr,
    ):

        result["reason"] = (
            "Vela de entrada "
            "demasiado pequeña/grande"
        )

        return result


    # ========================================================
    # EXTENSIÓN ATR
    # ========================================================
    #
    # IMPORTANTE:
    #
    # CALL:
    # extensión = distancia desde el último mínimo
    #             confirmado hasta el cierre.
    #
    # PUT:
    # extensión = distancia desde el último máximo
    #             confirmado hasta el cierre.
    #
    # SOLO:
    #
    # 1.00 <= extensión <= 1.60
    #
    # ========================================================

    if structure == "bullish":

        impulse_extension_atr = (
            price
            -
            last_low
        ) / atr

    else:

        impulse_extension_atr = (
            last_high
            -
            price
        ) / atr


    impulse_extension_atr = max(
        0.0,
        float(
            impulse_extension_atr
        ),
    )


    extension_valid = (
        MIN_IMPULSE_EXTENSION_ATR
        <=
        impulse_extension_atr
        <=
        MAX_IMPULSE_EXTENSION_ATR
    )


    result.update({

        "impulse_extension_atr":
            impulse_extension_atr,

        "impulse_extension_valid":
            extension_valid,
    })


    # ========================================================
    # BLOQUEO ATR
    # ========================================================

    if not extension_valid:

        result["zone"] = (
            "extension_atr"
        )

        result["reason"] = (
            "Extensión ATR fuera "
            "del rango permitido | "
            f"extensión="
            f"{impulse_extension_atr:.2f} ATR | "
            "permitido=1.00-1.60 ATR"
        )

        return result


    # ========================================================
    # EVITAR EXTREMO OPUESTO
    # ========================================================

    if not _not_overextended(
        price,
        last_high,
        last_low,
        atr,
        structure,
    ):

        if structure == "bullish":

            result["reason"] = (
                "CALL bloqueado "
                "cerca del máximo"
            )

        else:

            result["reason"] = (
                "PUT bloqueado "
                "cerca del mínimo"
            )

        result["zone"] = (
            "extremo_opuesto"
        )

        return result


    # ========================================================
    # CALL
    # ========================================================

    if structure == "bullish":

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        if not _ema_alignment(
            live,
            "bullish",
        ):

            result["reason"] = (
                "EMA no confirma "
                "estructura alcista"
            )

            return result


        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        if not (
            CALL_RSI_MIN
            <=
            rsi
            <=
            CALL_RSI_MAX
        ):

            result["reason"] = (
                f"RSI CALL fuera "
                f"de rango {rsi:.1f}"
            )

            return result


        # ----------------------------------------------------
        # TEST DE SOPORTE
        # ----------------------------------------------------

        support_ok, distance, zone_reason = (
            _zone_test(
                c,
                last_low,
                atr,
                "support",
            )
        )


        if not support_ok:

            result["reason"] = (
                "Esperando rechazo "
                "real del último mínimo"
            )

            result["zone"] = (
                "soporte"
            )

            return result


        # ----------------------------------------------------
        # DISTANCIA DE ZONA
        # ----------------------------------------------------

        if (
            distance
            >
            MAX_ENTRY_DISTANCE_ATR
        ):

            result["reason"] = (
                "Cierre demasiado "
                "alejado del soporte"
            )

            return result


        # ----------------------------------------------------
        # ESPACIO AL MÁXIMO
        # ----------------------------------------------------

        if not _room_to_opposite(
            price,
            last_high,
            atr,
            "bullish",
        ):

            result["reason"] = (
                "Poco espacio hasta "
                "el último máximo"
            )

            return result


        # ----------------------------------------------------
        # RECUPERACIÓN
        # ----------------------------------------------------

        if (
            price
            <=
            p["close"]
        ):

            result["reason"] = (
                "Rechazo sin "
                "recuperación suficiente"
            )

            return result


        # ----------------------------------------------------
        # CALIDAD
        # ----------------------------------------------------

        wick_strength = (
            c["lower"]
            /
            max(
                c["range"],
                EPS,
            )
        )

        quality = int(
            round(
                55
                +
                min(
                    20.0,
                    wick_strength
                    *
                    30.0,
                )
                +
                min(
                    15.0,
                    s_score
                    *
                    3.0,
                )
                +
                (
                    5.0
                    if
                    c[
                        "close_position"
                    ]
                    >=
                    0.72
                    else 0.0
                )
            )
        )

        quality = min(
            100,
            max(
                0,
                quality,
            ),
        )


        if quality < 70:

            result["reason"] = (
                f"Rechazo CALL "
                f"débil ({quality}/100)"
            )

            return result


        # ====================================================
        # CALL VÁLIDO
        # ====================================================

        result.update({

            "signal": "call",

            "score": min(
                10,
                max(
                    7,
                    int(
                        round(
                            quality
                            /
                            10.0
                        )
                    ),
                ),
            ),

            "reason": (
                "CALL | rechazo "
                "estructural de soporte | "
                f"calidad={quality}/100 | "
                f"extensión="
                f"{impulse_extension_atr:.2f} ATR"
            ),

            "continuity": True,

            "blocked": False,

            "zone": (
                "soporte_rechazado"
            ),

            "entry_type": (
                "rejection_support"
            ),

            "entry_quality": quality,

            "signal_price": price,

            "candle_open": c["open"],

            "candle_close": c["close"],

            "distance_to_zone_atr":
                distance,
        })

        return result


    # ========================================================
    # PUT
    # ========================================================

    if not _ema_alignment(
        live,
        "bearish",
    ):

        result["reason"] = (
            "EMA no confirma "
            "estructura bajista"
        )

        return result


    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if not (
        PUT_RSI_MIN
        <=
        rsi
        <=
        PUT_RSI_MAX
    ):

        result["reason"] = (
            f"RSI PUT fuera "
            f"de rango {rsi:.1f}"
        )

        return result


    # --------------------------------------------------------
    # TEST DE RESISTENCIA
    # --------------------------------------------------------

    resistance_ok, distance, zone_reason = (
        _zone_test(
            c,
            last_high,
            atr,
            "resistance",
        )
    )


    if not resistance_ok:

        result["reason"] = (
            "Esperando rechazo "
            "real del último máximo"
        )

        result["zone"] = (
            "resistencia"
        )

        return result


    # --------------------------------------------------------
    # DISTANCIA
    # --------------------------------------------------------

    if (
        distance
        >
        MAX_ENTRY_DISTANCE_ATR
    ):

        result["reason"] = (
            "Cierre demasiado "
            "alejado de la resistencia"
        )

        return result


    # --------------------------------------------------------
    # ESPACIO AL MÍNIMO
    # --------------------------------------------------------

    if not _room_to_opposite(
        price,
        last_low,
        atr,
        "bearish",
    ):

        result["reason"] = (
            "Poco espacio hasta "
            "el último mínimo"
        )

        return result


    # --------------------------------------------------------
    # RECUPERACIÓN BAJISTA
    # --------------------------------------------------------

    if (
        price
        >=
        p["close"]
    ):

        result["reason"] = (
            "Rechazo sin "
            "recuperación bajista suficiente"
        )

        return result


    # --------------------------------------------------------
    # CALIDAD
    # --------------------------------------------------------

    wick_strength = (
        c["upper"]
        /
        max(
            c["range"],
            EPS,
        )
    )

    quality = int(
        round(
            55
            +
            min(
                20.0,
                wick_strength
                *
                30.0,
            )
            +
            min(
                15.0,
                s_score
                *
                3.0,
            )
            +
            (
                5.0
                if
                c[
                    "close_position"
                ]
                <=
                0.28
                else 0.0
            )
        )
    )

    quality = min(
        100,
        max(
            0,
            quality,
        ),
    )


    if quality < 70:

        result["reason"] = (
            f"Rechazo PUT "
            f"débil ({quality}/100)"
        )

        return result


    # ========================================================
    # PUT VÁLIDO
    # ========================================================

    result.update({

        "signal": "put",

        "score": min(
            10,
            max(
                7,
                int(
                    round(
                        quality
                        /
                        10.0
                    )
                ),
            ),
        ),

        "reason": (
            "PUT | rechazo "
            "estructural de resistencia | "
            f"calidad={quality}/100 | "
            f"extensión="
            f"{impulse_extension_atr:.2f} ATR"
        ),

        "continuity": True,

        "blocked": False,

        "zone": (
            "resistencia_rechazada"
        ),

        "entry_type": (
            "rejection_resistance"
        ),

        "entry_quality": quality,

        "signal_price": price,

        "candle_open": c["open"],

        "candle_close": c["close"],

        "distance_to_zone_atr":
            distance,
    })

    return result


# ============================================================
# API COMPATIBLE
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
# EJECUCIÓN DIRECTA
# ============================================================

if __name__ == "__main__":

    print(
        "strategy.py cargado correctamente."
    )

    print(
        "Estrategia: RECHAZO ESTRUCTURAL"
    )

    print(
        "Filtro extensión ATR: "
        "1.00 - 1.60 ATR"
    )

    print(
        "API principal: analyze_market()"
    )
