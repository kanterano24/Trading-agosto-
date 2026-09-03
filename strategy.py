"""
strategy.py

Estrategia estructural de RECHAZO para DIGITAL OTC 1M.

Objetivo:
- Evitar entradas de continuidad tardías cerca del último máximo/mínimo.
- Identificar dinámicamente el último máximo y último mínimo confirmados.
- Buscar CALL en rechazo de soporte / último mínimo.
- Buscar PUT en rechazo de resistencia / último máximo.
- Confirmar estructura HH/HL o LH/LL antes de permitir la entrada.
- Mantener una API compatible con bot.py: analyze_market(df), get_signal(), signal().

IMPORTANTE:
El módulo NO ejecuta operaciones. Si bot.py trabaja con N como vela viva y
N+1 como vela de entrada, este módulo solo genera la señal; la ejecución sigue
siendo responsabilidad de bot.py.
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

# Pivotes confirmados. 2/2 evita usar un extremo que todavía no está confirmado.
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
SWING_LOOKBACK = 35

# Zona dinámica basada en ATR.
ZONE_ATR = 0.28
MAX_ENTRY_DISTANCE_ATR = 0.55
MIN_ROOM_TO_OPPOSITE_ATR = 0.70

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

    value = history["tr"].tail(ATR_PERIOD).mean() if "tr" in history else np.nan
    if pd.isna(value) or value <= 0:
        value = (history["high"] - history["low"]).tail(ATR_PERIOD).mean()
    if pd.isna(value) or value <= 0:
        value = abs(float(history["close"].iloc[-1])) * 0.0001
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
) -> Tuple[list[Tuple[int, float]], list[Tuple[int, float]]]:
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

        if h >= float(left_highs.max()) and h >= float(right_highs.max()):
            highs.append((i, h))

        if l <= float(left_lows.min()) and l <= float(right_lows.min()):
            lows.append((i, l))

    return highs, lows


def _last_swing_levels(history: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _confirmed_swings(history)

    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None

    # Fallback: si todavía no hay pivote confirmado, usa extremos recientes.
    w = history.tail(min(SWING_LOOKBACK, len(history)))
    if last_high is None and not w.empty:
        idx = int(w["high"].idxmax())
        last_high = (idx, float(w.loc[idx, "high"]))
    if last_low is None and not w.empty:
        idx = int(w["low"].idxmin())
        last_low = (idx, float(w.loc[idx, "low"]))

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
        h1 = highs[-2][1]
        h2 = highs[-1][1]
        l1 = lows[-2][1]
        l2 = lows[-1][1]

        high_gap = abs(h2 - h1)
        low_gap = abs(l2 - l1)
        atr = _atr(add_indicators(work))
        min_gap = max(atr * MIN_STRUCTURE_GAP_ATR, EPS)

        if h2 > h1 + min_gap and l2 > l1 + min_gap:
            return "bullish"
        if h2 < h1 - min_gap and l2 < l1 - min_gap:
            return "bearish"

    # Fallback suave con EMA, solo cuando los pivotes son insuficientes.
    ind = add_indicators(work)
    last = ind.iloc[-1]
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
    score = 0

    highs = swings["highs"]
    lows = swings["lows"]

    if len(highs) >= 2:
        score += 1 if highs[-1][1] != highs[-2][1] else 0
    if len(lows) >= 2:
        score += 1 if lows[-1][1] != lows[-2][1] else 0

    structure = detect_structure(work)
    if structure in ("bullish", "bearish"):
        score += 2

    ind = add_indicators(work)
    if len(ind) >= 3:
        if structure == "bullish" and ind["ema9"].iloc[-1] > ind["ema21"].iloc[-1]:
            score += 1
        elif structure == "bearish" and ind["ema9"].iloc[-1] < ind["ema21"].iloc[-1]:
            score += 1

    return min(score, 5)


# ============================================================
# ZONAS DINÁMICAS
# ============================================================

def recent_levels(df: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> Tuple[float, float]:
    work = _normalize(df)
    if work.empty:
        return 0.0, 0.0
    x = work.tail(lookback)
    return float(x["low"].min()), float(x["high"].max())


def _zone_test(
    candle: Dict[str, float],
    level: float,
    atr: float,
    side: str,
) -> Tuple[bool, float, str]:
    """
    Detecta si la vela realmente TESTEÓ el nivel y rechazó.
    side='support' para CALL, side='resistance' para PUT.
    """
    zone = max(atr * ZONE_ATR, EPS)

    if side == "support":
        touched = candle["low"] <= level + zone
        closed_above = candle["close"] > level
        wick_ok = (
            candle["lower"] / candle["range"] >= MIN_REJECTION_WICK_RATIO
            or candle["lower"] >= candle["body"] * MIN_WICK_BODY_RATIO
        )
        close_ok = candle["close_position"] >= MIN_CLOSE_POSITION_CALL
        valid = touched and closed_above and wick_ok and close_ok

        distance = abs(candle["close"] - level) / atr
        return valid, distance, "rechazo de soporte"

    touched = candle["high"] >= level - zone
    closed_below = candle["close"] < level
    wick_ok = (
        candle["upper"] / candle["range"] >= MIN_REJECTION_WICK_RATIO
        or candle["upper"] >= candle["body"] * MIN_WICK_BODY_RATIO
    )
    close_ok = candle["close_position"] <= MAX_CLOSE_POSITION_PUT
    valid = touched and closed_below and wick_ok and close_ok

    distance = abs(candle["close"] - level) / atr
    return valid, distance, "rechazo de resistencia"


def is_near_sr(df: pd.DataFrame, tolerance: float = 0.0) -> bool:
    """Compatibilidad: True si el último cierre está cerca de un extremo."""
    work = _normalize(df)
    if len(work) < 5:
        return True

    atr = _atr(add_indicators(work))
    tol = tolerance if tolerance > 0 else atr * ZONE_ATR
    low, high = recent_levels(work)
    price = float(work["close"].iloc[-1])
    return abs(price - low) <= tol or abs(high - price) <= tol


# ============================================================
# FILTROS DE ENTRADA
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
    e9 = _safe_float(last.get("ema9"))
    e21 = _safe_float(last.get("ema21"))
    e50 = _safe_float(last.get("ema50"))
    close = _safe_float(last.get("close"))

    if direction == "bullish":
        return e9 >= e21 and e21 >= e50 and close >= e21
    return e9 <= e21 and e21 <= e50 and close <= e21


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
        # CALL no se permite pegado al último máximo.
        return (last_high - price) >= atr * MIN_ROOM_TO_OPPOSITE_ATR
    # PUT no se permite pegado al último mínimo.
    return (price - last_low) >= atr * MIN_ROOM_TO_OPPOSITE_ATR


def _body_is_valid(candle: Dict[str, float], atr: float) -> bool:
    if atr <= 0:
        return False
    body_atr = candle["body"] / atr
    return (
        candle["body_ratio"] >= MIN_BODY_RATIO
        and MIN_BODY_ATR <= body_atr <= MAX_BODY_ATR
    )


# ============================================================
# API PRINCIPAL
# ============================================================

def analyze_market(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analiza la última vela del dataframe como vela viva/confirmación.

    La nueva lógica es:

      CALL = estructura alcista + test/rechazo del último mínimo/soporte
             + espacio hasta el último máximo.

      PUT  = estructura bajista + test/rechazo del último máximo/resistencia
             + espacio hasta el último mínimo.

    Esto evita el patrón de las pérdidas observadas en las capturas:
    comprar después de una subida cuando el precio ya está en el máximo, o
    vender después de una caída cuando el precio ya está en el mínimo.
    """
    result = _empty_result()

    clean = _normalize(df)
    if len(clean) < MIN_BARS:
        result["reason"] = f"Historial insuficiente {len(clean)}/{MIN_BARS}"
        return result

    data = add_indicators(clean)
    if data.empty or len(data) < MIN_BARS:
        result["reason"] = "Indicadores insuficientes"
        return result

    # Se mantiene la convención usada por las versiones anteriores:
    # última fila = vela viva; las anteriores = historial cerrado.
    live = data.iloc[-1]
    previous = data.iloc[-2]
    history = data.iloc[:-1].copy()

    atr = _atr(history)
    rsi = _safe_float(live.get("rsi"), 50.0)
    structure = detect_structure(history)
    s_score = structure_score(history)
    swings = _last_swing_levels(history)

    last_high = swings["last_high"][1] if swings["last_high"] else None
    last_low = swings["last_low"][1] if swings["last_low"] else None

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
            int(live["from"]) if "from" in data.columns and not pd.isna(live["from"])
            else None
        ),
    })

    if structure not in ("bullish", "bearish"):
        result["reason"] = "Estructura lateral/ambigua"
        return result

    if atr <= 0 or not math.isfinite(atr):
        result["reason"] = "ATR inválido"
        return result

    if last_high is None or last_low is None:
        result["reason"] = "No hay máximo/mínimo estructural"
        return result

    c = candle_metrics(live)
    p = candle_metrics(previous)
    price = c["close"]

    # Evita entrar inmediatamente después de una vela gigantesca.
    if not _body_is_valid(c, atr):
        result["reason"] = "Vela de entrada demasiado pequeña/grande"
        return result

    # Evita perseguir el precio en el extremo opuesto.
    if not _not_overextended(price, last_high, last_low, atr, structure):
        result["reason"] = (
            "CALL bloqueado cerca del máximo" if structure == "bullish"
            else "PUT bloqueado cerca del mínimo"
        )
        result["zone"] = "extremo_opuesto"
        return result

    # ========================================================
    # CALL: rechazo del último mínimo / soporte
    # ========================================================
    if structure == "bullish":
        if not _ema_alignment(live, "bullish"):
            result["reason"] = "EMA no confirma estructura alcista"
            return result

        if not (CALL_RSI_MIN <= rsi <= CALL_RSI_MAX):
            result["reason"] = f"RSI CALL fuera de rango {rsi:.1f}"
            return result

        support_ok, distance, zone_reason = _zone_test(
            c, last_low, atr, "support"
        )

        if not support_ok:
            result["reason"] = "Esperando rechazo real del último mínimo"
            result["zone"] = "soporte"
            return result

        if distance > MAX_ENTRY_DISTANCE_ATR:
            result["reason"] = "Cierre demasiado alejado del soporte"
            return result

        if not _room_to_opposite(price, last_high, atr, "bullish"):
            result["reason"] = "Poco espacio hasta el último máximo"
            return result

        # Confirmación adicional: la vela de rechazo debe cerrar mejor que
        # la vela anterior, evitando comprar un rebote todavía débil.
        if price <= p["close"]:
            result["reason"] = "Rechazo sin recuperación suficiente"
            return result

        wick_strength = c["lower"] / max(c["range"], EPS)
        quality = int(round(
            55
            + min(20.0, wick_strength * 30.0)
            + min(15.0, s_score * 3.0)
            + (5.0 if c["close_position"] >= 0.72 else 0.0)
        ))
        quality = min(100, max(0, quality))

        if quality < 70:
            result["reason"] = f"Rechazo CALL débil ({quality}/100)"
            return result

        result.update({
            "signal": "call",
            "score": min(10, max(7, int(round(quality / 10.0)))),
            "reason": (
                "CALL | rechazo soporte/último mínimo | "
                f"nivel={last_low:.8f} | calidad={quality}/100"
            ),
            "continuity": True,
            "blocked": False,
            "zone": "soporte_rechazado",
            "entry_type": "rejection_support",
            "entry_quality": quality,
            "signal_price": price,
            "candle_open": c["open"],
            "candle_close": c["close"],
            "distance_to_zone_atr": distance,
        })
        return result

    # ========================================================
    # PUT: rechazo del último máximo / resistencia
    # ========================================================
    if not _ema_alignment(live, "bearish"):
        result["reason"] = "EMA no confirma estructura bajista"
        return result

    if not (PUT_RSI_MIN <= rsi <= PUT_RSI_MAX):
        result["reason"] = f"RSI PUT fuera de rango {rsi:.1f}"
        return result

    resistance_ok, distance, zone_reason = _zone_test(
        c, last_high, atr, "resistance"
    )

    if not resistance_ok:
        result["reason"] = "Esperando rechazo real del último máximo"
        result["zone"] = "resistencia"
        return result

    if distance > MAX_ENTRY_DISTANCE_ATR:
        result["reason"] = "Cierre demasiado alejado de la resistencia"
        return result

    if not _room_to_opposite(price, last_low, atr, "bearish"):
        result["reason"] = "Poco espacio hasta el último mínimo"
        return result

    if price >= p["close"]:
        result["reason"] = "Rechazo sin recuperación bajista suficiente"
        return result

    wick_strength = c["upper"] / max(c["range"], EPS)
    quality = int(round(
        55
        + min(20.0, wick_strength * 30.0)
        + min(15.0, s_score * 3.0)
        + (5.0 if c["close_position"] <= 0.28 else 0.0)
    ))
    quality = min(100, max(0, quality))

    if quality < 70:
        result["reason"] = f"Rechazo PUT débil ({quality}/100)"
        return result

    result.update({
        "signal": "put",
        "score": min(10, max(7, int(round(quality / 10.0)))),
        "reason": (
            "PUT | rechazo resistencia/último máximo | "
            f"nivel={last_high:.8f} | calidad={quality}/100"
        ),
        "continuity": True,
        "blocked": False,
        "zone": "resistencia_rechazada",
        "entry_type": "rejection_resistance",
        "entry_quality": quality,
        "signal_price": price,
        "candle_open": c["open"],
        "candle_close": c["close"],
        "distance_to_zone_atr": distance,
    })
    return result


# ============================================================
# COMPATIBILIDAD
# ============================================================

def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df).get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)


if __name__ == "__main__":
    print("strategy.py estructural cargado correctamente.")
    print("API principal: analyze_market(df)")
