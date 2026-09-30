from __future__ import annotations

"""SNIPER OTC - estrategia de confluencia M1.

Nucleo de señal:
    CALL = Choppiness 14 cruza ARRIBA 61.8 + vela de cruce ROJA.
    PUT  = Choppiness 14 cruza ABAJO 38.2 + vela de cruce VERDE.

La mejora V3 exige que el cruce esté a favor de la estructura y que la vela
que produce el cruce muestre una acción del precio compatible con un pullback /
rechazo antes de la continuación.

No usa EMA, RSI, MACD, Bollinger, ATR, volumen ni soporte/resistencia como
indicadores de entrada. La estructura se calcula exclusivamente con máximos,
mínimos y velas OHLC.
"""

from typing import Any, Dict, Optional
import math
import os
import pandas as pd

MIN_BARS = 40
CI_PERIOD = 14
OVERBOUGHT = 61.8
OVERSOLD = 38.2

# Calidad mínima del cruce CI.
MIN_CI_CROSS_DELTA = float(os.getenv("MIN_CI_CROSS_DELTA", "0.75"))
MIN_CI_PENETRATION = float(os.getenv("MIN_CI_PENETRATION", "0.35"))

# Acción del precio de la vela de cruce.
MIN_CANDLE_BODY_RATIO = float(os.getenv("MIN_CANDLE_BODY_RATIO", "0.28"))
MIN_REJECTION_WICK_RATIO = float(os.getenv("MIN_REJECTION_WICK_RATIO", "0.85"))
MIN_CLOSE_POSITION_CALL = float(os.getenv("MIN_CLOSE_POSITION_CALL", "0.55"))
MAX_CLOSE_POSITION_PUT = float(os.getenv("MAX_CLOSE_POSITION_PUT", "0.45"))

# Estructura / impulso.
SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "2"))
STRUCTURE_BARS = int(os.getenv("STRUCTURE_BARS", "24"))
IMPULSE_BARS = int(os.getenv("IMPULSE_BARS", "4"))
MIN_IMPULSE_ALIGNED = int(os.getenv("MIN_IMPULSE_ALIGNED", "2"))
MIN_IMPULSE_BODY_RATIO = float(os.getenv("MIN_IMPULSE_BODY_RATIO", "0.45"))

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
}


def _empty(reason: str = "sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": "M1_M1",
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
        d = d.drop_duplicates("from")

    return d.dropna(subset=required).reset_index(drop=True)


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close = data["close"].astype(float)
    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    tr_sum = true_range.rolling(period, min_periods=period).sum()
    highest_high = high.rolling(period, min_periods=period).max()
    lowest_low = low.rolling(period, min_periods=period).min()
    price_range = highest_high - lowest_low

    result = pd.Series(float("nan"), index=data.index, dtype=float)
    valid = (
        tr_sum.notna()
        & price_range.notna()
        & (price_range > 0)
        & (tr_sum > 0)
    )
    denominator = math.log10(period)
    result.loc[valid] = (
        100.0
        * (tr_sum.loc[valid] / price_range.loc[valid]).map(math.log10)
        / denominator
    )
    return result


def _candle(row: pd.Series) -> Dict[str, float | str | bool]:
    op = float(row["open"])
    cl = float(row["close"])
    hi = float(row["high"])
    lo = float(row["low"])
    rng = max(hi - lo, 0.0)
    body = abs(cl - op)

    if rng <= 0:
        return {
            "open": op, "close": cl, "high": hi, "low": lo,
            "range": 0.0, "body": body, "body_ratio": 0.0,
            "upper_wick": 0.0, "lower_wick": 0.0,
            "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0,
            "close_position": 0.5, "color": "neutral",
        }

    upper = max(hi - max(op, cl), 0.0)
    lower = max(min(op, cl) - lo, 0.0)
    return {
        "open": op,
        "close": cl,
        "high": hi,
        "low": lo,
        "range": rng,
        "body": body,
        "body_ratio": body / rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "upper_wick_ratio": upper / rng,
        "lower_wick_ratio": lower / rng,
        "close_position": (cl - lo) / rng,
        "color": "green" if cl > op else "red" if cl < op else "neutral",
    }


def _pivot_points(data: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Pivots OHLC simples; no usa ningún indicador."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    if len(data) < (2 * lookback + 1):
        return highs, lows

    start = lookback
    end = len(data) - lookback
    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])
        left_h = data.iloc[i - lookback:i]["high"].astype(float)
        right_h = data.iloc[i + 1:i + lookback + 1]["high"].astype(float)
        left_l = data.iloc[i - lookback:i]["low"].astype(float)
        right_l = data.iloc[i + 1:i + lookback + 1]["low"].astype(float)

        if h >= float(left_h.max()) and h >= float(right_h.max()):
            highs.append((i, h))
        if l <= float(left_l.min()) and l <= float(right_l.min()):
            lows.append((i, l))
    return highs, lows


def _structure(data: pd.DataFrame) -> Dict[str, Any]:
    """Estructura por HH/HL o LH/LL, sin soporte/resistencia."""
    if len(data) < max(12, 2 * SWING_LOOKBACK + 5):
        return {"structure": "range", "reason": "historial insuficiente para estructura"}

    window = data.iloc[-STRUCTURE_BARS:].reset_index(drop=True)
    highs, lows = _pivot_points(window)

    if len(highs) < 2 or len(lows) < 2:
        # Fallback muy conservador usando las dos mitades del tramo reciente.
        half = max(4, len(window) // 2)
        a = window.iloc[:half]
        b = window.iloc[-half:]
        a_hi, b_hi = float(a["high"].max()), float(b["high"].max())
        a_lo, b_lo = float(a["low"].min()), float(b["low"].min())
        if b_hi > a_hi and b_lo > a_lo:
            return {"structure": "bullish", "method": "swing_fallback", "higher_high": True, "higher_low": True}
        if b_hi < a_hi and b_lo < a_lo:
            return {"structure": "bearish", "method": "swing_fallback", "lower_high": True, "lower_low": True}
        return {"structure": "range", "method": "swing_fallback"}

    h1_i, h1 = highs[-2]
    h2_i, h2 = highs[-1]
    l1_i, l1 = lows[-2]
    l2_i, l2 = lows[-1]

    bullish = h2 > h1 and l2 > l1
    bearish = h2 < h1 and l2 < l1

    return {
        "structure": "bullish" if bullish else "bearish" if bearish else "range",
        "method": "HH_HL_LH_LL",
        "higher_high": h2 > h1,
        "higher_low": l2 > l1,
        "lower_high": h2 < h1,
        "lower_low": l2 < l1,
        "previous_swing_high": h1,
        "latest_swing_high": h2,
        "previous_swing_low": l1,
        "latest_swing_low": l2,
        "high_indices": [h1_i, h2_i],
        "low_indices": [l1_i, l2_i],
    }


def _impulse_confirmation(data: pd.DataFrame, structure: str) -> Dict[str, Any]:
    """Busca impulso previo y no cuenta la vela que produce el cruce."""
    if len(data) < IMPULSE_BARS + 2 or structure not in ("bullish", "bearish"):
        return {"ok": False, "aligned_count": 0, "net_aligned": False, "strong_count": 0}

    # La última vela es la vela del cruce. Las anteriores forman el contexto.
    recent = data.iloc[-(IMPULSE_BARS + 1):-1]
    if len(recent) < IMPULSE_BARS:
        return {"ok": False, "aligned_count": 0, "net_aligned": False, "strong_count": 0}

    dirs = []
    strong_count = 0
    for _, row in recent.iterrows():
        c = _candle(row)
        if c["color"] == "green":
            dirs.append("bullish")
        elif c["color"] == "red":
            dirs.append("bearish")
        else:
            dirs.append("neutral")
        if float(c["body_ratio"]) >= MIN_IMPULSE_BODY_RATIO:
            strong_count += 1

    aligned_count = sum(1 for x in dirs if x == structure)
    first_close = float(recent.iloc[0]["close"])
    last_close = float(recent.iloc[-1]["close"])
    net_move = last_close - first_close
    net_aligned = net_move > 0 if structure == "bullish" else net_move < 0

    return {
        "ok": aligned_count >= MIN_IMPULSE_ALIGNED and net_aligned,
        "aligned_count": aligned_count,
        "required_aligned": MIN_IMPULSE_ALIGNED,
        "strong_count": strong_count,
        "net_move": net_move,
        "net_aligned": net_aligned,
        "directions": dirs,
    }


def _price_action_confirmation(data: pd.DataFrame, signal: str, structure: str) -> Dict[str, Any]:
    """Confirma pullback/rechazo en la vela que produce el cruce."""
    if len(data) < 3:
        return {"ok": False, "reason": "sin contexto de acción del precio"}

    cur = _candle(data.iloc[-1])
    prev = _candle(data.iloc[-2])
    prev2 = _candle(data.iloc[-3])

    if signal == "call" and structure == "bullish":
        # La vela de cruce es roja: debe ser retroceso, no una venta impulsiva.
        color_ok = cur["color"] == "red"
        body_ok = float(cur["body_ratio"]) >= MIN_CANDLE_BODY_RATIO
        rejection_ok = (
            float(cur["lower_wick_ratio"]) >= MIN_REJECTION_WICK_RATIO * max(float(cur["upper_wick_ratio"]), 0.08)
            and float(cur["close_position"]) >= MIN_CLOSE_POSITION_CALL
        )
        # Preferimos un barrido del mínimo previo con recuperación, o una mecha inferior clara.
        sweep = float(cur["low"]) < float(prev["low"]) and float(cur["close"]) > float(prev["low"])
        rejection = float(cur["lower_wick_ratio"]) >= 0.18 and float(cur["close_position"]) >= 0.55
        not_impulsive_against = not (
            float(cur["body_ratio"]) >= 0.70 and float(cur["close_position"]) <= 0.30
        )
        # El contexto inmediato debe mostrar que el retroceso llega después de una vela alcista o neutral.
        context_ok = prev2["color"] in ("green", "neutral") or prev["close"] >= prev2["close"]
        ok = color_ok and body_ok and rejection_ok and (sweep or rejection) and not_impulsive_against and context_ok
        return {
            "ok": ok,
            "type": "pullback_rejection",
            "color_ok": color_ok,
            "body_ok": body_ok,
            "rejection_ok": rejection_ok,
            "sweep": sweep,
            "rejection": rejection,
            "context_ok": context_ok,
            "not_impulsive_against": not_impulsive_against,
            "candle": cur,
        }

    if signal == "put" and structure == "bearish":
        color_ok = cur["color"] == "green"
        body_ok = float(cur["body_ratio"]) >= MIN_CANDLE_BODY_RATIO
        rejection_ok = (
            float(cur["upper_wick_ratio"]) >= MIN_REJECTION_WICK_RATIO * max(float(cur["lower_wick_ratio"]), 0.08)
            and float(cur["close_position"]) <= MAX_CLOSE_POSITION_PUT
        )
        sweep = float(cur["high"]) > float(prev["high"]) and float(cur["close"]) < float(prev["high"])
        rejection = float(cur["upper_wick_ratio"]) >= 0.18 and float(cur["close_position"]) <= 0.45
        not_impulsive_against = not (
            float(cur["body_ratio"]) >= 0.70 and float(cur["close_position"]) >= 0.70
        )
        context_ok = prev2["color"] in ("red", "neutral") or prev["close"] <= prev2["close"]
        ok = color_ok and body_ok and rejection_ok and (sweep or rejection) and not_impulsive_against and context_ok
        return {
            "ok": ok,
            "type": "pullback_rejection",
            "color_ok": color_ok,
            "body_ok": body_ok,
            "rejection_ok": rejection_ok,
            "sweep": sweep,
            "rejection": rejection,
            "context_ok": context_ok,
            "not_impulsive_against": not_impulsive_against,
            "candle": cur,
        }

    return {"ok": False, "reason": "señal contra estructura"}


def _cross(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if len(data) < MIN_BARS:
        return None
    ci = _choppiness_index(data)
    cur_i = len(data) - 1
    prev_i = cur_i - 1
    prev_ci = ci.iloc[prev_i]
    curr_ci = ci.iloc[cur_i]
    if pd.isna(prev_ci) or pd.isna(curr_ci):
        return None

    cur = _candle(data.iloc[cur_i])
    delta = float(curr_ci) - float(prev_ci)

    if float(prev_ci) <= OVERBOUGHT and float(curr_ci) > OVERBOUGHT and cur["color"] == "red":
        penetration = float(curr_ci) - OVERBOUGHT
        if delta >= MIN_CI_CROSS_DELTA and penetration >= MIN_CI_PENETRATION:
            return {
                "signal": "call",
                "ci_previous": float(prev_ci),
                "ci_current": float(curr_ci),
                "ci_delta": delta,
                "ci_penetration": penetration,
                "threshold": OVERBOUGHT,
                "candle": cur,
            }

    if float(prev_ci) >= OVERSOLD and float(curr_ci) < OVERSOLD and cur["color"] == "green":
        penetration = OVERSOLD - float(curr_ci)
        if (-delta) >= MIN_CI_CROSS_DELTA and penetration >= MIN_CI_PENETRATION:
            return {
                "signal": "put",
                "ci_previous": float(prev_ci),
                "ci_current": float(curr_ci),
                "ci_delta": delta,
                "ci_penetration": penetration,
                "threshold": OVERSOLD,
                "candle": cur,
            }
    return None


def _higher_structure(higher_tf_df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if higher_tf_df is None or not isinstance(higher_tf_df, pd.DataFrame) or higher_tf_df.empty:
        return {"structure": "range", "reason": "M5 no disponible"}
    h = _normalize(higher_tf_df)
    if len(h) < MIN_BARS:
        return {"structure": "range", "reason": f"M5 insuficiente {len(h)}/{MIN_BARS}"}
    return _structure(h)


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
        return _empty(f"historial M1 insuficiente {len(data)}/{MIN_BARS}")

    cross = _cross(data)
    if cross is None:
        return _empty("sin cruce CI 61.8/38.2 + color confirmado")

    local_st = _structure(data)
    structure = local_st.get("structure", "range")
    signal = cross["signal"]
    expected_structure = "bullish" if signal == "call" else "bearish"

    if structure != expected_structure:
        return _empty(
            f"{signal.upper()} bloqueado: estructura M1={structure}, requiere={expected_structure}"
        )

    higher_st = _higher_structure(higher_tf_df)
    higher_structure = higher_st.get("structure", "range")
    if higher_structure != expected_structure:
        return _empty(
            f"{signal.upper()} bloqueado: estructura M5={higher_structure}, requiere={expected_structure}"
        )

    impulse = _impulse_confirmation(data, structure)
    if not impulse["ok"]:
        return _empty(
            f"{signal.upper()} bloqueado: sin impulso previo suficiente a favor de estructura"
        )

    price_action = _price_action_confirmation(data, signal, structure)
    if not price_action.get("ok"):
        return _empty(
            f"{signal.upper()} bloqueado: acción del precio sin pullback/rechazo válido"
        )

    # Confluencias que deben estar presentes, no un score que sustituya reglas.
    confirmations = [
        "cruce_ci",
        "estructura_m1",
        "estructura_m5",
        "impulso_previo",
        "pullback_rechazo",
    ]
    score = 100
    sweep = bool(price_action.get("sweep"))
    if not sweep:
        # No bloquea: rechazo claro es suficiente, pero queda reflejado.
        score = 95

    candle = cross["candle"]
    candle_color = "roja" if signal == "call" else "verde"
    direction_text = "alcista" if signal == "call" else "bajista"
    ts = int(data.iloc[-1]["from"]) if "from" in data.columns and pd.notna(data.iloc[-1]["from"]) else None

    reason = (
        f"{signal.upper()} | estructura M1 {structure} + M5 {higher_structure} | "
        f"CI {cross['ci_previous']:.2f}->{cross['ci_current']:.2f} cruza {cross['threshold']:.1f} "
        f"| delta {cross['ci_delta']:+.2f} | penetracion {cross['ci_penetration']:.2f} "
        f"| vela {candle_color} | cuerpo {float(candle['body_ratio']):.0%} "
        f"| pullback/rechazo {'barrido' if sweep else 'mecha'} "
        f"| impulso previo {impulse['aligned_count']}/{IMPULSE_BARS} a favor "
        f"| direccion {direction_text} | siguiente M1"
    )

    return {
        "signal": signal,
        "direction": expected_structure,
        "trend": structure,
        "higher_trend": higher_structure,
        "reason": reason,
        "score": score,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "candle_timestamp": ts,
        "analysis": {
            "ci": {
                "period": CI_PERIOD,
                "previous": cross["ci_previous"],
                "current": cross["ci_current"],
                "delta": cross["ci_delta"],
                "threshold": cross["threshold"],
                "penetration": cross["ci_penetration"],
            },
            "structure_m1": local_st,
            "structure_m5": higher_st,
            "impulse": impulse,
            "price_action": price_action,
            "confirmations": confirmations,
            "pair": pair,
        },
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
