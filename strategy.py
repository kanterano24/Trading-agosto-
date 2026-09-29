from __future__ import annotations

"""Estrategia de accion del precio para M1, M2 y M5.

Objetivo:
- M1 -> M1 -> 1 minuto.
- M2 -> M2 -> 2 minutos.
- M5 -> M5 -> 5 minutos.
- M1/M2 solo operan en la direccion de una tendencia M5 clara.
- Toda entrada exige un rechazo real de soporte/resistencia y una vela
  posterior de confirmacion en la misma direccion.
- Una reversa aislada, una vela sin cuerpo suficiente o una entrada pegada
  al nivel contrario se bloquean.
- La estructura debe ser estrictamente alcista para CALL o bajista para PUT.
- La ultima vela cerrada (vela anterior a la entrada) debe tener el mismo
  color de la operacion: verde para CALL, roja para PUT.
- Solo se usan velas cerradas y precio/estructura; no se usan indicadores.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 30
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 30
SR_LOOKBACK = 20

# Tolerancia para considerar que una mecha toco una zona.
ZONE_TOLERANCE_MIN = 0.0003
ZONE_TOLERANCE_MAX = 0.0012
ZONE_RANGE_FACTOR = 0.20

DOJI_BODY_MAX = 0.10
INDECISION_BODY_MAX = 0.25
CONFIRM_BODY_MIN = 0.35
STRONG_BODY_MIN = 0.55
WICK_BODY_MIN = 1.20
MIN_ROOM_RANGES = 1.20
REJECTION_LOOKBACK = 3

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
    "M2_M2": {"analysis_tf": "M2", "entry_tf": "M2", "expiration": 2},
    "M5_M5": {"analysis_tf": "M5", "entry_tf": "M5", "expiration": 5},
}


def _empty(reason: str = "sin señal", mode: str = "") -> Dict[str, Any]:
    cfg = MODE_CONFIG.get(mode, {})
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": mode,
        "analysis_timeframe": cfg.get("analysis_tf"),
        "entry_timeframe": cfg.get("entry_tf"),
        "target_expiration_minutes": cfg.get("expiration"),
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
    close = float(row["close"])
    open_ = float(row["open"])
    if close > open_:
        return "bullish"
    if close < open_:
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

        lh = data.iloc[i - SWING_LEFT:i]["high"].max()
        rh = data.iloc[i + 1:i + 1 + SWING_RIGHT]["high"].max()
        ll = data.iloc[i - SWING_LEFT:i]["low"].min()
        rl = data.iloc[i + 1:i + 1 + SWING_RIGHT]["low"].min()

        if h >= float(lh) and h >= float(rh):
            highs.append((i, h))
        if l <= float(ll) and l <= float(rl):
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


def _price_action_trend(data: pd.DataFrame, lookback: int = 7) -> str:
    if data is None or len(data) < lookback:
        return "range"

    recent = data.iloc[-lookback:]
    dirs = [_direction(row) for _, row in recent.iterrows()]
    bull = sum(x == "bullish" for x in dirs)
    bear = sum(x == "bearish" for x in dirs)

    net = float(recent.iloc[-1]["close"]) - float(recent.iloc[0]["close"])
    ranges = (recent["high"] - recent["low"]).astype(float)
    typical = float(ranges.median()) if not ranges.empty else 0.0

    if typical <= 0:
        return "range"

    # No basta con el color de las velas: debe existir desplazamiento.
    if bull >= 5 and net > 0.8 * typical:
        return "bullish"
    if bear >= 5 and net < -0.8 * typical:
        return "bearish"
    if bull >= 4 and net > 1.2 * typical:
        return "bullish"
    if bear >= 4 and net < -1.2 * typical:
        return "bearish"

    return "range"


def _combined_trend(data: pd.DataFrame) -> Dict[str, Any]:
    st = _structure(data)
    swing = st["structure"]
    price = _price_action_trend(data, lookback=7)

    if swing == price and swing in ("bullish", "bearish"):
        combined = swing
    elif swing in ("bullish", "bearish") and price == "range":
        combined = swing
    elif price in ("bullish", "bearish") and swing == "range":
        combined = price
    else:
        # Dos lecturas opuestas = no se adivina.
        combined = "range"

    return {
        "trend": combined,
        "swing": swing,
        "price": price,
        "structure": st,
    }


def _candle_info(data: pd.DataFrame) -> Dict[str, Any]:
    i = len(data) - 1
    cur = _metrics(data.iloc[i])
    prev = _metrics(data.iloc[i - 1]) if i else cur

    direction = _direction(data.iloc[i])
    previous_direction = _direction(data.iloc[i - 1]) if i else "neutral"

    doji = cur["body_ratio"] <= DOJI_BODY_MAX
    indecision = cur["body_ratio"] <= INDECISION_BODY_MAX
    strong = cur["body_ratio"] >= STRONG_BODY_MIN

    bullish_momentum = (
        direction == "bullish"
        and cur["body_ratio"] >= 0.50
        and cur["close_pos"] >= 0.70
        and cur["close"] > prev["high"]
    )
    bearish_momentum = (
        direction == "bearish"
        and cur["body_ratio"] >= 0.50
        and cur["close_pos"] <= 0.30
        and cur["close"] < prev["low"]
    )

    bullish_engulf = (
        direction == "bullish"
        and previous_direction == "bearish"
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
    )
    bearish_engulf = (
        direction == "bearish"
        and previous_direction == "bullish"
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )

    return {
        **cur,
        "direction": direction,
        "previous_direction": previous_direction,
        "doji": doji,
        "indecision": indecision,
        "strong": strong,
        "bullish_momentum": bullish_momentum,
        "bearish_momentum": bearish_momentum,
        "bullish_engulf": bullish_engulf,
        "bearish_engulf": bearish_engulf,
    }


def _recent_pressure(data: pd.DataFrame, trend: str) -> Dict[str, Any]:
    if len(data) < 5 or trend not in ("bullish", "bearish"):
        return {
            "aligned": False,
            "aligned_count": 0,
            "opposite_count": 0,
            "last_two_aligned": False,
            "net_aligned": False,
            "directions": [],
            "net_move": 0.0,
        }

    recent = data.iloc[-5:]
    dirs = [_direction(row) for _, row in recent.iterrows()]
    aligned_count = sum(1 for x in dirs if x == trend)
    opposite_count = sum(
        1 for x in dirs if x not in (trend, "neutral")
    )

    last_two_aligned = dirs[-1] == trend and dirs[-2] == trend

    first_close = float(recent.iloc[0]["close"])
    last_close = float(recent.iloc[-1]["close"])
    net_move = last_close - first_close
    net_aligned = net_move > 0 if trend == "bullish" else net_move < 0

    return {
        "aligned": aligned_count >= 3 and net_aligned,
        "aligned_count": aligned_count,
        "opposite_count": opposite_count,
        "last_two_aligned": last_two_aligned,
        "net_aligned": net_aligned,
        "directions": dirs,
        "net_move": net_move,
    }


def _zones(data: pd.DataFrame, st: Dict[str, Any]):
    # Importante: no usamos la vela actual para definir el nivel.
    # Asi no se crea una "zona" artificial con la propia vela de entrada.
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


def _adaptive_zone_tolerance(data: pd.DataFrame) -> float:
    """Tolerancia de S/R adaptada al movimiento real del par.

    Evita que un porcentaje fijo sea demasiado ancho en pares baratos o
    demasiado estrecho en pares con velas mas amplias.
    """
    if data is None or data.empty:
        return ZONE_TOLERANCE_MIN

    recent = data.iloc[-min(10, len(data)):]
    typical_range = float((recent["high"] - recent["low"]).median())
    price = max(abs(float(recent.iloc[-1]["close"])), 1e-12)

    tolerance = (typical_range / price) * ZONE_RANGE_FACTOR
    return max(ZONE_TOLERANCE_MIN, min(ZONE_TOLERANCE_MAX, tolerance))


def _rejection_for_candle(
    row: pd.Series,
    support: float,
    resistance: float,
    tolerance: float,
) -> Dict[str, Any]:
    m = _metrics(row)

    near_support = (
        m["low"] <= support * (1 + tolerance)
        and m["close"] >= support
    )
    near_resistance = (
        m["high"] >= resistance * (1 - tolerance)
        and m["close"] <= resistance
    )

    bull_rejection = (
        near_support
        and _direction(row) == "bullish"
        and m["lower_body"] >= WICK_BODY_MIN
        and m["close_pos"] >= 0.60
    )

    bear_rejection = (
        near_resistance
        and _direction(row) == "bearish"
        and m["upper_body"] >= WICK_BODY_MIN
        and m["close_pos"] <= 0.40
    )

    return {
        "bull_rejection": bull_rejection,
        "bear_rejection": bear_rejection,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "metrics": m,
    }


def _find_recent_rejection(
    data: pd.DataFrame,
    support: float,
    resistance: float,
    allowed: str,
    tolerance: float,
) -> Dict[str, Any]:
    # La ultima vela es la confirmacion. La rechazadora debe estar antes.
    start = max(0, len(data) - 1 - REJECTION_LOOKBACK)
    end = len(data) - 1

    for idx in range(end - 1, start - 1, -1):
        row = data.iloc[idx]
        r = _rejection_for_candle(row, support, resistance, tolerance)

        if allowed == "bullish" and r["bull_rejection"]:
            return {
                "found": True,
                "type": "support",
                "index": idx,
                "age": end - idx,
                "timestamp": int(row["from"]) if "from" in data.columns and pd.notna(row["from"]) else None,
                "quality": r,
            }

        if allowed == "bearish" and r["bear_rejection"]:
            return {
                "found": True,
                "type": "resistance",
                "index": idx,
                "age": end - idx,
                "timestamp": int(row["from"]) if "from" in data.columns and pd.notna(row["from"]) else None,
                "quality": r,
            }

    return {
        "found": False,
        "type": None,
        "index": None,
        "age": None,
        "timestamp": None,
        "quality": {},
    }


def _confirmation_quality(data: pd.DataFrame, signal: str, rejection: Dict[str, Any]) -> Dict[str, Any]:
    cur = _metrics(data.iloc[-1])
    prev = _metrics(data.iloc[-2])

    if signal == "call":
        direction_ok = _direction(data.iloc[-1]) == "bullish"
        body_ok = cur["body_ratio"] >= CONFIRM_BODY_MIN
        close_progress = cur["close"] > prev["close"]
        break_rejection = (
            rejection.get("index") is not None
            and cur["close"] >= float(data.iloc[rejection["index"]]["high"])
        )
        momentum = cur["body_ratio"] >= STRONG_BODY_MIN and cur["close_pos"] >= 0.65
    else:
        direction_ok = _direction(data.iloc[-1]) == "bearish"
        body_ok = cur["body_ratio"] >= CONFIRM_BODY_MIN
        close_progress = cur["close"] < prev["close"]
        break_rejection = (
            rejection.get("index") is not None
            and cur["close"] <= float(data.iloc[rejection["index"]]["low"])
        )
        momentum = cur["body_ratio"] >= STRONG_BODY_MIN and cur["close_pos"] <= 0.35

    return {
        "direction_ok": direction_ok,
        "body_ok": body_ok,
        "close_progress": close_progress,
        "break_rejection": break_rejection,
        "momentum": momentum,
        "strong": direction_ok and body_ok and close_progress,
    }


def _room_from_opposite_zone(
    data: pd.DataFrame,
    signal: str,
    support: float,
    resistance: float,
) -> Dict[str, Any]:
    recent = data.iloc[-7:]
    typical = float((recent["high"] - recent["low"]).median())
    if typical <= 0:
        return {"ok": False, "distance": 0.0, "ranges": 0.0}

    last_close = float(data.iloc[-1]["close"])

    if signal == "call":
        distance = resistance - last_close
    else:
        distance = last_close - support

    ranges = distance / typical
    return {
        "ok": ranges >= MIN_ROOM_RANGES,
        "distance": distance,
        "ranges": ranges,
    }


def _blocked(
    data: pd.DataFrame,
    st: Dict[str, Any],
    info: Dict[str, Any],
    zone: Dict[str, Any],
    mode: str,
    reason: str,
    higher: str = "range",
) -> Dict[str, Any]:
    r = _empty(reason, mode)
    ts = None
    if "from" in data.columns and pd.notna(data.iloc[-1]["from"]):
        ts = int(data.iloc[-1]["from"])

    r.update(
        {
            "direction": info.get("direction", "neutral"),
            "trend": st.get("structure", "range"),
            "higher_trend": higher,
            "candle_timestamp": ts,
            "analysis": {
                "structure": st,
                "higher_structure": higher,
                "candle": info,
                "zone": zone,
            },
        }
    )
    return r


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    mode = mode if mode in MODE_CONFIG else "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _blocked(
            data,
            {"structure": "range"},
            {"direction": "neutral"},
            {},
            mode,
            f"historial insuficiente {len(data)}/{MIN_BARS}",
        )

    local = _combined_trend(data)
    trend = local["trend"]
    st = local["structure"]
    local_structure = st["structure"]
    info = _candle_info(data)
    support, resistance = _zones(data, st)

    higher_trend = "range"
    higher_structure = "range"
    higher_detail: Dict[str, Any] = {}

    if (
        higher_tf_df is not None
        and isinstance(higher_tf_df, pd.DataFrame)
        and not higher_tf_df.empty
    ):
        h = _normalize(higher_tf_df)
        if len(h) >= MIN_BARS:
            higher_detail = _combined_trend(h)
            higher_trend = higher_detail["trend"]
            higher_structure = higher_detail["structure"]["structure"]

    # REGLA ESTRICTA DE ESTRUCTURA.
    # M1/M2 deben coincidir con la estructura M5.
    if mode in ("M1_M1", "M2_M2"):
        if higher_structure not in ("bullish", "bearish"):
            return _blocked(
                data,
                st,
                info,
                {"support": support, "resistance": resistance},
                mode,
                "M1/M2 bloqueado: estructura M5 no es alcista ni bajista",
                higher_structure,
            )
        allowed = higher_structure

        if local_structure != allowed:
            return _blocked(
                data,
                st,
                info,
                {"support": support, "resistance": resistance},
                mode,
                f"estructura local {local_structure} contra M5 {allowed}",
                higher_structure,
            )
    else:
        if local_structure not in ("bullish", "bearish"):
            return _blocked(
                data,
                st,
                info,
                {"support": support, "resistance": resistance},
                mode,
                "M5 bloqueado: estructura local no es alcista ni bajista",
                local_structure,
            )
        allowed = local_structure

    # Nunca se usa una vela abierta.
    if info["doji"] or info["indecision"]:
        return _blocked(
            data,
            st,
            info,
            {"support": support, "resistance": resistance},
            mode,
            "vela cerrada sin direccion suficiente",
            higher_trend,
        )

    # La ultima vela cerrada es la vela anterior a la ejecucion.
    # CALL solo con vela verde; PUT solo con vela roja.
    previous_candle = info["direction"]
    expected_signal = "call" if allowed == "bullish" else "put"

    if previous_candle != allowed:
        candle_name = "verde" if previous_candle == "bullish" else "roja" if previous_candle == "bearish" else "neutral"
        return _blocked(
            data,
            st,
            info,
            {"support": support, "resistance": resistance},
            mode,
            f"entrada bloqueada: vela anterior {candle_name}; {expected_signal.upper()} exige vela {'verde' if expected_signal == 'call' else 'roja'}",
            higher_structure if mode in ("M1_M1", "M2_M2") else local_structure,
        )

    signal = expected_signal

    if signal != ("call" if allowed == "bullish" else "put"):
        return _blocked(
            data,
            st,
            info,
            {"support": support, "resistance": resistance},
            mode,
            f"{signal.upper()} contra tendencia establecida {allowed}",
            higher_trend,
        )

    # ---------------------------------------------------------------
    # REGLA PRINCIPAL:
    # La entrada debe venir de un rechazo S/R y luego confirmacion.
    # No hay entrada por "momentum" aislado.
    # ---------------------------------------------------------------
    zone_tolerance = _adaptive_zone_tolerance(data)

    rejection = _find_recent_rejection(
        data,
        support=support,
        resistance=resistance,
        allowed=allowed,
        tolerance=zone_tolerance,
    )

    if not rejection["found"]:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
            },
            mode,
            "entrada bloqueada: no hubo rechazo reciente de S/R a favor de la tendencia",
            higher_trend,
        )

    confirmation = _confirmation_quality(data, signal, rejection)

    if not confirmation["direction_ok"]:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
            },
            mode,
            "entrada bloqueada: la vela posterior al rechazo no confirma la direccion",
            higher_trend,
        )

    if not confirmation["body_ok"] or not confirmation["close_progress"]:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
            },
            mode,
            "entrada bloqueada: confirmacion debil despues del rechazo",
            higher_trend,
        )

    # Rechazo demasiado viejo = no se persigue el movimiento.
    if int(rejection["age"]) > REJECTION_LOOKBACK:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
            },
            mode,
            "entrada bloqueada: rechazo demasiado antiguo",
            higher_trend,
        )

    room = _room_from_opposite_zone(data, signal, support, resistance)
    if not room["ok"]:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
                "room": room,
            },
            mode,
            "entrada bloqueada: poco recorrido libre hasta la zona contraria",
            higher_trend,
        )

    pressure = _recent_pressure(data, allowed)

    # No perseguimos una confirmacion que ya se alejo demasiado del nivel
    # rechazado. La entrada debe seguir representando el rechazo, no un
    # movimiento ya extendido.
    rejection_idx = int(rejection["index"])
    rejection_close = float(data.iloc[rejection_idx]["close"])
    current_close = float(data.iloc[-1]["close"])
    recent_range = float((data.iloc[-7:]["high"] - data.iloc[-7:]["low"]).median())
    extension = abs(current_close - rejection_close) / max(recent_range, 1e-12)
    max_extension = 1.60 if confirmation["break_rejection"] else 1.25

    if extension > max_extension:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
                "room": room,
                "pressure": pressure,
                "extension_ranges": extension,
            },
            mode,
            "entrada bloqueada: confirmacion demasiado extendida desde el rechazo",
            higher_trend,
        )

    # El objetivo es entrar despues del rechazo, no en una vela extendida
    # que ya se haya alejado demasiado de la zona.
    if rejection["age"] > 2 and not confirmation["momentum"]:
        return _blocked(
            data,
            st,
            info,
            {
                "support": support,
                "resistance": resistance,
                "rejection": rejection,
                "confirmation": confirmation,
                "room": room,
                "pressure": pressure,
            },
            mode,
            "entrada bloqueada: rechazo no suficientemente fresco",
            higher_trend,
        )

    # Score = confluencia, NO probabilidad matematica.
    score = 40
    score += 15  # tendencia establecida + rechazo alineado
    score += 15 if confirmation["break_rejection"] else 8
    score += 10 if confirmation["momentum"] else 5
    score += 10 if pressure["aligned_count"] >= 3 else 5 if pressure["aligned_count"] >= 2 else 0
    score += 10 if room["ranges"] >= 2.0 else 5
    score = min(100, int(score))

    rejection_age = int(rejection["age"])
    rejection_name = "SOPORTE" if signal == "call" else "RESISTENCIA"

    candle_color = "verde" if signal == "call" else "roja"
    reason = (
        f"{signal.upper()} | estructura {allowed} | "
        f"vela anterior {candle_color} | "
        f"rechazo {rejection_name} hace {rejection_age} vela(s) | "
        f"confirmacion {'ruptura' if confirmation['break_rejection'] else 'alcista/bajista'} | "
        f"presion {pressure['aligned_count']}/5 | "
        f"recorrido {room['ranges']:.1f}R | "
        f"frescura {rejection_age} | "
        f"extension {extension:.1f}R"
    )

    ts = (
        int(data.iloc[-1]["from"])
        if "from" in data.columns and pd.notna(data.iloc[-1]["from"])
        else None
    )

    return {
        "signal": signal,
        "direction": info["direction"],
        "trend": trend,
        "higher_trend": higher_trend,
        "reason": reason,
        "score": score,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": MODE_CONFIG[mode]["analysis_tf"],
        "entry_timeframe": MODE_CONFIG[mode]["entry_tf"],
        "target_expiration_minutes": MODE_CONFIG[mode]["expiration"],
        "candle_timestamp": ts,
        "analysis": {
            "structure": local_structure,
            "local_swing": local["swing"],
            "local_price_trend": local["price"],
            "higher_structure": higher_structure,
            "higher_trend": higher_trend,
            "higher_detail": higher_detail,
            "allowed_direction": allowed,
            "candle": info,
            "pressure": pressure,
            "rejection": rejection,
            "confirmation": confirmation,
            "room": room,
            "support": support,
            "resistance": resistance,
            "zone_tolerance": zone_tolerance,
            "extension_ranges": extension,
        },
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
