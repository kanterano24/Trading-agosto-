"""
strategy.py
Estrategia de continuidad para velas de 1 minuto.

Regla principal:
- Se analiza la vela 1M que está viva.
- La señal es válida solamente si la vela viva mantiene continuidad
  a favor de la tendencia.
- El bot que usa este módulo NO ejecuta esta misma vela: guarda la señal
  y, al aparecer un nuevo timestamp de vela, ejecuta sobre la nueva vela.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import math
import pandas as pd


MAX_CANDLES = 90
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14

STRUCTURE_LOOKBACK = 8
SR_LOOKBACK = 20  # Compatibilidad; el rango ya no depende de un número fijo.
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
MIN_SWING_ATR = 0.15

# Multiplicadores deliberadamente conservadores: si el precio está cerca
# de una zona importante, la operación se bloquea.
SR_ATR_DISTANCE = 0.35

# Filtro obligatorio de ubicación: solo permite operar en los extremos
# del rango estructural reciente. La zona central queda bloqueada.
EXTREME_LOW_PERCENT = 0.20
EXTREME_HIGH_PERCENT = 0.80

REJECTION_WICK_RATIO = 0.50
MIN_BODY_ATR = 0.18
MAX_COUNTER_WICK_ATR = 0.55

# Evita entrar cuando la tendencia ya está demasiado extendida.
END_TREND_DISTANCE_ATR = 0.60

EPS = 1e-12


def _empty_result(reason: str = "Sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "reason": reason,
        "score": 0,
        "trend": "range",
        "continuity": False,
        "blocked": True,
        "zone": None,
    }


def _validate_df(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        return None

    work = df.copy()

    for col in required:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=list(required))

    if "from" in work.columns:
        work["from"] = pd.to_numeric(work["from"], errors="coerce")
        work = work.dropna(subset=["from"])
        work = work.sort_values("from")

    work = work.reset_index(drop=True)

    if len(work) > MAX_CANDLES:
        work = work.tail(MAX_CANDLES).reset_index(drop=True)

    return work


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    value = tr.tail(period).mean()
    if pd.isna(value) or value <= 0:
        return float(max(df["high"].iloc[-1] - df["low"].iloc[-1], EPS))
    return float(value)


def _add_emas(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["ema_fast"] = work["close"].ewm(
        span=EMA_FAST, adjust=False
    ).mean()
    work["ema_slow"] = work["close"].ewm(
        span=EMA_SLOW, adjust=False
    ).mean()
    return work


def _structure(df: pd.DataFrame) -> str:
    """Estructura de máximos/mínimos recientes."""
    if len(df) < STRUCTURE_LOOKBACK + 1:
        return "range"

    w = df.tail(STRUCTURE_LOOKBACK + 1)

    highs = w["high"].tolist()
    lows = w["low"].tolist()

    hh = hl = lh = ll = 0

    for i in range(1, len(w)):
        if highs[i] > highs[i - 1]:
            hh += 1
        elif highs[i] < highs[i - 1]:
            lh += 1

        if lows[i] > lows[i - 1]:
            hl += 1
        elif lows[i] < lows[i - 1]:
            ll += 1

    bullish_points = hh + hl
    bearish_points = lh + ll

    if bullish_points >= 10 and bullish_points >= bearish_points + 3:
        return "bullish"

    if bearish_points >= 10 and bearish_points >= bullish_points + 3:
        return "bearish"

    return "range"


def _trend(df: pd.DataFrame) -> str:
    if len(df) < EMA_SLOW + 5:
        return "range"

    work = _add_emas(df)

    fast = float(work["ema_fast"].iloc[-1])
    slow = float(work["ema_slow"].iloc[-1])

    look = min(4, len(work) - 1)
    fast_prev = float(work["ema_fast"].iloc[-1 - look])
    slow_prev = float(work["ema_slow"].iloc[-1 - look])

    structure = _structure(work)

    bullish = (
        fast > slow
        and fast >= fast_prev
        and slow >= slow_prev
        and structure == "bullish"
    )

    bearish = (
        fast < slow
        and fast <= fast_prev
        and slow <= slow_prev
        and structure == "bearish"
    )

    if bullish:
        return "bullish"
    if bearish:
        return "bearish"
    return "range"


def _candle_metrics(candle: pd.Series) -> Dict[str, float]:
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])

    body = abs(c - o)
    rng = max(h - l, EPS)
    upper = max(h - max(o, c), 0.0)
    lower = max(min(o, c) - l, 0.0)

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "body": body,
        "range": rng,
        "upper": upper,
        "lower": lower,
    }


def _confirmed_swings(history: pd.DataFrame) -> tuple[list[float], list[float]]:
    """Detecta swings confirmados usando vecinos a izquierda y derecha.

    Los pivots se calculan solamente sobre velas cerradas. El rango resultante
    se adapta a la estructura observada de cada par, sin usar tail(20).
    """
    highs: list[float] = []
    lows: list[float] = []
    n = len(history)

    if n < PIVOT_LEFT + PIVOT_RIGHT + 1:
        return highs, lows

    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        h = float(history["high"].iloc[i])
        l = float(history["low"].iloc[i])
        left_highs = history["high"].iloc[i - PIVOT_LEFT:i]
        right_highs = history["high"].iloc[i + 1:i + 1 + PIVOT_RIGHT]
        left_lows = history["low"].iloc[i - PIVOT_LEFT:i]
        right_lows = history["low"].iloc[i + 1:i + 1 + PIVOT_RIGHT]

        if h >= float(left_highs.max()) and h >= float(right_highs.max()):
            highs.append(h)
        if l <= float(left_lows.min()) and l <= float(right_lows.min()):
            lows.append(l)

    return highs, lows


def _dynamic_structural_range(
    history: pd.DataFrame,
    atr: float,
) -> tuple[Optional[float], Optional[float], str]:
    """Obtiene el rango activo desde swings confirmados del par.

    Se usan el último máximo y mínimo estructural disponibles. Si todavía no
    existen ambos, no se inventa un rango: la señal queda bloqueada.
    """
    swing_highs, swing_lows = _confirmed_swings(history)

    if not swing_highs or not swing_lows:
        return None, None, "sin_swings_confirmados"

    high = max(swing_highs[-3:])
    low = min(swing_lows[-3:])

    if high - low < max(atr * MIN_SWING_ATR, EPS):
        return high, low, "rango_estructural_pequeno"

    return high, low, "rango_estructural_dinamico"


def _extreme_location(
    history: pd.DataFrame,
    price: float,
    direction: str,
    atr: float,
) -> tuple[bool, Optional[str], Optional[float], Optional[float]]:
    """Filtra la ubicación usando el rango estructural dinámico del par."""
    recent_high, recent_low, range_status = _dynamic_structural_range(history, atr)

    if recent_high is None or recent_low is None:
        return False, range_status, recent_high, recent_low

    range_size = recent_high - recent_low
    if range_size <= EPS:
        return False, "rango_invalido", recent_high, recent_low

    position = (price - recent_low) / range_size

    if direction == "bullish" and position <= EXTREME_LOW_PERCENT:
        return True, "extremo_inferior", recent_high, recent_low

    if direction == "bearish" and position >= EXTREME_HIGH_PERCENT:
        return True, "extremo_superior", recent_high, recent_low

    if position <= EXTREME_LOW_PERCENT:
        return False, "extremo_inferior_direccion_incompatible", recent_high, recent_low
    if position >= EXTREME_HIGH_PERCENT:
        return False, "extremo_superior_direccion_incompatible", recent_high, recent_low

    return False, "zona_central", recent_high, recent_low


def _rejection(c: Dict[str, float], direction: str) -> bool:
    body = c["body"]
    rng = c["range"]

    # Mecha dominante = rechazo.
    if direction == "bullish":
        if c["upper"] / rng >= REJECTION_WICK_RATIO:
            return True
        if c["lower"] > body * 2.8 and c["lower"] / rng > 0.45:
            return True
    else:
        if c["lower"] / rng >= REJECTION_WICK_RATIO:
            return True
        if c["upper"] > body * 2.8 and c["upper"] / rng > 0.45:
            return True

    return False


def _weakness(
    live: Dict[str, float],
    previous: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    if live["body"] < atr * MIN_BODY_ATR:
        return True

    if direction == "bullish":
        if live["close"] <= previous["close"]:
            return True
        if live["upper"] > atr * MAX_COUNTER_WICK_ATR:
            return True
    else:
        if live["close"] >= previous["close"]:
            return True
        if live["lower"] > atr * MAX_COUNTER_WICK_ATR:
            return True

    return False


def _pullback(
    live: Dict[str, float],
    previous: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    """
    No se acepta una vela viva que se comporte principalmente como
    retroceso contra la dirección de la estructura.
    """
    if direction == "bullish":
        # Apertura/cuerpo demasiado por debajo del cierre anterior.
        if live["open"] < previous["close"] - 0.20 * atr:
            return True
        # Una vela que cierra por debajo de su apertura no es continuidad.
        if live["close"] <= live["open"]:
            return True
    else:
        if live["open"] > previous["close"] + 0.20 * atr:
            return True
        if live["close"] >= live["open"]:
            return True

    return False


def _end_of_trend(
    history: pd.DataFrame,
    live: Dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    """
    Bloquea si el precio vivo ya está demasiado cerca del extremo
    de la estructura reciente. Esto evita vender en máximos o comprar
    en mínimos, además del bloqueo general de S/R.
    """
    recent_high, recent_low, _ = _dynamic_structural_range(history, atr)
    if recent_high is None or recent_low is None:
        return True
    price = live["close"]

    if direction == "bullish":
        if recent_high - price <= atr * END_TREND_DISTANCE_ATR:
            return True
    else:
        if price - recent_low <= atr * END_TREND_DISTANCE_ATR:
            return True

    return False


def analyze_market(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analiza como máximo las últimas 60 velas 1M.

    IMPORTANTE:
    - df.iloc[-1] se considera la vela viva.
    - La estructura y los niveles se calculan principalmente con las
      velas anteriores.
    - Esta función NO ejecuta operaciones.
    """
    work = _validate_df(df)

    if work is None or len(work) < max(EMA_SLOW + 5, 30):
        return _empty_result("Historial insuficiente")

    work = _add_emas(work)

    live = work.iloc[-1]
    previous = work.iloc[-2]
    history = work.iloc[:-1]

    if len(history) < 25:
        return _empty_result("Historial cerrado insuficiente")

    atr = _atr(history)

    direction = _trend(history)

    result: Dict[str, Any] = {
        "signal": None,
        "direction": direction,
        "trend": direction,
        "reason": "",
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": None,
        "atr": atr,
        "entry_type": None,
        "entry_quality": 0,
        "analysis": {
            "force": False,
            "structure": direction,
            "atr": atr,
            "last_swing_high": None,
            "last_swing_low": None,
            "entry_quality": 0,
        },
        "candle_timestamp": (
            int(live["from"]) if "from" in work.columns and not pd.isna(live["from"])
            else None
        ),
    }

    if direction not in ("bullish", "bearish"):
        result["reason"] = "No existe tendencia clara"
        return result

    c_live = _candle_metrics(live)
    c_prev = _candle_metrics(previous)

    # Ubicación obligatoria: únicamente extremos estructurales.
    # La zona central queda bloqueada antes de evaluar continuidad.
    at_extreme, zone, recent_high, recent_low = _extreme_location(
        history,
        c_live["close"],
        direction,
        atr,
    )

    if not at_extreme:
        result["reason"] = f"Ubicación bloqueada: {zone}"
        result["zone"] = zone
        result["recent_high"] = recent_high
        result["recent_low"] = recent_low
        return result

    result["zone"] = zone
    result["recent_high"] = recent_high
    result["recent_low"] = recent_low

    # Rechazo.
    if _rejection(c_live, direction):
        result["reason"] = "Rechazo detectado"
        return result

    # Pullback.
    if _pullback(c_live, c_prev, direction, atr):
        result["reason"] = "Pullback detectado"
        return result

    # Debilidad.
    if _weakness(c_live, c_prev, direction, atr):
        result["reason"] = "Debilidad detectada"
        return result

    # Final/extensión de tendencia.
    if _end_of_trend(history, c_live, direction, atr):
        result["reason"] = "Final/extensión de tendencia"
        return result

    # Confirmación de continuidad.
    if direction == "bullish":
        valid = (
            c_live["close"] > c_live["open"]
            and c_live["close"] > c_prev["close"]
            and c_live["body"] >= atr * MIN_BODY_ATR
            and c_live["close"] >= c_live["low"] + c_live["range"] * 0.55
        )
        signal = "call"
    else:
        valid = (
            c_live["close"] < c_live["open"]
            and c_live["close"] < c_prev["close"]
            and c_live["body"] >= atr * MIN_BODY_ATR
            and c_live["close"] <= c_live["high"] - c_live["range"] * 0.55
        )
        signal = "put"

    if not valid:
        result["reason"] = "Continuidad no confirmada"
        return result

    result.update(
        {
            "signal": signal,
            "reason": "Continuidad confirmada en extremo estructural",
            "score": 5,
            "continuity": True,
            "blocked": False,
            "zone": zone,
            "entry_type": "force",
            "entry_quality": 5,
            "analysis": {
                "force": True,
                "structure": direction,
                "atr": atr,
                "last_swing_high": recent_high,
                "last_swing_low": recent_low,
                "entry_quality": 5,
                "extreme_zone": zone,
            },
            "signal_price": c_live["close"],
            "candle_open": c_live["open"],
            "candle_close": c_live["close"],
        }
    )

    return result


# Compatibilidad con versiones anteriores que importen otras funciones.
def candle_direction(candle: pd.Series) -> str:
    if float(candle["close"]) > float(candle["open"]):
        return "bull"
    if float(candle["close"]) < float(candle["open"]):
        return "bear"
    return "neutral"


def detect_structure(df: pd.DataFrame) -> str:
    work = _validate_df(df)
    if work is None:
        return "range"
    return _structure(work.tail(MAX_CANDLES))


def is_near_sr(df: pd.DataFrame, tolerance: float = 0.0003) -> bool:
    work = _validate_df(df)
    if work is None or len(work) < 5:
        return True

    price = float(work["close"].iloc[-1])
    high = float(work["high"].tail(SR_LOOKBACK).max())
    low = float(work["low"].tail(SR_LOOKBACK).min())

    return abs(price - high) <= tolerance or abs(price - low) <= tolerance
