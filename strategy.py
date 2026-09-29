"""strategy.py

Estrategia de ACCION DEL PRECIO para velas M5, sin indicadores.

La estrategia:
- Analiza exclusivamente velas M5 cerradas.
- Clasifica cada vela: indecision, continuidad, reversion, fuerza,
  descanso, momentum, doji, estrella fugaz, estrella de la tarde,
  pullback y rechazo.
- Usa estructura de precio, maximos/minimos, zonas S/R y secuencia de velas.
- No usa EMA, RSI, ATR ni ningun indicador tecnico.
- Devuelve UNA sola direccion: call, put o None.
- El bot ejecuta la senal al abrir la siguiente vela M1.
- Expiracion objetivo: 1 minuto.

Importante: clasificar un patron de vela no garantiza el resultado de una
operacion. El bot debe bloquear senales cuando la accion del precio sea
ambigua o contradiga la estructura.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 30
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 30
SR_LOOKBACK = 20
ZONE_TOLERANCE = 0.0015

DOJI_BODY_MAX = 0.10
INDECISION_BODY_MAX = 0.25
SMALL_BODY_MAX = 0.35
STRONG_BODY_MIN = 0.60
EXTREME_CLOSE_MIN = 0.70
WICK_BODY_MIN = 1.20

TARGET_EXPIRATION_MINUTES = 1


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": "none",
        "entry_type": "PRICE_ACTION_M5_NEXT_M1_1M",
        "entry_quality": 0,
        "candle_timestamp": None,
        "analysis": {},
    }


def _normalize(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    data = df.copy()
    rename = {}
    if "max" in data.columns and "high" not in data.columns:
        rename["max"] = "high"
    if "min" in data.columns and "low" not in data.columns:
        rename["min"] = "low"
    if rename:
        data = data.rename(columns=rename)
    required = ["open", "high", "low", "close"]
    if any(c not in data.columns for c in required):
        return pd.DataFrame()
    for c in required:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    if "from" in data.columns:
        data["from"] = pd.to_numeric(data["from"], errors="coerce")
        data = data.sort_values("from")
    data = data.dropna(subset=required).reset_index(drop=True)
    return data


def _metrics(row: pd.Series) -> Dict[str, float]:
    o, h, l, c = map(float, (row["open"], row["high"], row["low"], row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    close_pos = (c - l) / rng
    return {
        "open": o, "high": h, "low": l, "close": c,
        "range": rng, "body": body,
        "body_ratio": body / rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_body": upper / max(body, 1e-12),
        "lower_body": lower / max(body, 1e-12),
        "close_pos": close_pos,
    }


def _direction(row: pd.Series) -> str:
    if float(row["close"]) > float(row["open"]):
        return "bullish"
    if float(row["close"]) < float(row["open"]):
        return "bearish"
    return "neutral"


def _swings(data: pd.DataFrame) -> Tuple[list[Tuple[int, float]], list[Tuple[int, float]]]:
    highs: list[Tuple[int, float]] = []
    lows: list[Tuple[int, float]] = []
    start = max(SWING_LEFT, len(data) - SWING_LOOKBACK - SWING_RIGHT)
    end = len(data) - SWING_RIGHT
    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        left_h = data.iloc[i-SWING_LEFT:i]["high"].max()
        right_h = data.iloc[i+1:i+1+SWING_RIGHT]["high"].max()
        left_l = data.iloc[i-SWING_LEFT:i]["low"].min()
        right_l = data.iloc[i+1:i+1+SWING_RIGHT]["low"].min()
        if h >= float(left_h) and h >= float(right_h):
            highs.append((i, h))
        if l <= float(left_l) and l <= float(right_l):
            lows.append((i, l))
    return highs, lows


def _structure(data: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = _swings(data)
    out: Dict[str, Any] = {
        "structure": "range",
        "highs": highs,
        "lows": lows,
        "last_high": highs[-1][1] if highs else None,
        "previous_high": highs[-2][1] if len(highs) >= 2 else None,
        "last_low": lows[-1][1] if lows else None,
        "previous_low": lows[-2][1] if len(lows) >= 2 else None,
    }
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            out["structure"] = "bullish"
        elif lh and ll:
            out["structure"] = "bearish"
    return out


def _classify_candle(data: pd.DataFrame, idx: int) -> Dict[str, Any]:
    cur = _metrics(data.iloc[idx])
    prev = _metrics(data.iloc[idx - 1]) if idx > 0 else cur
    d = _direction(data.iloc[idx])
    prev_d = _direction(data.iloc[idx - 1]) if idx > 0 else "neutral"

    doji = cur["body_ratio"] <= DOJI_BODY_MAX
    indecision = cur["body_ratio"] <= INDECISION_BODY_MAX
    strength = cur["body_ratio"] >= STRONG_BODY_MIN and (
        cur["close_pos"] >= EXTREME_CLOSE_MIN or cur["close_pos"] <= 1 - EXTREME_CLOSE_MIN
    )
    continuation = d in ("bullish", "bearish") and d == prev_d and cur["body_ratio"] >= 0.35

    bullish_engulf = (
        d == "bullish" and prev_d == "bearish"
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
    )
    bearish_engulf = (
        d == "bearish" and prev_d == "bullish"
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )
    reversal = bullish_engulf or bearish_engulf

    rest = cur["body_ratio"] <= SMALL_BODY_MAX and not doji

    momentum_bull = (
        d == "bullish" and cur["body_ratio"] >= 0.55
        and cur["close_pos"] >= 0.70
        and cur["close"] > prev["high"]
    )
    momentum_bear = (
        d == "bearish" and cur["body_ratio"] >= 0.55
        and cur["close_pos"] <= 0.30
        and cur["close"] < prev["low"]
    )

    shooting_star = (
        cur["upper_body"] >= WICK_BODY_MIN
        and cur["upper_wick"] > cur["lower_wick"] * 1.5
        and cur["close_pos"] <= 0.55
    )
    evening_star = False
    if idx >= 2:
        a = _metrics(data.iloc[idx - 2])
        b = _metrics(data.iloc[idx - 1])
        evening_star = (
            _direction(data.iloc[idx - 2]) == "bullish"
            and a["body_ratio"] >= 0.55
            and b["body_ratio"] <= 0.35
            and d == "bearish"
            and cur["close"] < (a["open"] + a["close"]) / 2.0
        )

    return {
        "direction": d,
        "doji": doji,
        "indecision": indecision,
        "continuation": continuation,
        "reversal": reversal,
        "strength": strength,
        "rest": rest,
        "momentum_bull": momentum_bull,
        "momentum_bear": momentum_bear,
        "shooting_star": shooting_star,
        "evening_star": evening_star,
        "body_ratio": cur["body_ratio"],
        "close_pos": cur["close_pos"],
        "upper_body": cur["upper_body"],
        "lower_body": cur["lower_body"],
    }


def _pullback(data: pd.DataFrame, structure: str) -> Tuple[bool, int]:
    if len(data) < 5 or structure not in ("bullish", "bearish"):
        return False, 0
    recent = data.iloc[-5:-1]
    counter = 0
    for _, row in recent.iterrows():
        d = _direction(row)
        if structure == "bullish" and d == "bearish":
            counter += 1
        elif structure == "bearish" and d == "bullish":
            counter += 1
    return counter >= 1, counter


def _zones(data: pd.DataFrame, st: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    support = st.get("last_low")
    resistance = st.get("last_high")
    if support is None:
        support = float(data.iloc[-SR_LOOKBACK:]["low"].min())
    if resistance is None:
        resistance = float(data.iloc[-SR_LOOKBACK:]["high"].max())
    return support, resistance


def _rejection(cur: pd.Series, support: Optional[float], resistance: Optional[float]) -> Dict[str, Any]:
    m = _metrics(cur)
    near_support = support is not None and m["low"] <= support * (1 + ZONE_TOLERANCE) and m["close"] >= support
    near_resistance = resistance is not None and m["high"] >= resistance * (1 - ZONE_TOLERANCE) and m["close"] <= resistance
    bull_reject = near_support and m["lower_body"] >= WICK_BODY_MIN and m["close_pos"] >= 0.60
    bear_reject = near_resistance and m["upper_body"] >= WICK_BODY_MIN and m["close_pos"] <= 0.40
    if near_support and not near_resistance:
        zone = "support"
    elif near_resistance and not near_support:
        zone = "resistance"
    elif near_support and near_resistance:
        zone = "ambiguous"
    else:
        zone = "none"
    return {
        "zone": zone,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "bull_rejection": bull_reject,
        "bear_rejection": bear_reject,
    }


def _pattern_names(p: Dict[str, Any]) -> list[str]:
    names = []
    if p["doji"]:
        names.append("doji")
    elif p["indecision"]:
        names.append("indecision")
    if p["continuation"]:
        names.append("continuidad")
    if p["reversal"]:
        names.append("reversion")
    if p["strength"]:
        names.append("fuerza")
    if p["rest"]:
        names.append("descanso")
    if p["momentum_bull"] or p["momentum_bear"]:
        names.append("momentum")
    if p["shooting_star"]:
        names.append("estrella_fugaz")
    if p["evening_star"]:
        names.append("estrella_atardecer")
    return names


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_5m: Any = None,
    previous_m5: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    if df is not None:
        base = df.copy()
    elif previous_m5 is not None:
        base = previous_m5.copy()
        if candle_5m is not None:
            base = pd.concat([base, pd.DataFrame([candle_5m])], ignore_index=True)
    else:
        base = pd.DataFrame()

    data = _normalize(base)
    result = _empty()
    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M5 insuficiente {len(data)}/{MIN_BARS}"
        return result

    idx = len(data) - 1
    cur = data.iloc[idx]
    st = _structure(data)
    structure = st["structure"]
    p = _classify_candle(data, idx)
    pullback_ok, counter = _pullback(data, structure)
    support, resistance = _zones(data, st)
    rej = _rejection(cur, support, resistance)

    direction = p["direction"]
    patterns = _pattern_names(p)

    # La senal debe venir de una estructura clara y de accion del precio.
    bullish_context = structure == "bullish"
    bearish_context = structure == "bearish"

    # Pullback: se acepta retroceso antes de la vela de confirmacion.
    bull_pullback = bullish_context and pullback_ok
    bear_pullback = bearish_context and pullback_ok

    # Dos familias de entrada:
    # 1) Rechazo en zona + vela de confirmacion.
    # 2) Momentum/continuacion despues de pullback, sin perseguir una vela extrema.
    call_rejection = bullish_context and direction == "bullish" and rej["bull_rejection"]
    put_rejection = bearish_context and direction == "bearish" and rej["bear_rejection"]
    call_momentum = bullish_context and direction == "bullish" and p["momentum_bull"] and bull_pullback
    put_momentum = bearish_context and direction == "bearish" and p["momentum_bear"] and bear_pullback

    # Prohibiciones duras de zona.
    if rej["zone"] == "support" and direction == "bearish":
        return _blocked_result(data, st, p, rej, patterns, counter, "PUT bloqueada: precio en SOPORTE")
    if rej["zone"] == "resistance" and direction == "bullish":
        return _blocked_result(data, st, p, rej, patterns, counter, "CALL bloqueada: precio en RESISTENCIA")
    if rej["zone"] == "ambiguous":
        return _blocked_result(data, st, p, rej, patterns, counter, "Entrada bloqueada: S/R ambiguos")

    call_ok = call_rejection or call_momentum
    put_ok = put_rejection or put_momentum

    # No se opera doji/indecision puro, descanso puro ni patron de agotamiento
    # sin confirmacion estructural posterior.
    if p["doji"] or p["indecision"]:
        call_ok = False
        put_ok = False
    if p["shooting_star"] and not call_rejection:
        call_ok = False
    if p["evening_star"] and not put_rejection:
        put_ok = False

    if call_ok == put_ok:
        reason = "sin señal: accion del precio ambigua"
        if call_ok and put_ok:
            reason = "sin señal: CALL y PUT simultaneamente posibles"
        return _blocked_result(data, st, p, rej, patterns, counter, reason)

    signal = "call" if call_ok else "put"
    score = 0
    score += 25 if structure in ("bullish", "bearish") else 0
    score += 15 if pullback_ok else 0
    score += 25 if (call_rejection or put_rejection) else 0
    score += 20 if (call_momentum or put_momentum) else 0
    score += 10 if p["reversal"] or p["continuation"] else 0
    score += 5 if p["strength"] else 0
    score = min(100, score)

    reasons = [
        f"estructura {structure}",
        f"patrones: {', '.join(patterns) if patterns else 'vela normal'}",
        "pullback detectado" if pullback_ok else "sin pullback claro",
        "rechazo confirmado" if (call_rejection or put_rejection) else "sin rechazo directo",
        "momentum confirmado" if (call_momentum or put_momentum) else "sin ruptura de momentum",
    ]

    result.update({
        "signal": signal,
        "direction": direction,
        "trend": structure,
        "reason": ("CALL" if signal == "call" else "PUT") + " | " + " | ".join(reasons),
        "score": score,
        "continuity": True,
        "blocked": False,
        "zone": rej["zone"],
        "entry_type": "PRICE_ACTION_M5_NEXT_M1_1M",
        "entry_quality": score,
        "candle_timestamp": int(cur["from"]) if "from" in data.columns and pd.notna(cur["from"]) else None,
        "analysis": {
            "timeframe": "M5",
            "indicators_used": False,
            "structure": structure,
            "last_high": st.get("last_high"),
            "previous_high": st.get("previous_high"),
            "last_low": st.get("last_low"),
            "previous_low": st.get("previous_low"),
            "support": support,
            "resistance": resistance,
            "zone": rej["zone"],
            "near_support": rej["near_support"],
            "near_resistance": rej["near_resistance"],
            "support_rejection": rej["bull_rejection"],
            "resistance_rejection": rej["bear_rejection"],
            "pullback": pullback_ok,
            "counter_candles": counter,
            "patterns": patterns,
            "candle_classification": p,
            "reversal": p["reversal"],
            "continuation": p["continuation"],
            "strength": p["strength"],
            "rest": p["rest"],
            "call_rejection": call_rejection,
            "put_rejection": put_rejection,
            "call_momentum": call_momentum,
            "put_momentum": put_momentum,
            "target_expiration_minutes": 1,
        },
    })
    return result


def _blocked_result(data, st, p, rej, patterns, counter, reason):
    cur = data.iloc[-1]
    r = _empty(reason)
    r.update({
        "direction": p["direction"],
        "trend": st["structure"],
        "zone": rej["zone"],
        "candle_timestamp": int(cur["from"]) if "from" in data.columns and pd.notna(cur["from"]) else None,
        "analysis": {
            "timeframe": "M5",
            "indicators_used": False,
            "structure": st["structure"],
            "support": st.get("last_low"),
            "resistance": st.get("last_high"),
            "zone": rej["zone"],
            "patterns": patterns,
            "candle_classification": p,
            "reversal": p["reversal"],
            "continuation": p["continuation"],
            "strength": p["strength"],
            "rest": p["rest"],
            "pullback": bool(counter),
            "counter_candles": counter,
            "target_expiration_minutes": 1,
        },
    })
    return r


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
