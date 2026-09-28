"""strategy.py - SOLO ACCION DEL PRECIO, sin indicadores.

Entrada CALL: estructura HH/HL + retroceso + rechazo de SOPORTE + momentum.
Entrada PUT: estructura LH/LL + retroceso + rechazo de RESISTENCIA + momentum.
Regla dura: SOPORTE solo permite CALL; RESISTENCIA solo permite PUT.
La estrategia no ejecuta; devuelve call/put para que bot.py ejecute a 2 minutos.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
import pandas as pd

MIN_BARS = 25
STRUCTURE_LOOKBACK = 25
SWING_LEFT = 2
SWING_RIGHT = 2
PULLBACK_LOOKBACK = 4
PULLBACK_MIN_COUNTER_CANDLES = 1
SR_ZONE_TOLERANCE = 0.0015
# Si el precio está en una zona de soporte, jamás se permite PUT.
# Si el precio está en una zona de resistencia, jamás se permite CALL.
MIN_WICK_BODY_RATIO = 0.80
MIN_CLOSE_POSITION = 0.65
TARGET_EXPIRATION_MINUTES = 2


def _empty_result(reason="sin señal"):
    return {
        "signal": None, "direction": "range", "trend": "range",
        "reason": reason, "score": 0, "continuity": False,
        "blocked": True, "zone": "price_action",
        "entry_type": "PRICE_ACTION_STRUCTURE_PULLBACK_REJECTION_2M",
        "entry_quality": 0, "rsi": None, "atr": None,
        "candle_timestamp": None, "analysis": {}
    }


def _normalize(df):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    data = df.copy()
    for col in ("open", "high", "low", "close"):
        if col not in data.columns:
            return pd.DataFrame()
        data[col] = pd.to_numeric(data[col], errors="coerce")
    return data.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def _swings(data):
    highs, lows = [], []
    start = max(SWING_LEFT, len(data) - STRUCTURE_LOOKBACK)
    end = len(data) - SWING_RIGHT
    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        if h >= float(data.iloc[i-SWING_LEFT:i]["high"].max()) and h >= float(data.iloc[i+1:i+1+SWING_RIGHT]["high"].max()):
            highs.append((i, h))
        if l <= float(data.iloc[i-SWING_LEFT:i]["low"].min()) and l <= float(data.iloc[i+1:i+1+SWING_RIGHT]["low"].min()):
            lows.append((i, l))
    return highs, lows


def _structure(data):
    highs, lows = _swings(data)
    result = {
        "structure": "range",
        "last_swing_high": highs[-1][1] if highs else None,
        "previous_swing_high": highs[-2][1] if len(highs) >= 2 else None,
        "last_swing_low": lows[-1][1] if lows else None,
        "previous_swing_low": lows[-2][1] if len(lows) >= 2 else None,
    }
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
            result["structure"] = "bullish"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
            result["structure"] = "bearish"
    return result


def _pullback(data, direction):
    recent = data.iloc[-(PULLBACK_LOOKBACK + 1):-1]
    counter = 0
    for _, c in recent.iterrows():
        if direction == "bullish" and float(c.close) < float(c.open):
            counter += 1
        elif direction == "bearish" and float(c.close) > float(c.open):
            counter += 1
    return counter >= PULLBACK_MIN_COUNTER_CANDLES, counter


def _rejection(current, direction, support, resistance):
    """Clasifica la zona actual y aplica dirección obligatoria.

    SOPORTE -> únicamente CALL.
    RESISTENCIA -> únicamente PUT.
    Si el precio está cerca de soporte, PUT queda bloqueado aunque no haya
    una mecha de rechazo perfecta. Lo mismo para CALL en resistencia.
    """
    op = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])

    body = max(abs(close - op), 1e-12)
    rng = max(high - low, 1e-12)
    close_pos = (close - low) / rng

    support_wick = (min(op, close) - low) / body
    resistance_wick = (high - max(op, close)) / body

    near_support = (
        support is not None
        and low <= float(support) * (1.0 + SR_ZONE_TOLERANCE)
    )
    near_resistance = (
        resistance is not None
        and high >= float(resistance) * (1.0 - SR_ZONE_TOLERANCE)
    )

    support_rejection = bool(
        near_support
        and close > float(support)
        and support_wick >= MIN_WICK_BODY_RATIO
        and close_pos >= MIN_CLOSE_POSITION
    )

    resistance_rejection = bool(
        near_resistance
        and close < float(resistance)
        and resistance_wick >= MIN_WICK_BODY_RATIO
        and close_pos <= (1.0 - MIN_CLOSE_POSITION)
    )

    # Regla de seguridad principal: zona determina dirección.
    if near_support and not near_resistance:
        zone = "support"
        allowed_direction = "bullish"
        rejection_ok = direction == "bullish" and support_rejection
        reason = (
            "Rechazo de SOPORTE: solo CALL"
            if rejection_ok
            else "Entrada bloqueada: precio en SOPORTE, solo se permite CALL"
        )
        wick = support_wick

    elif near_resistance and not near_support:
        zone = "resistance"
        allowed_direction = "bearish"
        rejection_ok = direction == "bearish" and resistance_rejection
        reason = (
            "Rechazo de RESISTENCIA: solo PUT"
            if rejection_ok
            else "Entrada bloqueada: precio en RESISTENCIA, solo se permite PUT"
        )
        wick = resistance_wick

    elif near_support and near_resistance:
        zone = "ambiguous_sr"
        allowed_direction = None
        rejection_ok = False
        reason = "Entrada bloqueada: soporte y resistencia demasiado cercanos"
        wick = max(support_wick, resistance_wick)

    else:
        zone = None
        allowed_direction = None
        rejection_ok = False
        reason = "Entrada bloqueada: precio no está en una zona S/R válida"
        wick = 0.0

    return (
        rejection_ok, zone, wick, close_pos, allowed_direction,
        support_rejection, resistance_rejection, reason,
        near_support, near_resistance,
    )


def analyze_market(df: Optional[pd.DataFrame] = None, candle_1m: Any = None,
                   previous_m1: Optional[pd.DataFrame] = None, candle_5m: Any = None,
                   previous_m5: Optional[pd.DataFrame] = None, m1_block=None,
                   pair=None, **kwargs) -> Dict[str, Any]:
    if df is not None:
        base = df.copy()
    elif previous_m1 is not None:
        base = previous_m1.copy()
        if candle_1m is not None:
            base = pd.concat([base, pd.DataFrame([candle_1m])], ignore_index=True)
    elif previous_m5 is not None:
        base = previous_m5.copy()
        if candle_5m is not None:
            base = pd.concat([base, pd.DataFrame([candle_5m])], ignore_index=True)
    else:
        base = pd.DataFrame()

    data = _normalize(base)
    result = _empty_result()
    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"
        return result

    cur, prev = data.iloc[-1], data.iloc[-2]
    direction = "bullish" if cur.close > cur.open else "bearish" if cur.close < cur.open else "range"

    st = _structure(data)
    structure_ok = st["structure"] == direction
    pullback_ok, counter = _pullback(data, direction)

    support = st["last_swing_low"]
    resistance = st["last_swing_high"]
    (
        rejection_ok,
        zone,
        wick,
        close_pos,
        allowed_direction,
        support_rejection,
        resistance_rejection,
        rejection_reason,
        near_support,
        near_resistance,
    ) = _rejection(cur, direction, support, resistance)

    # REGLA DURA DE DIRECCIÓN POR ZONA:
    # soporte = CALL exclusivamente
    # resistencia = PUT exclusivamente
    zone_direction_ok = (
        allowed_direction is not None
        and direction == allowed_direction
    )

    momentum_ok = (
        (direction == "bullish" and cur.close > cur.open and cur.close > prev.high) or
        (direction == "bearish" and cur.close < cur.open and cur.close < prev.low)
    )

    entry_ok = (
        structure_ok
        and pullback_ok
        and rejection_ok
        and zone_direction_ok
        and momentum_ok
    )

    score = (25 if structure_ok else 0) + (20 if pullback_ok else 0) + (25 if rejection_ok else 0) + (30 if momentum_ok else 0)
    reasons = [
        "estructura HH/HL" if st["structure"] == "bullish" else
        "estructura LH/LL" if st["structure"] == "bearish" else "estructura sin dirección",
        "retroceso válido" if pullback_ok else "sin retroceso válido",
        "rechazo de soporte" if direction == "bullish" and rejection_ok else
        "rechazo de resistencia" if direction == "bearish" and rejection_ok else "sin rechazo S/R",
        "momentum rompe máximo anterior" if direction == "bullish" and momentum_ok else
        "momentum rompe mínimo anterior" if direction == "bearish" and momentum_ok else "sin momentum confirmado"
    ]

    result.update({
        "direction": direction, "trend": direction, "score": score,
        "entry_quality": score, "blocked": not entry_ok,
        "candle_timestamp": int(cur["from"]) if "from" in data.columns and pd.notna(cur["from"]) else None,
        "analysis": {
            "timeframe": "M1", "indicators_used": False,
            "structure": st["structure"], "structure_ok": structure_ok,
            "pullback_ok": pullback_ok, "counter_candles": counter,
            "support": support, "resistance": resistance,
            "rejection_ok": rejection_ok,
            "near_support": near_support,
            "near_resistance": near_resistance,
            "zone_direction_ok": zone_direction_ok,
            "allowed_direction": allowed_direction,
            "support_rejection": support_rejection,
            "resistance_rejection": resistance_rejection,
            "wick_body_ratio": wick,
            "close_position": close_pos,
            "rejection_reason": rejection_reason,
            "momentum_ok": momentum_ok,
            "previous_high": float(prev.high), "previous_low": float(prev.low),
            "reasons": reasons, "target_expiration_minutes": 2
        }
    })

    if entry_ok:
        signal = "call" if direction == "bullish" else "put"
        result.update({
            "signal": signal, "continuity": True, "blocked": False,
            "zone": zone, "entry_type": "PRICE_ACTION_STRUCTURE_PULLBACK_REJECTION_2M",
            "reason": ("CALL" if signal == "call" else "PUT") + " | " + " | ".join(reasons)
        })
    else:
        if near_support and not near_resistance and direction != "bullish":
            result["reason"] = "Entrada bloqueada: precio en SOPORTE = solo CALL"
        elif support_rejection and direction != "bullish":
            result["reason"] = "Entrada bloqueada: rechazo de SOPORTE = solo CALL"
        elif near_resistance and not near_support and direction != "bearish":
            result["reason"] = "Entrada bloqueada: precio en RESISTENCIA = solo PUT"
        elif resistance_rejection and direction != "bearish":
            result["reason"] = "Entrada bloqueada: rechazo de RESISTENCIA = solo PUT"
        elif support_rejection and resistance_rejection:
            result["reason"] = "Entrada bloqueada: rechazo S/R ambiguo"
        elif not structure_ok:
            result["reason"] = "Entrada bloqueada: estructura no coincide"
        elif not pullback_ok:
            result["reason"] = "Entrada bloqueada: no hay retroceso"
        elif not rejection_ok:
            result["reason"] = rejection_reason
        elif not zone_direction_ok:
            result["reason"] = "Entrada bloqueada: dirección incompatible con la zona"
        elif not momentum_ok:
            result["reason"] = "Entrada bloqueada: no hay momentum"

    return result


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
