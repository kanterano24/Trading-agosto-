"""strategy.py - MOMENTUM M1 con filtro de ubicacion de entrada.

Analiza exclusivamente velas M1 cerradas. Mantiene la logica de momentum
pero agrega un primer filtro de ubicacion para evitar entradas tardias,
extendidas o demasiado cerca de una zona extrema reciente.

La vela N cerrada genera una señal para ejecutar en N+1.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import pandas as pd

MIN_BARS = 35
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 5

# Momentum original
MIN_BODY_RATIO = 0.55
MIN_BODY_ATR = 0.35
MAX_BODY_ATR = 4.0
MIN_CLOSE_POSITION = 0.75
MIN_RANGE_ATR = 0.70
MIN_MOMENTUM_SCORE = 70
MAX_CONSECUTIVE = 5

# Paso 1: ubicacion de la entrada.
# Se excluyen las ultimas BREAKOUT_LOOKBACK velas al buscar una zona
# mayor, para no confundir el breakout inmediato con la resistencia/soporte
# que puede frenar la siguiente vela.
LOCATION_LOOKBACK = 20
LOCATION_EXCLUDE_RECENT = BREAKOUT_LOOKBACK
MIN_ROOM_ATR = 0.50
MAX_ENTRY_EXTENSION_ATR = 1.75
MAX_LATE_CONSECUTIVE = 3
EPS = 1e-12


def _empty_result(reason="sin señal") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "reason": reason,
        "score": 0,
        "continuity": False,
        "blocked": True,
        "zone": "momentum",
        "entry_type": "momentum",
        "entry_quality": 0,
        "rsi": 50.0,
        "atr": 0.0,
        "candle_timestamp": None,
        "analysis": {},
    }


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "max" in out.columns and "high" not in out.columns:
        out = out.rename(columns={"max": "high"})
    if "min" in out.columns and "low" not in out.columns:
        out = out.rename(columns={"min": "low"})

    required = ["open", "high", "low", "close"]
    if any(c not in out.columns for c in required):
        return pd.DataFrame()

    for c in required:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")

    sort_col = "from" if "from" in out.columns else required[0]
    out = (
        out.dropna(subset=required)
        .sort_values(sort_col)
        .reset_index(drop=True)
    )
    return out


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    rolling = tr.rolling(period).mean()
    if len(df) < period or pd.isna(rolling.iloc[-1]):
        return 0.0
    return float(rolling.iloc[-1])


def _rsi(s: pd.Series, period: int = 14) -> float:
    d = s.diff()
    gain = d.clip(lower=0).rolling(period).mean()
    loss = (-d.clip(upper=0)).rolling(period).mean()
    if pd.isna(gain.iloc[-1]) or pd.isna(loss.iloc[-1]):
        return 50.0
    if loss.iloc[-1] == 0:
        return 100.0 if gain.iloc[-1] > 0 else 50.0
    return float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1]))


def _metrics(c: pd.Series):
    rng = max(float(c.high - c.low), EPS)
    body = abs(float(c.close - c.open))
    return body, rng, body / rng, float(c.close - c.low) / rng


def _location_reference(hist: pd.DataFrame) -> pd.DataFrame:
    """Devuelve la zona mayor anterior al breakout inmediato."""
    if hist.empty:
        return hist
    if len(hist) > LOCATION_EXCLUDE_RECENT:
        base = hist.iloc[:-LOCATION_EXCLUDE_RECENT]
    else:
        base = hist.iloc[:0]
    return base.tail(LOCATION_LOOKBACK)


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    candle_1m: Any = None,
    previous_m1: Optional[pd.DataFrame] = None,
    candle_5m: Any = None,
    previous_m5: Optional[pd.DataFrame] = None,
    m1_block=None,
    pair=None,
    **kwargs,
):
    if df is None:
        if previous_m1 is not None:
            base = previous_m1.copy()
            if candle_1m is not None:
                base = pd.concat(
                    [base, pd.DataFrame([candle_1m])], ignore_index=True
                )
        elif previous_m5 is not None:
            base = previous_m5.copy()
            if candle_5m is not None:
                base = pd.concat(
                    [base, pd.DataFrame([candle_5m])], ignore_index=True
                )
        else:
            base = pd.DataFrame()
    else:
        base = df.copy()

    data = _normalize(base)
    result = _empty_result()
    if len(data) < MIN_BARS:
        result["reason"] = f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"
        return result

    current = data.iloc[-1]
    hist = data.iloc[:-1]
    atr = _atr(data)
    rsi = _rsi(data["close"])
    if atr <= 0:
        result["reason"] = "ATR M1 inválido"
        return result

    body, rng, body_ratio, close_pos = _metrics(current)
    direction = (
        "bullish"
        if current.close > current.open
        else "bearish"
        if current.close < current.open
        else "range"
    )

    ema9_series = _ema(data.close, EMA_FAST)
    ema21_series = _ema(data.close, EMA_MID)
    ema50_series = _ema(data.close, EMA_SLOW)
    ema9 = ema9_series.iloc[-1]
    ema21 = ema21_series.iloc[-1]
    ema50 = ema50_series.iloc[-1]
    ema9p = ema9_series.iloc[-4]
    ema21p = ema21_series.iloc[-4]

    recent = hist.tail(BREAKOUT_LOOKBACK)
    prev_high = float(recent.high.max())
    prev_low = float(recent.low.min())
    bullish_break = float(current.close) > prev_high
    bearish_break = float(current.close) < prev_low

    body_atr = body / atr
    range_atr = rng / atr

    trend_bull = (
        ema9 > ema21 > ema50
        and ema9 > ema9p
        and ema21 > ema21p
        and current.close > ema21
    )
    trend_bear = (
        ema9 < ema21 < ema50
        and ema9 < ema9p
        and ema21 < ema21p
        and current.close < ema21
    )

    dirs = [
        "bull" if r.close > r.open else "bear" if r.close < r.open else "neutral"
        for _, r in data.tail(MAX_CONSECUTIVE + 1).iterrows()
    ]
    consecutive = 0
    wanted = "bull" if direction == "bullish" else "bear"
    if direction != "range":
        for d in reversed(dirs):
            if d == wanted:
                consecutive += 1
            else:
                break

    # ------------------------------------------------------------
    # PASO 1: UBICACION
    # ------------------------------------------------------------
    location_ref = _location_reference(hist)
    if not location_ref.empty:
        major_high = float(location_ref.high.max())
        major_low = float(location_ref.low.min())
    else:
        major_high = float(prev_high)
        major_low = float(prev_low)

    if direction == "bullish":
        entry_extension_atr = (float(current.close) - float(ema21)) / atr
        room_to_resistance = (major_high - float(current.close)) / atr
    elif direction == "bearish":
        entry_extension_atr = (float(ema21) - float(current.close)) / atr
        room_to_support = (float(current.close) - major_low) / atr
    else:
        room_to_resistance = 0.0
        room_to_support = 0.0
        entry_extension_atr = 0.0

    # Si el precio ya supero la zona mayor, esa zona deja de ser
    # "resistencia/soporte por delante". En ese caso el filtro usa el
    # control de extension ATR para evitar comprar/vender demasiado tarde.
    if direction == "bullish":
        room_atr = max(0.0, room_to_resistance)
        if float(current.close) > major_high:
            room_atr = float("inf")
    elif direction == "bearish":
        room_atr = max(0.0, room_to_support)
        if float(current.close) < major_low:
            room_atr = float("inf")
    else:
        room_atr = 0.0

    # Una entrada es "tardia" cuando ya hay demasiadas velas consecutivas
    # en la misma direccion. No se cambia el MAX_CONSECUTIVE original;
    # este es un filtro adicional exclusivamente para la ubicacion.
    late_entry = consecutive > MAX_LATE_CONSECUTIVE
    too_extended = entry_extension_atr > MAX_ENTRY_EXTENSION_ATR
    too_close_to_zone = room_atr < MIN_ROOM_ATR

    location_ok = not (late_entry or too_extended or too_close_to_zone)

    score = 0
    reasons = []

    if direction == "bullish":
        checks = [
            bullish_break,
            body_ratio >= MIN_BODY_RATIO,
            body_atr >= MIN_BODY_ATR,
            range_atr >= MIN_RANGE_ATR,
            close_pos >= MIN_CLOSE_POSITION,
            trend_bull,
            consecutive <= MAX_CONSECUTIVE,
        ]
        score = sum(10 for x in checks if x)
        if bullish_break:
            reasons.append("rompe máximo de las últimas M1")
        if body_ratio >= MIN_BODY_RATIO:
            reasons.append("cuerpo fuerte")
        if body_atr >= MIN_BODY_ATR:
            reasons.append("cuerpo con fuerza ATR")
        if close_pos >= MIN_CLOSE_POSITION:
            reasons.append("cierre cerca del máximo")
        if trend_bull:
            reasons.append("EMA 9/21/50 alineadas al alza")
        valid = all(checks[:6]) and consecutive <= MAX_CONSECUTIVE
    elif direction == "bearish":
        close_pos_bear = 1 - close_pos
        checks = [
            bearish_break,
            body_ratio >= MIN_BODY_RATIO,
            body_atr >= MIN_BODY_ATR,
            range_atr >= MIN_RANGE_ATR,
            close_pos_bear >= MIN_CLOSE_POSITION,
            trend_bear,
            consecutive <= MAX_CONSECUTIVE,
        ]
        score = sum(10 for x in checks if x)
        if bearish_break:
            reasons.append("rompe mínimo de las últimas M1")
        if body_ratio >= MIN_BODY_RATIO:
            reasons.append("cuerpo fuerte")
        if body_atr >= MIN_BODY_ATR:
            reasons.append("cuerpo con fuerza ATR")
        if close_pos_bear >= MIN_CLOSE_POSITION:
            reasons.append("cierre cerca del mínimo")
        if trend_bear:
            reasons.append("EMA 9/21/50 alineadas a la baja")
        valid = all(checks[:6]) and consecutive <= MAX_CONSECUTIVE
    else:
        valid = False

    # Bonus de calidad del desplazamiento, sin permitir que la fuerza
    # por si sola convierta una mala ubicacion en 100/100.
    score += 10 if range_atr >= 1.0 else 0
    score += 10 if body_ratio >= 0.70 else 0
    score = min(100, score)

    # Penalizaciones suaves para mostrar calidad real de ubicacion.
    location_penalty = 0
    if late_entry:
        location_penalty += 10
        reasons.append("entrada tardía: demasiadas velas consecutivas")
    if too_extended:
        location_penalty += 10
        reasons.append("precio demasiado extendido respecto a EMA 21")
    if too_close_to_zone:
        location_penalty += 10
        reasons.append("poco espacio hasta zona extrema previa")

    score = max(0, min(100, score - location_penalty))

    # Hasta aqui el score mide momentum. Solo una ubicacion limpia puede
    # recuperar el ultimo 10%: asi 100/100 exige fuerza + ubicacion.
    location_bonus = 0
    if location_ok:
        if room_atr >= 1.0 and entry_extension_atr <= 1.0 and consecutive <= 2:
            location_bonus = 10
        else:
            location_bonus = 5
    score = min(100, score + location_bonus)

    ts = int(current["from"]) if "from" in data.columns and pd.notna(current["from"]) else None
    analysis = {
        "timeframe": "M1",
        "momentum": True,
        "location_filter": True,
        "breakout_high": prev_high,
        "breakout_low": prev_low,
        "major_resistance": major_high,
        "major_support": major_low,
        "room_to_zone_atr": float(room_atr),
        "entry_extension_atr": float(entry_extension_atr),
        "late_entry": late_entry,
        "too_extended": too_extended,
        "too_close_to_zone": too_close_to_zone,
        "location_ok": location_ok,
        "location_penalty": location_penalty,
        "location_bonus": location_bonus,
        "body_ratio": body_ratio,
        "body_atr": body_atr,
        "range_atr": range_atr,
        "close_position": close_pos,
        "consecutive": consecutive,
        "ema9": float(ema9),
        "ema21": float(ema21),
        "ema50": float(ema50),
        "reasons": reasons,
    }

    result.update(
        {
            "direction": direction,
            "trend": direction,
            "rsi": rsi,
            "atr": atr,
            "candle_timestamp": ts,
            "analysis": analysis,
        }
    )

    if not valid:
        result["reason"] = "Momentum M1 insuficiente"
        return result

    # Paso 1: si la ubicacion es mala, NO se convierte en senal.
    if not location_ok:
        result["reason"] = "Entrada bloqueada por ubicación: " + "; ".join(reasons)
        result["score"] = int(score)
        result["entry_quality"] = int(score)
        result["blocked"] = True
        return result

    if score < MIN_MOMENTUM_SCORE:
        result["reason"] = f"Score insuficiente tras filtro de ubicación: {score}/100"
        result["score"] = int(score)
        result["entry_quality"] = int(score)
        return result

    signal = "call" if direction == "bullish" else "put"
    reason_text = "; ".join(reasons)
    result.update(
        {
            "signal": signal,
            "score": int(score),
            "continuity": True,
            "blocked": False,
            "entry_quality": int(score),
            "reason": f"{signal.upper()} | MOMENTUM M1 | {reason_text}",
        }
    )
    return result


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
