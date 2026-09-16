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


MAX_CANDLES = 60
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14

STRUCTURE_LOOKBACK = 8
SR_LOOKBACK = 20

# Multiplicadores deliberadamente conservadores: si el precio está cerca
# de una zona importante, la operación se bloquea.
SR_ATR_DISTANCE = 0.45
REJECTION_WICK_RATIO = 0.55
MIN_BODY_ATR = 0.22
MAX_COUNTER_WICK_ATR = 0.65

# Evita entrar cuando la tendencia ya está demasiado extendida.
END_TREND_DISTANCE_ATR = 0.75

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


def _near_sr(
    history: pd.DataFrame,
    price: float,
    atr: float,
) -> tuple[bool, Optional[str], Optional[float]]:
    """
    Bloqueo absoluto cerca de máximos/mínimos recientes.
    history NO incluye la vela viva.
    """
    if len(history) < 5:
        return True, "insuficiente_historial", None

    w = history.tail(SR_LOOKBACK)

    recent_high = float(w["high"].max())
    recent_low = float(w["low"].min())

    tolerance = max(atr * SR_ATR_DISTANCE, EPS)

    dist_high = abs(price - recent_high)
    dist_low = abs(price - recent_low)

    if dist_high <= tolerance:
        return True, "resistencia", recent_high

    if dist_low <= tolerance:
        return True, "soporte", recent_low

    return False, None, None


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
    w = history.tail(SR_LOOKBACK)

    recent_high = float(w["high"].max())
    recent_low = float(w["low"].min())
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

    # S/R: bloqueo absoluto.
    blocked, zone, level = _near_sr(
        history,
        c_live["close"],
        atr,
    )

    if blocked:
        result["reason"] = f"Precio en {zone}"
        result["zone"] = zone
        result["level"] = level
        return result

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
            "reason": "Continuidad confirmada",
            "score": 5,
            "continuity": True,
            "blocked": False,
            "zone": "continuidad",
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
