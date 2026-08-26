from __future__ import annotations

from typing import Optional
import pandas as pd


# ============================================================
# CONFIGURACIÓN
# ============================================================

MIN_SCORE_TO_TRADE = 75

MIN_CANDLES = 8

MIN_BODY_RATIO = 0.30

MAX_DOJI_RATIO = 0.25


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

    df = df.copy()

    for column in required:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df.dropna(
        subset=list(required),
        inplace=True,
    )

    df.reset_index(
        drop=True,
        inplace=True,
    )

    return df


# ============================================================
# CARACTERÍSTICAS DE UNA VELA
# ============================================================

def candle_info(candle):

    open_price = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])

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
            "bull": False,
            "bear": False,
            "range": 0,
            "body": 0,
            "body_ratio": 0,
            "upper_wick": 0,
            "lower_wick": 0,
        }

    body_ratio = (
        body / total_range
    )

    return {
        "bull": close > open_price,
        "bear": close < open_price,
        "range": total_range,
        "body": body,
        "body_ratio": body_ratio,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
    }


# ============================================================
# ANALIZAR ESTRUCTURA
# ============================================================

def market_structure(df):

    if len(df) < 6:
        return "NEUTRAL"

    closes = df["close"].tail(6).tolist()

    rising = 0
    falling = 0

    for i in range(1, len(closes)):

        if closes[i] > closes[i - 1]:
            rising += 1

        elif closes[i] < closes[i - 1]:
            falling += 1

    if rising >= 4 and closes[-1] > closes[0]:

        return "BULLISH"

    if falling >= 4 and closes[-1] < closes[0]:

        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# CONTINUIDAD
# ============================================================

def continuity_score(
    candles,
    direction,
):

    score = 0

    for candle in candles:

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
# IMPULSO
# ============================================================

def momentum_score(
    candles,
    direction,
):

    score = 0

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        current_close = float(
            current["close"]
        )

        previous_close = float(
            previous["close"]
        )

        current_open = float(
            current["open"]
        )

        current_high = float(
            current["high"]
        )

        current_low = float(
            current["low"]
        )

        current_range = (
            current_high
            - current_low
        )

        current_body = abs(
            current_close
            - current_open
        )

        if current_range <= 0:
            continue

        body_ratio = (
            current_body
            / current_range
        )

        if (
            direction == "BULLISH"
            and current_close > previous_close
            and current_close > current_open
            and body_ratio >= 0.40
        ):

            score += 1

        elif (
            direction == "BEARISH"
            and current_close < previous_close
            and current_close < current_open
            and body_ratio >= 0.40
        ):

            score += 1

    return score


# ============================================================
# RECHAZO
# ============================================================

def rejection_score(
    candle,
    direction,
):

    info = candle_info(candle)

    body = info["body"]

    if body <= 0:
        return 0

    # --------------------------------------------------------
    # RECHAZO ALCISTA
    # --------------------------------------------------------

    if direction == "BULLISH":

        if (
            info["lower_wick"]
            >= body * 0.50
            and info["bull"]
        ):

            return 2

        if (
            info["lower_wick"]
            >= body * 0.80
        ):

            return 1


    # --------------------------------------------------------
    # RECHAZO BAJISTA
    # --------------------------------------------------------

    if direction == "BEARISH":

        if (
            info["upper_wick"]
            >= body * 0.50
            and info["bear"]
        ):

            return 2

        if (
            info["upper_wick"]
            >= body * 0.80
        ):

            return 1

    return 0


# ============================================================
# FUERZA DE LA ÚLTIMA VELA
# ============================================================

def last_candle_score(
    candle,
    direction,
):

    info = candle_info(candle)

    if info["range"] <= 0:
        return 0

    # --------------------------------------------------------
    # EVITAR DOJI / VELA BASURA
    # --------------------------------------------------------

    if (
        info["body_ratio"]
        < MAX_DOJI_RATIO
    ):

        return 0


    # --------------------------------------------------------
    # ALCISTA
    # --------------------------------------------------------

    if direction == "BULLISH":

        if info["bull"]:

            if info["body_ratio"] >= 0.70:
                return 3

            if info["body_ratio"] >= 0.50:
                return 2

            return 1


    # --------------------------------------------------------
    # BAJISTA
    # --------------------------------------------------------

    if direction == "BEARISH":

        if info["bear"]:

            if info["body_ratio"] >= 0.70:
                return 3

            if info["body_ratio"] >= 0.50:
                return 2

            return 1

    return 0


# ============================================================
# EVITAR ENTRADA CONTRA UNA VELA MUY FUERTE
# ============================================================

def exhaustion_check(
    df,
    direction,
):

    if len(df) < 4:
        return False

    last_four = df.tail(4)

    same_direction = 0

    for _, candle in last_four.iterrows():

        info = candle_info(candle)

        if (
            direction == "BULLISH"
            and info["bull"]
        ):

            same_direction += 1

        elif (
            direction == "BEARISH"
            and info["bear"]
        ):

            same_direction += 1

    # Si las 4 últimas velas fueron
    # extremadamente continuas, evitamos
    # entrar tarde.

    if same_direction >= 4:

        last = candle_info(
            df.iloc[-1]
        )

        if last["body_ratio"] >= 0.75:

            return True

    return False


# ============================================================
# SOPORTE / RESISTENCIA BÁSICA
# ============================================================

def near_recent_extreme(
    df,
    direction,
):

    if len(df) < 10:
        return False

    recent = df.iloc[:-1].tail(9)

    last_close = float(
        df["close"].iloc[-1]
    )

    recent_high = float(
        recent["high"].max()
    )

    recent_low = float(
        recent["low"].min()
    )

    recent_range = (
        recent_high
        - recent_low
    )

    if recent_range <= 0:
        return False

    tolerance = (
        recent_range * 0.12
    )

    # --------------------------------------------------------
    # ALCISTA cerca de máximo
    # --------------------------------------------------------

    if direction == "BULLISH":

        if (
            abs(
                last_close
                - recent_high
            )
            <= tolerance
        ):

            return True


    # --------------------------------------------------------
    # BAJISTA cerca de mínimo
    # --------------------------------------------------------

    if direction == "BEARISH":

        if (
            abs(
                last_close
                - recent_low
            )
            <= tolerance
        ):

            return True

    return False


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def analyze_market(
    candle_1m,
    previous_m1=None,
    pair: str = None,
):

    result = {

        "valid": False,

        "signal": None,

        "score": 0,

        "direction": "NEUTRAL",

        "pair": pair,

    }


    # --------------------------------------------------------
    # PAR OBLIGATORIO
    # --------------------------------------------------------

    if not pair:

        return result


    # --------------------------------------------------------
    # PREPARAR DATOS
    # --------------------------------------------------------

    hist = safe_dataframe(
        previous_m1
    )

    if len(hist) < MIN_CANDLES:

        return result


    # --------------------------------------------------------
    # ESTRUCTURA
    # --------------------------------------------------------

    direction = market_structure(
        hist
    )

    result["direction"] = direction


    if direction == "NEUTRAL":

        return result


    # --------------------------------------------------------
    # ÚLTIMAS VELAS
    # --------------------------------------------------------

    candles = hist.tail(6)


    # --------------------------------------------------------
    # CONTINUIDAD
    # --------------------------------------------------------

    continuity = continuity_score(
        candles.to_dict("records"),
        direction,
    )


    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum = momentum_score(
        candles.to_dict("records"),
        direction,
    )


    # --------------------------------------------------------
    # ÚLTIMA VELA
    # --------------------------------------------------------

    last = hist.iloc[-1]

    last_score = last_candle_score(
        last,
        direction,
    )


    # --------------------------------------------------------
    # RECHAZO
    # --------------------------------------------------------

    rejection = rejection_score(
        last,
        direction,
    )


    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0


    # Estructura
    if direction in (
        "BULLISH",
        "BEARISH",
    ):

        score += 25


    # Continuidad
    score += min(
        continuity * 7,
        28,
    )


    # Momentum
    score += min(
        momentum * 5,
        20,
    )


    # Última vela
    score += last_score * 5


    # Rechazo
    score += rejection * 4


    # --------------------------------------------------------
    # BONIFICACIÓN POR PROGRESO
    # --------------------------------------------------------

    closes = hist["close"].tail(6)

    progress = 0

    for i in range(
        1,
        len(closes),
    ):

        if (
            direction == "BULLISH"
            and closes.iloc[i]
            > closes.iloc[i - 1]
        ):

            progress += 1

        elif (
            direction == "BEARISH"
            and closes.iloc[i]
            < closes.iloc[i - 1]
        ):

            progress += 1


    score += min(
        progress * 3,
        15,
    )


    # --------------------------------------------------------
    # PENALIZACIÓN POR EXTREMO
    # --------------------------------------------------------

    if near_recent_extreme(
        hist,
        direction,
    ):

        score -= 10


    # --------------------------------------------------------
    # PENALIZACIÓN POR AGOTAMIENTO
    # --------------------------------------------------------

    exhausted = exhaustion_check(
        hist,
        direction,
    )

    if exhausted:

        score -= 20


    # --------------------------------------------------------
    # LIMITAR SCORE
    # --------------------------------------------------------

    score = max(
        0,
        min(
            int(score),
            100,
        ),
    )

    result["score"] = score


    # --------------------------------------------------------
    # SEÑAL
    # --------------------------------------------------------

    if score < MIN_SCORE_TO_TRADE:

        return result


    # --------------------------------------------------------
    # EVITAR ENTRADA SI LA ÚLTIMA VELA
    # NO CONFIRMA LA DIRECCIÓN
    # --------------------------------------------------------

    last_info = candle_info(
        last
    )


    if direction == "BULLISH":

        if not last_info["bull"]:

            return result

        result["signal"] = "call"


    elif direction == "BEARISH":

        if not last_info["bear"]:

            return result

        result["signal"] = "put"


    else:

        return result


    # --------------------------------------------------------
    # VALIDACIÓN FINAL
    # --------------------------------------------------------

    result["valid"] = True

    return result
