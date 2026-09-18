"""
strategy.py

Estrategia estructural de RECHAZO para DIGITAL OTC 1M.

Objetivo:
- Evitar entradas de continuidad tardías cerca del último máximo/mínimo.
- Identificar dinámicamente el último máximo y último mínimo confirmados.
- Permitir únicamente CALL/PUT con rechazo confirmado en su zona correspondiente.
- CALL: estructura bullish + rechazo de soporte / último mínimo.
- PUT: estructura bearish + rechazo de resistencia / último máximo.
- Exigir score y calidad mínimos de 95/100, ejecución N+1 y expiración de 1 minuto.
- Mantener una API compatible con bot.py: analyze_market(df), get_signal(), signal().

IMPORTANTE:
El módulo NO ejecuta operaciones. Si bot.py trabaja con N como vela viva y
N+1 como vela de entrada, este módulo solo genera la señal; la ejecución sigue
siendo responsabilidad de bot.py.
"""

from __future__ import annotations

from dataclasses import dataclass
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

# Pivotes confirmados. 2/2 evita usar un extremo que todavía no está confirmado.
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
SWING_LOOKBACK = 35

# Zona dinámica basada en ATR.
ZONE_ATR = 0.28
MAX_ENTRY_DISTANCE_ATR = 0.55
MIN_ROOM_TO_OPPOSITE_ATR = 0.70
MIN_RECENT_ROOM_ATR = 0.55
MIN_TREND_SLOPE_ATR = 0.08
MIN_SIGNAL_QUALITY = 95
MIN_SIGNAL_SCORE = 95

# Rechazo.
MIN_BODY_RATIO = 0.25
MIN_REJECTION_WICK_RATIO = 0.35
MIN_WICK_BODY_RATIO = 1.15
MIN_CLOSE_POSITION_CALL = 0.62
MAX_CLOSE_POSITION_PUT = 0.38

# Evita velas de continuación exageradas.
MIN_BODY_ATR = 0.12
MAX_BODY_ATR = 1.35

# Tendencia / estructura.
MIN_STRUCTURE_GAP_ATR = 0.05

# RSI: no se usa como disparador; solo evita perseguir extremos.
CALL_RSI_MIN = 38.0
CALL_RSI_MAX = 68.0
PUT_RSI_MIN = 32.0
PUT_RSI_MAX = 62.0

EPS = 1e-12


@dataclass
class Signal:
    """Adaptador de salida compatible con bot.py."""
    action: str = "none"
    score: int = 0
    entry_price: Optional[float] = None
    candle_time: Optional[int] = None
    reason: str = "sin señal"
    support: Optional[float] = None
    resistance: Optional[float] = None
    quality: int = 0
    entry_type: Optional[str] = None


# ============================================================
# RESULTADO ESTABLE
# ============================================================

def _empty_result(reason: str = "sin señal") -> Dict[str, Any]:
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
        "quality": 0,
        "score_100": 0,
        "execution": "N+1",
        "expiration_minutes": 1,
        "entry_for_next_candle": False,
        "indecision_requires_n_plus_1": True,
        "last_swing_high": None,
        "last_swing_low": None,
        "support": None,
        "resistance": None,
        "rsi": 0.0,
        "atr": 0.0,
        "candle_timestamp": None,
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


# ============================================================
# NORMALIZACIÓN
# ============================================================

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    rename = {}
    if "max" in out.columns and "high" not in out.columns:
        rename["max"] = "high"
    if "min" in out.columns and "low" not in out.columns:
        rename["min"] = "low"

    rename.update({
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
    })

    out.rename(columns=rename, inplace=True)

    required = ["open", "high", "low", "close"]

    if any(c not in out.columns for c in required):
        return pd.DataFrame()

    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out.sort_values("from", inplace=True)
        out.drop_duplicates(subset=["from"], keep="last", inplace=True)

    out.dropna(subset=required, inplace=True)
    out.reset_index(drop=True, inplace=True)

    if len(out) > MAX_CANDLES:
        out = out.tail(MAX_CANDLES).reset_index(drop=True)

    return out


# ============================================================
# INDICADORES
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize(df)

    if out.empty:
        return out

    close = out["close"]
    high = out["high"]
    low = out["low"]

    out["ema9"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    out["ema21"] = close.ewm(span=EMA_MID, adjust=False).mean()
    out["ema50"] = close.ewm(span=EMA_SLOW, adjust=False).mean()

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["tr"] = tr
    out["atr"] = tr.rolling(
        ATR_PERIOD,
        min_periods=ATR_PERIOD
    ).mean()

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

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

    rs = avg_gain / avg_loss.replace(0, np.nan)

    out["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    out.loc[
        (avg_loss == 0) & (avg_gain > 0),
        "rsi"
    ] = 100.0

    out.loc[
        (avg_gain == 0) & (avg_loss > 0),
        "rsi"
    ] = 0.0

    return out


def _atr(history: pd.DataFrame) -> float:
    if history is None or history.empty:
        return 0.0

    value = (
        history["tr"].tail(ATR_PERIOD).mean()
        if "tr" in history
        else np.nan
    )

    if pd.isna(value) or value <= 0:
        value = (
            history["high"] - history["low"]
        ).tail(ATR_PERIOD).mean()

    if pd.isna(value) or value <= 0:
        value = abs(
            float(history["close"].iloc[-1])
        ) * 0.0001

    return float(max(value, EPS))


# ============================================================
# VELA
# ============================================================

def candle_direction(candle: pd.Series) -> str:
    o = _safe_float(candle.get("open"))
    c = _safe_float(candle.get("close"))

    if c > o:
        return "bull"

    if c < o:
        return "bear"

    return "neutral"


def candle_metrics(candle: pd.Series) -> Dict[str, float]:
    o = _safe_float(candle.get("open"))
    h = _safe_float(candle.get("high"))
    l = _safe_float(candle.get("low"))
    c = _safe_float(candle.get("close"))

    rng = max(h - l, EPS)
    body = abs(c - o)
    upper = max(h - max(o, c), 0.0)
    lower = max(min(o, c) - l, 0.0)

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "upper": upper,
        "lower": lower,
        "body_ratio": body / rng,
        "close_position": (c - l) / rng,
    }


# ============================================================
# PIVOTES / ÚLTIMO MÁXIMO / ÚLTIMO MÍNIMO
# ============================================================

def _confirmed_swings(
    history: pd.DataFrame,
    left: int = PIVOT_LEFT,
    right: int = PIVOT_RIGHT,
) -> Tuple[
    list[Tuple[int, float]],
    list[Tuple[int, float]]
]:
    """Devuelve pivotes confirmados: [(index, price), ...]."""

    highs: list[Tuple[int, float]] = []
    lows: list[Tuple[int, float]] = []

    if history is None or len(history) < left + right + 3:
        return highs, lows

    start = max(left, len(history) - SWING_LOOKBACK)
    end = len(history) - right

    for i in range(start, end):
        h = float(history["high"].iloc[i])
        l = float(history["low"].iloc[i])

        left_highs = history["high"].iloc[i - left:i]
        right_highs = history["high"].iloc[i + 1:i + right + 1]

        left_lows = history["low"].iloc[i - left:i]
        right_lows = history["low"].iloc[i + 1:i + right + 1]

        if (
            h >= float(left_highs.max())
            and h >= float(right_highs.max())
        ):
            highs.append((i, h))

        if (
            l <= float(left_lows.min())
            and l <= float(right_lows.min())
        ):
            lows.append((i, l))

    return highs, lows
        if direction == "bullish":
        return close >= e21 and e9 >= e21 and e21 >= e50

    if direction == "bearish":
        return close <= e21 and e9 <= e21 and e21 <= e50

    return False


def _ema_slope_ok(
    history: pd.DataFrame,
    direction: str,
    atr: float,
) -> bool:
    if len(history) < 6 or atr <= 0:
        return False

    ind = add_indicators(history)

    if len(ind) < 6:
        return False

    current = _safe_float(ind["ema21"].iloc[-1])
    previous = _safe_float(ind["ema21"].iloc[-6])

    slope = current - previous
    minimum = atr * MIN_TREND_SLOPE_ATR

    if direction == "bullish":
        return slope >= minimum

    if direction == "bearish":
        return slope <= -minimum

    return False


def _rsi_ok(rsi: float, direction: str) -> bool:
    if not math.isfinite(rsi):
        return False

    if direction == "bullish":
        return CALL_RSI_MIN <= rsi <= CALL_RSI_MAX

    if direction == "bearish":
        return PUT_RSI_MIN <= rsi <= PUT_RSI_MAX

    return False


def _body_ok(metrics: Dict[str, float], atr: float) -> bool:
    if atr <= 0:
        return False

    body = metrics["body"]

    if body < atr * MIN_BODY_ATR:
        return False

    if body > atr * MAX_BODY_ATR:
        return False

    if metrics["body_ratio"] < MIN_BODY_RATIO:
        return False

    return True


def _distance_ok(distance_atr: float) -> bool:
    return (
        math.isfinite(distance_atr)
        and distance_atr <= MAX_ENTRY_DISTANCE_ATR
    )


def _recent_room_ok(
    history: pd.DataFrame,
    price: float,
    atr: float,
    direction: str,
) -> bool:
    if atr <= 0 or history is None or history.empty:
        return False

    recent = history.tail(10)

    if direction == "bullish":
        recent_high = float(recent["high"].max())
        return (recent_high - price) >= atr * MIN_RECENT_ROOM_ATR

    if direction == "bearish":
        recent_low = float(recent["low"].min())
        return (price - recent_low) >= atr * MIN_RECENT_ROOM_ATR

    return False


# ============================================================
# PUNTUACIÓN DE LA SEÑAL
# ============================================================

def _score_signal(
    direction: str,
    structure: str,
    structure_points: int,
    ema_ok: bool,
    slope_ok: bool,
    rsi_ok: bool,
    body_ok: bool,
    rejection_ok: bool,
    distance_ok: bool,
    room_ok: bool,
    recent_room_ok: bool,
) -> Tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if structure == direction:
        score += 25
        reasons.append("estructura confirmada")
    else:
        reasons.append("estructura no confirmada")

    score += min(structure_points * 3, 15)

    if ema_ok:
        score += 15
        reasons.append("alineación EMA")
    else:
        reasons.append("EMA no alineada")

    if slope_ok:
        score += 10
        reasons.append("pendiente favorable")
    else:
        reasons.append("pendiente insuficiente")

    if rsi_ok:
        score += 5
        reasons.append("RSI permitido")
    else:
        reasons.append("RSI fuera de rango")

    if body_ok:
        score += 5
        reasons.append("cuerpo válido")
    else:
        reasons.append("cuerpo inválido")

    if rejection_ok:
        score += 15
        reasons.append("rechazo confirmado")
    else:
        reasons.append("rechazo no confirmado")

    if distance_ok:
        score += 5
        reasons.append("distancia adecuada")
    else:
        reasons.append("distancia excesiva")

    if room_ok:
        score += 3
        reasons.append("espacio hacia nivel opuesto")
    else:
        reasons.append("poco espacio hacia nivel opuesto")

    if recent_room_ok:
        score += 2
        reasons.append("espacio reciente suficiente")
    else:
        reasons.append("espacio reciente insuficiente")

    return min(score, 100), reasons


# ============================================================
# ANÁLISIS PRINCIPAL
# ============================================================

def analyze_market(
    df: pd.DataFrame,
    min_score: int = MIN_SIGNAL_SCORE,
) -> Dict[str, Any]:
    result = _empty_result()

    work = _normalize(df)

    if len(work) < MIN_BARS:
        result["reason"] = (
            f"historial insuficiente: {len(work)}/{MIN_BARS} velas"
        )
        return result

    ind = add_indicators(work)

    if ind.empty or len(ind) < MIN_BARS:
        result["reason"] = "indicadores insuficientes"
        return result

    last = ind.iloc[-1]
    metrics = candle_metrics(last)

    atr = _atr(ind)

    if atr <= 0:
        result["reason"] = "ATR inválido"
        return result

    structure = detect_structure(work)
    structure_points = structure_score(work)

    swings = _last_swing_levels(work)

    last_high = swings["last_high"]
    last_low = swings["last_low"]

    resistance = (
        float(last_high[1])
        if last_high is not None
        else float(work["high"].tail(SWING_LOOKBACK).max())
    )

    support = (
        float(last_low[1])
        if last_low is not None
        else float(work["low"].tail(SWING_LOOKBACK).min())
    )

    price = metrics["close"]
    rsi = _safe_float(last.get("rsi"), 50.0)

    result.update({
        "direction": structure,
        "trend": structure,
        "last_swing_high": resistance,
        "last_swing_low": support,
        "support": support,
        "resistance": resistance,
        "rsi": rsi,
        "atr": atr,
        "candle_timestamp": last.get("from"),
    })

    if structure not in ("bullish", "bearish"):
        result["reason"] = "estructura lateral o no confirmada"
        return result

    ema_ok = _ema_alignment(last, structure)
    slope_ok = _ema_slope_ok(work, structure, atr)
    rsi_valid = _rsi_ok(rsi, structure)
    body_valid = _body_ok(metrics, atr)

    if structure == "bullish":
        level = support
        rejection_valid, distance, rejection_reason = _zone_test(
            metrics,
            level,
            atr,
            "support",
        )
        entry_type = "support_rejection"
        opposite_level = resistance

    else:
        level = resistance
        rejection_valid, distance, rejection_reason = _zone_test(
            metrics,
            level,
            atr,
            "resistance",
        )
        entry_type = "resistance_rejection"
        opposite_level = support

    distance_valid = _distance_ok(distance)

    room_valid = _room_to_opposite(
        price,
        opposite_level,
        atr,
        structure,
    )

    recent_room_valid = _recent_room_ok(
        work,
        price,
        atr,
        structure,
    )

    score, reasons = _score_signal(
        direction=structure,
        structure=structure,
        structure_points=structure_points,
        ema_ok=ema_ok,
        slope_ok=slope_ok,
        rsi_ok=rsi_valid,
        body_ok=body_valid,
        rejection_ok=rejection_valid,
        distance_ok=distance_valid,
        room_ok=room_valid,
        recent_room_ok=recent_room_valid,
    )

    quality = score

    all_valid = all([
        ema_ok,
        slope_ok,
        rsi_valid,
        body_valid,
        rejection_valid,
        distance_valid,
        room_valid,
        recent_room_valid,
        score >= min_score,
    ])

    action = None

    if all_valid:
        action = "call" if structure == "bullish" else "put"

    reason = (
        f"{rejection_reason}; "
        + ", ".join(reasons)
    )

    result.update({
        "signal": action,
        "score": score,
        "score_100": score,
        "quality": quality,
        "entry_quality": quality,
        "entry_type": entry_type,
        "reason": reason,
        "blocked": action is None,
        "continuity": False,
        "entry_for_next_candle": action is not None,
        "expiration_minutes": 1,
    })

    if action is not None:
        result["execution"] = "N+1"
    else:
        result["execution"] = "blocked"

    return result
# ============================================================
# COMPATIBILIDAD
# ============================================================

def _candles_to_dataframe(candles: Any) -> pd.DataFrame:
    """Convierte las velas de IQ Option (lista de dicts) a DataFrame."""
    if isinstance(candles, pd.DataFrame):
        return candles.copy()

    if candles is None:
        return pd.DataFrame()

    try:
        return pd.DataFrame(list(candles))
    except (TypeError, ValueError):
        return pd.DataFrame()


def analyze_rejection(
    candles: Any,
    min_score: int = MIN_SIGNAL_SCORE
) -> Signal:
    """API compatible con bot.py para analizar velas y devolver Signal."""

    result = analyze_market(
        _candles_to_dataframe(candles)
    )

    score = int(
        result.get(
            "score_100",
            result.get("score", 0)
        ) or 0
    )

    action = (
        result.get("signal")
        if not result.get("blocked", True)
        else None
    )

    if action not in ("call", "put") or score < int(min_score):
        action = "none"

    entry_price = result.get("signal_price")

    if entry_price is None:
        entry_price = result.get("candle_close")

    candle_time = result.get("candle_timestamp")

    try:
        candle_time = (
            int(candle_time)
            if candle_time is not None
            else None
        )
    except (TypeError, ValueError):
        candle_time = None

    return Signal(
        action=action,
        score=score,
        entry_price=(
            float(entry_price)
            if entry_price is not None
            else None
        ),
        candle_time=candle_time,
        reason=str(
            result.get("reason", "sin señal")
        ),
        support=result.get("support"),
        resistance=result.get("resistance"),
        quality=int(
            result.get(
                "quality",
                result.get("entry_quality", 0)
            ) or 0
        ),
        entry_type=result.get("entry_type"),
    )


def get_signal(
    df: pd.DataFrame
) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(
    df: pd.DataFrame
) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print(
        "strategy.py estructural cargado correctamente."
    )

    print(
        "API principal: analyze_market(df)"
    )


# ============================================================
# COMPATIBILIDAD CON EL bot.py LEGACY
# ============================================================

# El bot.py legacy invierte la dirección antes de ejecutar:
# call -> put / put -> call.
# Por eso pro_signal devuelve la dirección inversa
# de la señal real para conservar la compatibilidad.


def pro_signal(
    df: pd.DataFrame,
    aggressive: bool = False
):
    result = analyze_market(df)

    signal_value = result.get("signal")

    if signal_value not in ("call", "put"):
        return None, None, 0

    legacy_signal = (
        "put"
        if signal_value == "call"
        else "call"
    )

    return (
        legacy_signal,
        result.get("entry_type"),
        int(
            result.get("score_100", 0)
        ),
    )


def update_result(value: Any) -> None:
    """Compatibilidad con bot.py; los resultados no modifican la estrategia."""
    return None
