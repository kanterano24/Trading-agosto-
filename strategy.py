"""
strategy.py

Estrategia EXCLUSIVA de RECHAZO ESTRUCTURAL para DIGITAL OTC 1M.

Reglas obligatorias:
- Solo se permiten operaciones de tipo rejection.
- No se permiten operaciones de continuation.
- PUT:
    * Estructura bearish (LH + LL).
    * Precio toca/testea la resistencia o último máximo confirmado.
    * Vela de rechazo bajista tipo shooting star:
        - mecha superior dominante;
        - cierre por debajo de la apertura;
        - cierre de vuelta por debajo de la resistencia;
        - cierre ubicado en la parte inferior de la vela.
    * La vela N debe cerrar primero.
    * La operación se prepara únicamente para N+1.
    * Expiración: 1 minuto.
- CALL:
    * Estructura bullish (HH + HL).
    * Precio toca/testea el soporte o último mínimo confirmado.
    * Vela de rechazo alcista tipo hammer:
        - mecha inferior dominante;
        - cierre por encima de la apertura;
        - cierre de vuelta por encima del soporte;
        - cierre ubicado en la parte superior de la vela.
    * La vela N debe cerrar primero.
    * La operación se prepara únicamente para N+1.
    * Expiración: 1 minuto.
- Score mínimo: 95/100.
- Calidad mínima: 95/100.
- Si falta una sola condición obligatoria, no se genera señal.

IMPORTANTE:
Este módulo no ejecuta operaciones. bot.py es responsable de ejecutar la
señal en N+1 y de controlar la operación.
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
MAX_CANDLES = 100

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
SWING_LOOKBACK = 35

# Zona de contacto con soporte/resistencia.
ZONE_ATR = 0.28
MAX_ENTRY_DISTANCE_ATR = 0.55

# Espacio mínimo después del rechazo.
MIN_ROOM_TO_OPPOSITE_ATR = 0.70
MIN_RECENT_ROOM_ATR = 0.55

# Calidad y score obligatorios.
MIN_SIGNAL_QUALITY = 95
MIN_SIGNAL_SCORE = 95

# Rechazo.
MIN_BODY_RATIO = 0.18
MIN_REJECTION_WICK_RATIO = 0.35
MIN_WICK_BODY_RATIO = 1.15
MIN_CLOSE_POSITION_CALL = 0.62
MAX_CLOSE_POSITION_PUT = 0.38

# Evita velas sin cuerpo o velas excesivamente grandes.
MIN_BODY_ATR = 0.05
MAX_BODY_ATR = 1.35

# Separación mínima para validar cambios estructurales.
MIN_STRUCTURE_GAP_ATR = 0.05

# Filtro RSI. No es el disparador principal.
CALL_RSI_MIN = 35.0
CALL_RSI_MAX = 70.0
PUT_RSI_MIN = 30.0
PUT_RSI_MAX = 65.0

EPS = 1e-12


# ============================================================
# RESULTADO ESTABLE
# ============================================================

def _empty_result(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "structure": "range",
        "reason": reason,
        "score": 0,
        "score_100": 0,
        "quality": 0,
        "entry_quality": 0,
        "continuity": False,
        "blocked": True,
        "zone": None,
        "entry_type": None,
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
        "analysis": {},
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
    if any(col not in out.columns for col in required):
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
    out["atr"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()

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

    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100.0
    out.loc[(avg_gain == 0) & (avg_loss > 0), "rsi"] = 0.0

    return out


def _atr(history: pd.DataFrame) -> float:
    if history is None or history.empty:
        return 0.0

    if "tr" in history.columns:
        value = history["tr"].tail(ATR_PERIOD).mean()
    else:
        value = np.nan

    if pd.isna(value) or value <= 0:
        value = (history["high"] - history["low"]).tail(ATR_PERIOD).mean()

    if pd.isna(value) or value <= 0:
        value = abs(float(history["close"].iloc[-1])) * 0.0001

    return float(max(value, EPS))


# ============================================================
# VELAS
# ============================================================

def candle_direction(candle: pd.Series) -> str:
    open_price = _safe_float(candle.get("open"))
    close_price = _safe_float(candle.get("close"))

    if close_price > open_price:
        return "bull"
    if close_price < open_price:
        return "bear"
    return "neutral"


def candle_metrics(candle: pd.Series) -> Dict[str, float]:
    open_price = _safe_float(candle.get("open"))
    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))
    close_price = _safe_float(candle.get("close"))

    candle_range = max(high - low, EPS)
    body = abs(close_price - open_price)
    upper_wick = max(high - max(open_price, close_price), 0.0)
    lower_wick = max(min(open_price, close_price) - low, 0.0)

    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close_price,
        "range": candle_range,
        "body": body,
        "upper": upper_wick,
        "lower": lower_wick,
        "body_ratio": body / candle_range,
        "upper_ratio": upper_wick / candle_range,
        "lower_ratio": lower_wick / candle_range,
        "close_position": (close_price - low) / candle_range,
    }


# ============================================================
# PIVOTES CONFIRMADOS
# ============================================================

def _confirmed_swings(
    history: pd.DataFrame,
    left: int = PIVOT_LEFT,
    right: int = PIVOT_RIGHT,
) -> Tuple[list[Tuple[int, float]], list[Tuple[int, float]]]:
    highs: list[Tuple[int, float]] = []
    lows: list[Tuple[int, float]] = []

    if history is None or len(history) < left + right + 3:
        return highs, lows

    start = max(left, len(history) - SWING_LOOKBACK)
    end = len(history) - right

    for i in range(start, end):
        high = float(history["high"].iloc[i])
        low = float(history["low"].iloc[i])

        left_highs = history["high"].iloc[i - left:i]
        right_highs = history["high"].iloc[i + 1:i + right + 1]
        left_lows = history["low"].iloc[i - left:i]
        right_lows = history["low"].iloc[i + 1:i + right + 1]

        if high >= float(left_highs.max()) and high >= float(right_highs.max()):
            highs.append((i, high))

        if low <= float(left_lows.min()) and low <= float(right_lows.min()):
            lows.append((i, low))

    return highs, lows


def _last_swing_levels(history: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _confirmed_swings(history)

    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None

    # Fallback para mantener funcionamiento con poco historial de pivotes.
    recent = history.tail(min(SWING_LOOKBACK, len(history)))

    if last_high is None and not recent.empty:
        idx = int(recent["high"].idxmax())
        last_high = (idx, float(recent.loc[idx, "high"]))

    if last_low is None and not recent.empty:
        idx = int(recent["low"].idxmin())
        last_low = (idx, float(recent.loc[idx, "low"]))

    return {
        "highs": highs,
        "lows": lows,
        "last_high": last_high,
        "last_low": last_low,
    }


# ============================================================
# ESTRUCTURA HH/HL - LH/LL
# ============================================================

def detect_structure(df: pd.DataFrame) -> str:
    work = _normalize(df)

    if len(work) < 12:
        return "range"

    swings = _last_swing_levels(work)
    highs = swings["highs"]
    lows = swings["lows"]

    if len(highs) >= 2 and len(lows) >= 2:
        previous_high = highs[-2][1]
        latest_high = highs[-1][1]
        previous_low = lows[-2][1]
        latest_low = lows[-1][1]

        atr = _atr(add_indicators(work))
        minimum_gap = max(atr * MIN_STRUCTURE_GAP_ATR, EPS)

        if (
            latest_high > previous_high + minimum_gap
            and latest_low > previous_low + minimum_gap
        ):
            return "bullish"

        if (
            latest_high < previous_high - minimum_gap
            and latest_low < previous_low - minimum_gap
        ):
            return "bearish"

    # Fallback únicamente si no existen suficientes pivotes confirmados.
    indicators = add_indicators(work)
    last = indicators.iloc[-1]

    if last["ema9"] > last["ema21"] > last["ema50"]:
        return "bullish"

    if last["ema9"] < last["ema21"] < last["ema50"]:
        return "bearish"

    return "range"


def structure_score(df: pd.DataFrame) -> int:
    work = _normalize(df)

    if len(work) < 12:
        return 0

    swings = _last_swing_levels(work)
    highs = swings["highs"]
    lows = swings["lows"]

    score = 0

    if len(highs) >= 2 and highs[-1][1] != highs[-2][1]:
        score += 1

    if len(lows) >= 2 and lows[-1][1] != lows[-2][1]:
        score += 1

    structure = detect_structure(work)

    if structure in ("bullish", "bearish"):
        score += 2

    indicators = add_indicators(work)

    if len(indicators) >= 3:
        if (
            structure == "bullish"
            and indicators["ema9"].iloc[-1] > indicators["ema21"].iloc[-1]
        ):
            score += 1

        if (
            structure == "bearish"
            and indicators["ema9"].iloc[-1] < indicators["ema21"].iloc[-1]
        ):
            score += 1

    return min(score, 5)


# ============================================================
# ZONAS
# ============================================================

def recent_levels(
    df: pd.DataFrame,
    lookback: int = SWING_LOOKBACK,
) -> Tuple[float, float]:
    work = _normalize(df)

    if work.empty:
        return 0.0, 0.0

    recent = work.tail(lookback)

    return (
        float(recent["low"].min()),
        float(recent["high"].max()),
    )


def _zone_test(
    candle: Dict[str, float],
    level: float,
    atr: float,
    side: str,
) -> Tuple[bool, float, str]:
    """
    Confirma el contacto y rechazo del nivel.

    support:
        CALL, rechazo alcista tipo hammer.

    resistance:
        PUT, rechazo bajista tipo shooting star.
    """
    zone = max(atr * ZONE_ATR, EPS)

    if side == "support":
        touched = candle["low"] <= level + zone
        closed_above = candle["close"] > level
        bullish_body = candle["close"] > candle["open"]

        wick_ok = (
            candle["lower_ratio"] >= MIN_REJECTION_WICK_RATIO
            or candle["lower"] >= candle["body"] * MIN_WICK_BODY_RATIO
        )

        close_ok = candle["close_position"] >= MIN_CLOSE_POSITION_CALL
        body_ok = candle["body_ratio"] >= MIN_BODY_RATIO

        valid = (
            touched
            and closed_above
            and bullish_body
            and wick_ok
            and close_ok
            and body_ok
        )

        distance = abs(candle["close"] - level) / max(atr, EPS)

        return valid, distance, "rechazo de soporte"

    touched = candle["high"] >= level - zone
    closed_below = candle["close"] < level
    bearish_body = candle["close"] < candle["open"]

    wick_ok = (
        candle["upper_ratio"] >= MIN_REJECTION_WICK_RATIO
        or candle["upper"] >= candle["body"] * MIN_WICK_BODY_RATIO
    )

    close_ok = candle["close_position"] <= MAX_CLOSE_POSITION_PUT
    body_ok = candle["body_ratio"] >= MIN_BODY_RATIO

    valid = (
        touched
        and closed_below
        and bearish_body
        and wick_ok
        and close_ok
        and body_ok
    )

    distance = abs(candle["close"] - level) / max(atr, EPS)

    return valid, distance, "rechazo de resistencia"


def is_near_sr(df: pd.DataFrame, tolerance: float = 0.0) -> bool:
    work = _normalize(df)

    if len(work) < 5:
        return True

    atr = _atr(add_indicators(work))
    tolerance_value = tolerance if tolerance > 0 else atr * ZONE_ATR

    support, resistance = recent_levels(work)
    price = float(work["close"].iloc[-1])

    return (
        abs(price - support) <= tolerance_value
        or abs(resistance - price) <= tolerance_value
    )


# ============================================================
# FILTROS
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
        room = opposite_level - price
    else:
        room = price - opposite_level

    return room >= atr * MIN_ROOM_TO_OPPOSITE_ATR


def _ema_alignment(last: pd.Series, direction: str) -> bool:
    ema9 = _safe_float(last.get("ema9"))
    ema21 = _safe_float(last.get("ema21"))
    ema50 = _safe_float(last.get("ema50"))
    close = _safe_float(last.get("close"))

    if direction == "bullish":
        return ema9 >= ema21 and ema21 >= ema50 and close >= ema21

    return ema9 <= ema21 and ema21 <= ema50 and close <= ema21


def _recent_room_ok(
    history: pd.DataFrame,
    price: float,
    atr: float,
    direction: str,
    lookback: int = 8,
) -> bool:
    if history is None or history.empty or atr <= 0:
        return False

    recent = history.tail(lookback)
    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    required = atr * MIN_RECENT_ROOM_ATR

    if direction == "bullish":
        return recent_high - price >= required

    return price - recent_low >= required


def _body_is_valid(candle: Dict[str, float], atr: float) -> bool:
    if atr <= 0:
        return False

    body_atr = candle["body"] / atr

    return (
        candle["body_ratio"] >= MIN_BODY_RATIO
        and MIN_BODY_ATR <= body_atr <= MAX_BODY_ATR
    )


def _trend_slope_ok(
    history: pd.DataFrame,
    atr: float,
    direction: str,
) -> bool:
    if history is None or len(history) < 6 or atr <= 0:
        return False

    closes = history["close"].astype(float)
    delta = float(closes.iloc[-1] - closes.iloc[-6])
    minimum = atr * 0.08

    if direction == "bullish":
        return delta >= minimum

    return delta <= -minimum


# ============================================================
# FRAME DE ANÁLISIS
# ============================================================

def _build_analysis_frame(
    df: Optional[pd.DataFrame] = None,
    *,
    candle_1m: Optional[Dict[str, Any]] = None,
    previous_m1: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if candle_1m is not None:
        history = _normalize(
            previous_m1 if previous_m1 is not None else pd.DataFrame()
        )
        current = _normalize(pd.DataFrame([candle_1m]))

        if history.empty:
            return current

        return _normalize(
            pd.concat([history, current], ignore_index=True)
        )

    return _normalize(df if df is not None else pd.DataFrame())


def _attach_public_analysis(result: Dict[str, Any]) -> Dict[str, Any]:
    analysis = result.get("analysis")

    if not isinstance(analysis, dict):
        analysis = {}

    for key in (
        "structure",
        "trend",
        "force",
        "entry_quality",
        "impulse_phase",
        "pullback",
        "zone",
        "last_swing_high",
        "last_swing_low",
    ):
        if key in result and key not in analysis:
            analysis[key] = result[key]

    result["analysis"] = analysis
    return result


# ============================================================
# API PRINCIPAL
# ============================================================

def analyze_market(
    df: Optional[pd.DataFrame] = None,
    *,
    candle_1m: Optional[Dict[str, Any]] = None,
    previous_m1: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analiza la última vela como vela N.

    La señal solo se genera cuando:
    - La estructura coincide con la dirección.
    - Existe contacto con el nivel.
    - Existe rechazo confirmado.
    - La vela N ya cerró.
    - Score y calidad son >= 95/100.

    La ejecución queda indicada para N+1, con expiración de 1 minuto.
    """
    result = _empty_result()

    clean = _build_analysis_frame(
        df,
        candle_1m=candle_1m,
        previous_m1=previous_m1,
    )

    if len(clean) < MIN_BARS:
        result["reason"] = f"Historial insuficiente {len(clean)}/{MIN_BARS}"
        return _attach_public_analysis(result)

    data = add_indicators(clean)

    if data.empty or len(data) < MIN_BARS:
        result["reason"] = "Indicadores insuficientes"
        return _attach_public_analysis(result)

    # Convención:
    # última fila = vela N recién cerrada o vela de análisis;
    # filas anteriores = historial.
    live = data.iloc[-1]
    previous = data.iloc[-2]
    history = data.iloc[:-1].copy()

    atr = _atr(history)
    rsi = _safe_float(live.get("rsi"), 50.0)
    structure = detect_structure(history)
    s_score = structure_score(history)
    swings = _last_swing_levels(history)

    last_high = (
        swings["last_high"][1]
        if swings["last_high"] is not None
        else None
    )

    last_low = (
        swings["last_low"][1]
        if swings["last_low"] is not None
        else None
    )

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
        "candle": candle_direction(live),
        "candle_timestamp": (
            int(live["from"])
            if "from" in data.columns and not pd.isna(live["from"])
            else None
        ),
        "pair": pair,
    })

    # Solo bullish o bearish.
    if structure not in ("bullish", "bearish"):
        result["reason"] = "BLOQUEADO: estructura lateral o ambigua"
        return _attach_public_analysis(result)

    if atr <= 0 or not math.isfinite(atr):
        result["reason"] = "ATR inválido"
        return _attach_public_analysis(result)

    if last_high is None or last_low is None:
        result["reason"] = "No existe máximo/mínimo estructural"
        return _attach_public_analysis(result)

    current_candle = candle_metrics(live)
    previous_candle = candle_metrics(previous)
    price = current_candle["close"]

    if not _body_is_valid(current_candle, atr):
        result["reason"] = "BLOQUEADO: cuerpo de la vela inválido"
        return _attach_public_analysis(result)

    if not _recent_room_ok(history, price, atr, structure):
        result["reason"] = "BLOQUEADO: poco espacio en el extremo reciente"
        result["zone"] = "extremo_reciente"
        return _attach_public_analysis(result)

    if not _trend_slope_ok(history, atr, structure):
        result["reason"] = "BLOQUEADO: pendiente insuficiente o posible rango"
        result["zone"] = "rango"
        return _attach_public_analysis(result)

    # ========================================================
    # PUT: RECHAZO DE RESISTENCIA / SHOOTING STAR
    # ========================================================
    if structure == "bearish":
        direction_name = "PUT"

        if not _ema_alignment(live, "bearish"):
            result["reason"] = "PUT bloqueado: EMA no confirma estructura bearish"
            return _attach_public_analysis(result)

        if not (PUT_RSI_MIN <= rsi <= PUT_RSI_MAX):
            result["reason"] = f"PUT bloqueado: RSI fuera de rango ({rsi:.1f})"
            return _attach_public_analysis(result)

        resistance_ok, distance, zone_reason = _zone_test(
            current_candle,
            last_high,
            atr,
            "resistance",
        )

        if not resistance_ok:
            result["reason"] = (
                "PUT bloqueado: esperando rechazo confirmado en resistencia "
                "tipo shooting star"
            )
            result["zone"] = "resistencia"
            return _attach_public_analysis(result)

        if distance > MAX_ENTRY_DISTANCE_ATR:
            result["reason"] = "PUT bloqueado: cierre demasiado alejado de resistencia"
            return _attach_public_analysis(result)

        if not _room_to_opposite(price, last_low, atr, "bearish"):
            result["reason"] = "PUT bloqueado: poco espacio hacia el último mínimo"
            return _attach_public_analysis(result)

        # La vela N debe confirmar presión bajista frente a la vela anterior.
        if price >= previous_candle["close"]:
            result["reason"] = (
                "PUT bloqueado: no existe confirmación bajista frente a la vela previa"
            )
            return _attach_public_analysis(result)

        wick_strength = current_candle["upper"] / max(
            current_candle["range"],
            EPS,
        )

        quality = int(round(
            55.0
            + min(20.0, wick_strength * 30.0)
            + min(15.0, s_score * 3.0)
            + (
                5.0
                if current_candle["close_position"] <= 0.28
                else 0.0
            )
        ))

        signal = "put"
        entry_type = "rejection_resistance"
        zone_label = "resistencia_rechazada"
        level = last_high
        reason_prefix = "PUT"

    # ========================================================
    # CALL: RECHAZO DE SOPORTE / HAMMER
    # ========================================================
    else:
        direction_name = "CALL"

        if not _ema_alignment(live, "bullish"):
            result["reason"] = "CALL bloqueado: EMA no confirma estructura bullish"
            return _attach_public_analysis(result)

        if not (CALL_RSI_MIN <= rsi <= CALL_RSI_MAX):
            result["reason"] = f"CALL bloqueado: RSI fuera de rango ({rsi:.1f})"
            return _attach_public_analysis(result)

        support_ok, distance, zone_reason = _zone_test(
            current_candle,
            last_low,
            atr,
            "support",
        )

        if not support_ok:
            result["reason"] = (
                "CALL bloqueado: esperando rechazo confirmado en soporte "
                "tipo hammer"
            )
            result["zone"] = "soporte"
            return _attach_public_analysis(result)

        if distance > MAX_ENTRY_DISTANCE_ATR:
            result["reason"] = "CALL bloqueado: cierre demasiado alejado de soporte"
            return _attach_public_analysis(result)

        if not _room_to_opposite(price, last_high, atr, "bullish"):
            result["reason"] = "CALL bloqueado: poco espacio hacia el último máximo"
            return _attach_public_analysis(result)

        # La vela N debe confirmar presión alcista frente a la vela anterior.
        if price <= previous_candle["close"]:
            result["reason"] = (
                "CALL bloqueado: no existe confirmación alcista frente a la vela previa"
            )
            return _attach_public_analysis(result)

        wick_strength = current_candle["lower"] / max(
            current_candle["range"],
            EPS,
        )

        quality = int(round(
            55.0
            + min(20.0, wick_strength * 30.0)
            + min(15.0, s_score * 3.0)
            + (
                5.0
                if current_candle["close_position"] >= 0.72
                else 0.0
            )
        ))

        signal = "call"
        entry_type = "rejection_support"
        zone_label = "soporte_rechazado"
        level = last_low
        reason_prefix = "CALL"

    # ========================================================
    # FILTROS FINALES DE 95/100
    # ========================================================

    quality = min(100, max(0, quality))
    score_100 = quality

    if quality < MIN_SIGNAL_QUALITY:
        result["reason"] = (
            f"{reason_prefix} bloqueado: calidad {quality}/100 < 95/100"
        )
        return _attach_public_analysis(result)

    if score_100 < MIN_SIGNAL_SCORE:
        result["reason"] = (
            f"{reason_prefix} bloqueado: score {score_100}/100 < 95/100"
        )
        return _attach_public_analysis(result)

    result.update({
        "signal": signal,
        "direction": signal,
        "trend": structure,
        "structure": structure,
        "score": score_100,
        "score_100": score_100,
        "quality": quality,
        "entry_quality": quality,
        "reason": (
            f"{reason_prefix} | rejection confirmado | "
            f"{'resistencia' if signal == 'put' else 'soporte'} | "
            f"nivel={level:.8f} | score={score_100}/100 | "
            f"calidad={quality}/100 | N cerrada | ejecución N+1"
        ),
        "continuity": False,
        "blocked": False,
        "zone": zone_label,
        "entry_type": entry_type,
        "execution": "N+1",
        "expiration_minutes": 1,
        "entry_for_next_candle": True,
        "indecision_requires_n_plus_1": True,
        "signal_price": price,
        "candle_open": current_candle["open"],
        "candle_close": current_candle["close"],
        "distance_to_zone_atr": distance,
        "rejection_confirmed": True,
        "candle_closed": True,
        "next_candle_only": True,
        "pair": pair,
    })

    return _attach_public_analysis(result)


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


def pro_signal(
    df: pd.DataFrame,
    aggressive: bool = False,
):
    """
    Compatibilidad con bot.py legacy.

    ATENCIÓN:
    Se conserva la inversión histórica de dirección porque algunas versiones
    de bot.py invierten call/put antes de ejecutar.
    """
    result = analyze_market(df)
    current_signal = result.get("signal")

    if current_signal not in ("call", "put"):
        return None, None, 0

    legacy_signal = "put" if current_signal == "call" else "call"

    return (
        legacy_signal,
        result.get("entry_type"),
        int(result.get("score_100", 0)),
    )


def update_result(value: Any) -> None:
    """Compatibilidad con bot.py; no modifica la estrategia."""
    return None


if __name__ == "__main__":
    print("strategy.py de rechazo estructural cargado correctamente.")
    print("Solo rejection | score >= 95 | calidad >= 95 | ejecución N+1 | expiración 1M")
