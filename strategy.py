from __future__ import annotations

"""Estrategia estricta M15 -> M1 para expiracion de 1 minuto.

Regla principal:
    M15 tendencia clara
        -> M1 estructura
        -> rechazo de soporte/resistencia
        -> LH + LL (PUT) o HL + HH (CALL)
        -> ruptura del swing de confirmacion
        -> vela de confirmacion cerrada
        -> entrada M1 con expiracion de 1 minuto.

No se opera por momentum aislado. Si falta una pieza del patron, se bloquea.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 40
M15_MIN_BARS = 8
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 36
SR_LOOKBACK = 24
ZONE_TOLERANCE = 0.0015
DOJI_BODY_MAX = 0.10
INDECISION_BODY_MAX = 0.22
CONFIRM_BODY_MIN = 0.35
STRONG_BODY_MIN = 0.50
WICK_BODY_MIN = 1.15
MIN_ROOM_RANGES = 1.00
REJECTION_LOOKBACK = 6
MAX_PATTERN_BARS = 8

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
}


def _empty(reason: str = "sin señal", mode: str = "M1_M1") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
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
        d = d.sort_values("from")

    return d.dropna(subset=required).reset_index(drop=True)


def _direction(row: pd.Series) -> str:
    o = float(row["open"])
    c = float(row["close"])
    if c > o:
        return "bullish"
    if c < o:
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
    """Retorna swings confirmados. El ultimo swing no puede ser la vela abierta."""
    highs, lows = [], []
    if len(data) < SWING_LEFT + SWING_RIGHT + 1:
        return highs, lows

    start = max(SWING_LEFT, len(data) - SWING_LOOKBACK - SWING_RIGHT)
    end = len(data) - SWING_RIGHT

    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        left_h = float(data.iloc[i - SWING_LEFT:i]["high"].max())
        right_h = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["high"].max())
        left_l = float(data.iloc[i - SWING_LEFT:i]["low"].min())
        right_l = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["low"].min())

        if h >= left_h and h >= right_h:
            highs.append((i, h))
        if l <= left_l and l <= right_l:
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


def _zones(data: pd.DataFrame) -> Tuple[float, float]:
    """Zonas calculadas sin usar la vela de confirmacion."""
    base = data.iloc[:-1].tail(SR_LOOKBACK)
    highs, lows = _swings(data.iloc[:-1])

    resistance = highs[-1][1] if highs else float(base["high"].max())
    support = lows[-1][1] if lows else float(base["low"].min())
    return float(support), float(resistance)


def _rejection(row: pd.Series, support: float, resistance: float) -> Dict[str, bool]:
    m = _metrics(row)

    near_support = (
        m["low"] <= support * (1 + ZONE_TOLERANCE)
        and m["close"] >= support
    )
    near_resistance = (
        m["high"] >= resistance * (1 - ZONE_TOLERANCE)
        and m["close"] <= resistance
    )

    bullish = (
        near_support
        and _direction(row) == "bullish"
        and m["lower_body"] >= WICK_BODY_MIN
        and m["close_pos"] >= 0.60
    )
    bearish = (
        near_resistance
        and _direction(row) == "bearish"
        and m["upper_body"] >= WICK_BODY_MIN
        and m["close_pos"] <= 0.40
    )

    return {
        "bullish": bullish,
        "bearish": bearish,
        "near_support": near_support,
        "near_resistance": near_resistance,
    }


def _find_rejection(
    data: pd.DataFrame,
    support: float,
    resistance: float,
    side: str,
) -> Dict[str, Any]:
    """Busca el rechazo antes de la vela de confirmacion."""
    last_confirm = len(data) - 1
    start = max(0, last_confirm - REJECTION_LOOKBACK - 1)

    for idx in range(last_confirm - 1, start - 1, -1):
        r = _rejection(data.iloc[idx], support, resistance)
        if r[side]:
            return {
                "found": True,
                "index": idx,
                "age": last_confirm - idx,
                "type": "support" if side == "bullish" else "resistance",
                "price": float(data.iloc[idx]["close"]),
                "high": float(data.iloc[idx]["high"]),
                "low": float(data.iloc[idx]["low"]),
                "quality": r,
            }

    return {
        "found": False,
        "index": None,
        "age": None,
        "type": None,
        "price": None,
        "high": None,
        "low": None,
        "quality": {},
    }


def _pattern_after_rejection(
    data: pd.DataFrame,
    signal: str,
    rejection: Dict[str, Any],
) -> Dict[str, Any]:
    """Patron obligatorio posterior al rechazo.

    PUT: despues del rechazo debe aparecer LH y luego LL; la ultima vela debe
         romper el LL y cerrar debajo de el.
    CALL: despues del rechazo debe aparecer HL y luego HH; la ultima vela debe
          romper el HH y cerrar encima de el.
    """
    ridx = rejection.get("index")
    last = len(data) - 1

    empty = {
        "ok": False,
        "lh_hl": False,
        "ll_hh": False,
        "break": False,
        "rejection_index": ridx,
        "lh_hl_index": None,
        "ll_hh_index": None,
        "break_level": None,
    }

    if ridx is None or ridx >= last - 2:
        return empty

    # Solo se usan velas entre rechazo y confirmacion; la ultima queda reservada
    # exclusivamente para la ruptura/confirmacion.
    post = data.iloc[ridx + 1:last].copy().reset_index(drop=True)
    if len(post) < 3:
        return empty

    hs, ls = _swings(post)
    if not hs or not ls:
        return empty

    rejection_high = float(data.iloc[ridx]["high"])
    rejection_low = float(data.iloc[ridx]["low"])

    if signal == "put":
        # El LH tiene que quedar por debajo del maximo del rechazo y antes del LL.
        candidate_h = [x for x in hs if x[1] < rejection_high]
        if not candidate_h:
            return empty
        lh_rel, lh_price = candidate_h[-1]

        candidate_l = [x for x in ls if x[0] > lh_rel and x[1] < rejection_low]
        if not candidate_l:
            return empty
        ll_rel, ll_price = candidate_l[-1]

        ll_abs = ridx + 1 + ll_rel
        confirm = _metrics(data.iloc[-1])
        brk = confirm["close"] < ll_price

        return {
            "ok": bool(brk),
            "lh_hl": True,
            "ll_hh": True,
            "break": bool(brk),
            "rejection_index": ridx,
            "lh_hl_index": ridx + 1 + lh_rel,
            "ll_hh_index": ll_abs,
            "break_level": float(ll_price),
        }

    # CALL: HL por encima del minimo del rechazo, despues HH por encima del maximo.
    candidate_l = [x for x in ls if x[1] > rejection_low]
    if not candidate_l:
        return empty
    hl_rel, hl_price = candidate_l[-1]

    candidate_h = [x for x in hs if x[0] > hl_rel and x[1] > rejection_high]
    if not candidate_h:
        return empty
    hh_rel, hh_price = candidate_h[-1]

    hh_abs = ridx + 1 + hh_rel
    confirm = _metrics(data.iloc[-1])
    brk = confirm["close"] > hh_price

    return {
        "ok": bool(brk),
        "lh_hl": True,
        "ll_hh": True,
        "break": bool(brk),
        "rejection_index": ridx,
        "lh_hl_index": ridx + 1 + hl_rel,
        "ll_hh_index": hh_abs,
        "break_level": float(hh_price),
    }


def _room(
    data: pd.DataFrame,
    signal: str,
    support: float,
    resistance: float,
) -> Dict[str, float | bool]:
    recent = data.tail(7)
    typical = float((recent["high"] - recent["low"]).median())
    close = float(data.iloc[-1]["close"])
    distance = close - support if signal == "put" else resistance - close
    ranges = distance / max(typical, 1e-12)
    return {"ok": ranges >= MIN_ROOM_RANGES, "ranges": ranges, "distance": distance}


def _blocked_result(
    data: pd.DataFrame,
    mode: str,
    reason: str,
    higher: str,
    local: Dict[str, Any],
    support: float,
    resistance: float,
    rejection: Optional[Dict[str, Any]] = None,
    pattern: Optional[Dict[str, Any]] = None,
    room: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        **_empty(reason, mode),
        "trend": local.get("structure", "range"),
        "higher_trend": higher,
        "analysis_timeframe": "M1",
        "analysis": {
            "m15_trend": higher,
            "m1_structure": local,
            "support": support,
            "resistance": resistance,
            "rejection": rejection or {},
            "pattern": pattern or {},
            "room": room or {},
        },
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    mode = "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _empty(f"historial M1 insuficiente {len(data)}/{MIN_BARS}", mode)

    higher = _normalize(higher_tf_df)
    if len(higher) < M15_MIN_BARS:
        return _empty(f"historial M15 insuficiente {len(higher)}/{M15_MIN_BARS}", mode)

    # M15 debe ser estructural; no se sustituye por un simple desplazamiento de precio.
    m15 = _structure(higher)
    m15_trend = m15["structure"]

    if m15_trend not in ("bullish", "bearish"):
        return _blocked_result(
            data, mode, "M15 sin estructura HH+HL o LH+LL clara",
            m15_trend, _structure(data), *_zones(data)
        )

    local = _structure(data)
    support, resistance = _zones(data)
    expected = "put" if m15_trend == "bearish" else "call"

    # La estructura general M1 debe acompañar al M15.
    if local["structure"] != m15_trend:
        return _blocked_result(
            data, mode,
            f"estructura M1 {local['structure']} no coincide con M15 {m15_trend}",
            m15_trend, local, support, resistance,
        )

    current = _metrics(data.iloc[-1])
    if current["body_ratio"] <= DOJI_BODY_MAX or current["body_ratio"] <= INDECISION_BODY_MAX:
        return _blocked_result(
            data, mode, "vela final indecisa", m15_trend,
            local, support, resistance,
        )

    rejection = _find_rejection(
        data,
        support,
        resistance,
        "bearish" if expected == "put" else "bullish",
    )
    if not rejection["found"]:
        return _blocked_result(
            data, mode,
            f"sin rechazo reciente de {'RESISTENCIA' if expected == 'put' else 'SOPORTE'}",
            m15_trend, local, support, resistance,
        )

    # Patron obligatorio: rechazo -> LH+LL/HL+HH -> ruptura.
    pattern = _pattern_after_rejection(data, expected, rejection)
    if not pattern["ok"]:
        return _blocked_result(
            data, mode,
            f"patron incompleto: falta {'LH + LL + ruptura' if expected == 'put' else 'HL + HH + ruptura'}",
            m15_trend, local, support, resistance, rejection, pattern,
        )

    direction_ok = _direction(data.iloc[-1]) == ("bearish" if expected == "put" else "bullish")
    body_ok = current["body_ratio"] >= CONFIRM_BODY_MIN
    close_ok = current["close_pos"] <= 0.35 if expected == "put" else current["close_pos"] >= 0.65

    if not direction_ok or not body_ok or not close_ok:
        return _blocked_result(
            data, mode,
            "vela de confirmacion no tiene cuerpo/cierre suficiente",
            m15_trend, local, support, resistance, rejection, pattern,
        )

    room = _room(data, expected, support, resistance)
    if not room["ok"]:
        return _blocked_result(
            data, mode,
            f"poco recorrido hasta zona contraria ({room['ranges']:.2f}R)",
            m15_trend, local, support, resistance, rejection, pattern, room,
        )

    # Evita perseguir una vela anormalmente grande justo en la confirmacion.
    if current["body_ratio"] >= 0.50 and current["range"] > float((data.tail(8)["high"] - data.tail(8)["low"]).median()) * 2.5:
        return _blocked_result(
            data, mode, "confirmacion demasiado extendida para entrada M1",
            m15_trend, local, support, resistance, rejection, pattern, room,
        )

    score = 88
    if rejection["age"] == 1:
        score += 4
    if current["body_ratio"] >= STRONG_BODY_MIN:
        score += 3
    if room["ranges"] >= 1.5:
        score += 3
    if current["body_ratio"] >= 0.45:
        score += 2
    score = min(score, 100)

    reason = (
        f"{expected.upper()} | M15 {m15_trend} | "
        f"M1 {'LH+LL' if expected == 'put' else 'HL+HH'} | "
        f"rechazo {'RESISTENCIA' if expected == 'put' else 'SOPORTE'} "
        f"hace {rejection['age']} vela(s) | ruptura confirmada | "
        f"vela {'bajista' if expected == 'put' else 'alcista'} | "
        f"recorrido {room['ranges']:.2f}R"
    )

    return {
        "signal": expected,
        "direction": expected,
        "trend": m15_trend,
        "higher_trend": m15_trend,
        "reason": reason,
        "score": score,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "analysis": {
            "pair": pair,
            "m15": m15,
            "m15_trend": m15_trend,
            "m1_structure": local,
            "support": support,
            "resistance": resistance,
            "rejection": rejection,
            "pattern": pattern,
            "room": room,
            "confirmation": {
                "direction_ok": direction_ok,
                "body_ok": body_ok,
                "close_ok": close_ok,
            },
        },
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df, mode="M1_M1").get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
