from __future__ import annotations

"""Estrategia PURA de ACCION DEL PRECIO para IQ Option.

NO utiliza indicadores tecnicos.

Reglas M1 -> M1:
- Solo velas M1 cerradas.
- Estructura alcista: HH + HL recientes.
- Estructura bajista: LH + LL recientes.
- CALL: estructura alcista + rechazo de soporte + confirmacion alcista.
- PUT : estructura bajista + rechazo de resistencia + confirmacion bajista.
- La vela de confirmacion es la ultima vela cerrada; la entrada se hace
  en la apertura de la siguiente M1.
- Expiracion: 1 minuto.

No se utilizan CI, EMA, RSI, MACD, ATR, volumen ni ningun otro indicador.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

M1 = 60
MIN_BARS = 30
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 30
SR_LOOKBACK = 20
REJECTION_LOOKBACK = 3
ZONE_TOLERANCE = 0.0015
DOJI_BODY_MAX = 0.10
CONFIRM_BODY_MIN = 0.35
WICK_BODY_MIN = 1.20
MIN_ROOM_RANGES = 1.20

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
}


def _empty(reason: str = "sin señal", mode: str = "M1_M1") -> Dict[str, Any]:
    cfg = MODE_CONFIG[mode]
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": mode,
        "analysis_timeframe": cfg["analysis_tf"],
        "entry_timeframe": cfg["entry_tf"],
        "target_expiration_minutes": cfg["expiration"],
        "candle_timestamp": None,
        "analysis": {},
    }


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    d = df.copy().rename(columns={"max": "high", "min": "low"})
    required = ["open", "high", "low", "close"]
    if any(c not in d.columns for c in required):
        return pd.DataFrame()

    for c in required:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    if "from" in d.columns:
        d["from"] = pd.to_numeric(d["from"], errors="coerce")
        d = d.sort_values("from").drop_duplicates("from")

    return d.dropna(subset=required).reset_index(drop=True)


def _direction(row: pd.Series) -> str:
    if float(row["close"]) > float(row["open"]):
        return "bullish"
    if float(row["close"]) < float(row["open"]):
        return "bearish"
    return "neutral"


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "body_ratio": body / rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_body": upper / max(body, 1e-12),
        "lower_body": lower / max(body, 1e-12),
        "close_pos": (c - l) / rng,
    }


def _swings(data: pd.DataFrame) -> Tuple[list, list]:
    highs, lows = [], []
    start = max(SWING_LEFT, len(data) - SWING_LOOKBACK - SWING_RIGHT)
    end = len(data) - SWING_RIGHT

    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        left_high = float(data.iloc[i - SWING_LEFT:i]["high"].max())
        right_high = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["high"].max())
        left_low = float(data.iloc[i - SWING_LEFT:i]["low"].min())
        right_low = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["low"].min())

        if h >= left_high and h >= right_high:
            highs.append((i, h))
        if l <= left_low and l <= right_low:
            lows.append((i, l))

    return highs, lows


def _structure(data: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _swings(data)
    structure = "range"

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            structure = "bullish"
        elif lh and ll:
            structure = "bearish"

    return {
        "structure": structure,
        "highs": highs,
        "lows": lows,
        "last_high": highs[-1][1] if highs else None,
        "previous_high": highs[-2][1] if len(highs) >= 2 else None,
        "last_low": lows[-1][1] if lows else None,
        "previous_low": lows[-2][1] if len(lows) >= 2 else None,
    }


def _zones(data: pd.DataFrame, st: Dict[str, Any]) -> Tuple[float, float]:
    recent = data.iloc[max(0, len(data) - SR_LOOKBACK - 1):-1]
    support = st.get("last_low")
    resistance = st.get("last_high")

    if support is None and not recent.empty:
        support = float(recent["low"].min())
    if resistance is None and not recent.empty:
        resistance = float(recent["high"].max())

    if support is None:
        support = float(data["low"].min())
    if resistance is None:
        resistance = float(data["high"].max())

    return float(support), float(resistance)


def _rejection(row: pd.Series, support: float, resistance: float) -> Dict[str, Any]:
    m = _metrics(row)
    near_support = m["low"] <= support * (1 + ZONE_TOLERANCE) and m["close"] >= support
    near_resistance = m["high"] >= resistance * (1 - ZONE_TOLERANCE) and m["close"] <= resistance

    bull = (
        near_support
        and _direction(row) == "bullish"
        and m["lower_body"] >= WICK_BODY_MIN
        and m["close_pos"] >= 0.60
    )
    bear = (
        near_resistance
        and _direction(row) == "bearish"
        and m["upper_body"] >= WICK_BODY_MIN
        and m["close_pos"] <= 0.40
    )

    return {
        "bull_rejection": bull,
        "bear_rejection": bear,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "metrics": m,
    }


def _find_rejection(data: pd.DataFrame, support: float, resistance: float, structure: str) -> Dict[str, Any]:
    end = len(data) - 1  # ultima cerrada = confirmacion, no rechazo
    start = max(0, end - REJECTION_LOOKBACK)

    for idx in range(end - 1, start - 1, -1):
        row = data.iloc[idx]
        r = _rejection(row, support, resistance)
        if structure == "bullish" and r["bull_rejection"]:
            return {"found": True, "type": "support", "index": idx, "age": end - idx, "quality": r}
        if structure == "bearish" and r["bear_rejection"]:
            return {"found": True, "type": "resistance", "index": idx, "age": end - idx, "quality": r}

    return {"found": False, "type": None, "index": None, "age": None, "quality": {}}


def _confirm(data: pd.DataFrame, signal: str, rejection: Dict[str, Any]) -> Dict[str, Any]:
    cur = _metrics(data.iloc[-1])
    prev = _metrics(data.iloc[-2])
    direction = _direction(data.iloc[-1])

    if signal == "call":
        direction_ok = direction == "bullish"
        body_ok = cur["body_ratio"] >= CONFIRM_BODY_MIN
        close_progress = cur["close"] > prev["close"]
        break_ok = rejection["index"] is not None and cur["close"] >= float(data.iloc[rejection["index"]]["high"])
    else:
        direction_ok = direction == "bearish"
        body_ok = cur["body_ratio"] >= CONFIRM_BODY_MIN
        close_progress = cur["close"] < prev["close"]
        break_ok = rejection["index"] is not None and cur["close"] <= float(data.iloc[rejection["index"]]["low"])

    return {
        "direction_ok": direction_ok,
        "body_ok": body_ok,
        "close_progress": close_progress,
        "break_rejection": break_ok,
        "strong": direction_ok and body_ok and close_progress,
    }


def _room(data: pd.DataFrame, signal: str, support: float, resistance: float) -> Dict[str, float | bool]:
    recent = data.iloc[-7:]
    typical = float((recent["high"] - recent["low"]).median())
    if typical <= 0:
        return {"ok": False, "distance": 0.0, "ranges": 0.0}

    close = float(data.iloc[-1]["close"])
    distance = resistance - close if signal == "call" else close - support
    ranges = distance / typical
    return {"ok": ranges >= MIN_ROOM_RANGES, "distance": distance, "ranges": ranges}


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **_: Any,
) -> Dict[str, Any]:
    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}", mode)

    st = _structure(data)
    structure = st["structure"]
    info = _metrics(data.iloc[-1])
    direction = _direction(data.iloc[-1])
    ts = int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None

    if structure not in ("bullish", "bearish"):
        return _empty("estructura sin direccion clara", mode)

    if info["body_ratio"] <= DOJI_BODY_MAX:
        return _empty("ultima vela cerrada es indecision", mode)

    support, resistance = _zones(data, st)
    expected = "call" if structure == "bullish" else "put"
    rejection = _find_rejection(data, support, resistance, structure)

    if not rejection["found"]:
        return _empty("sin rechazo reciente de soporte/resistencia", mode)

    confirmation = _confirm(data, expected, rejection)
    if not confirmation["strong"]:
        return _empty("rechazo sin confirmacion de precio suficiente", mode)

    if rejection["age"] > REJECTION_LOOKBACK:
        return _empty("rechazo demasiado antiguo", mode)

    room = _room(data, expected, support, resistance)
    if not room["ok"]:
        return _empty("poco recorrido libre hasta la zona contraria", mode)

    rejection_name = "SOPORTE" if expected == "call" else "RESISTENCIA"
    candle_name = "verde" if expected == "call" else "roja"
    reason = (
        f"{expected.upper()} | estructura {structure} | "
        f"vela {candle_name} | rechazo {rejection_name} "
        f"hace {int(rejection['age'])} vela(s) | "
        f"confirmacion {'ruptura' if confirmation['break_rejection'] else 'continuacion'}"
    )

    return {
        "signal": expected,
        "direction": direction,
        "trend": structure,
        "higher_trend": "range",
        "reason": reason,
        "score": 100,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "candle_timestamp": ts,
        "analysis": {
            "structure": structure,
            "support": support,
            "resistance": resistance,
            "rejection": rejection,
            "confirmation": confirmation,
            "room": room,
            "signal_candle_from": ts,
            "signal_candle_color": candle_name,
        },
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
