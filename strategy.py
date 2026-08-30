from __future__ import annotations

from typing import Any, Dict, Optional
import pandas as pd

# ============================================================
# M1 MULTI-MARKET CONTINUITY STRATEGY
# ============================================================
# Compatible with bot.py and market_scanner.py supplied by user.
# Main principle:
#   DO NOT buy merely because the current candle is the 2nd/3rd
#   green/red candle. First identify a real structural breakout,
#   then require a controlled continuation/pullback. Avoid buying
#   the extreme end of an impulse.
# ============================================================

TREND_LOOKBACK = 20
STRUCTURE_LOOKBACK = 12
CONTINUITY_LOOKBACK = 6
ATR_PERIOD = 14
SR_LOOKBACK = 20

MIN_STRUCTURE_SCORE = 8
MIN_RECENT_STRUCTURE_SCORE = 5
MIN_FINAL_SCORE = 82

# Impulse / continuation protection
IMPULSE_LOOKBACK = 10
MAX_IMPULSE_AGE = 5
MIN_IMPULSE_BODY_RATIO = 0.45
MAX_IMPULSE_TOTAL_ATR = 3.25
MIN_IMPULSE_EXTENSION_ATR = 1.00
MAX_IMPULSE_EXTENSION_ATR = 1.60
MAX_CONSECUTIVE_DIRECTION_CANDLES = 4

# We prefer continuation after a pause/retest, not the climax candle.
MIN_PULLBACK_SCORE = 3
MAX_EXTENSION_FROM_BASE_ATR = 2.20
MAX_ENTRY_CANDLE_RANGE_ATR = 1.45
MAX_ENTRY_CANDLE_BODY_ATR = 1.10

DOJI_BODY_RATIO = 0.10
INDECISION_BODY_RATIO = 0.25
MIN_CONTINUITY_BODY_RATIO = 0.38
MAX_COUNTER_WICK_RATIO = 0.45
SR_TOLERANCE_ATR = 0.30
MAX_SCORE = 100


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_ohlc(candle: pd.Series):
    if candle is None:
        return None
    o = _to_float(candle.get("open"))
    c = _to_float(candle.get("close"))
    h = _to_float(candle.get("high", candle.get("max")))
    l = _to_float(candle.get("low", candle.get("min")))
    if None in (o, c, h, l) or h < l:
        return None
    return o, h, l, c


def safe_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    required = {"open", "close", "high", "low"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    out = df.copy()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out.dropna(subset=list(required), inplace=True)
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out.sort_values("from", inplace=True)
        out.drop_duplicates(subset=["from"], keep="last", inplace=True)
    out.reset_index(drop=True, inplace=True)
    return out


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    work = safe_dataframe(df)
    if len(work) < 2:
        return 0.0
    prev = work["close"].shift(1)
    tr = pd.concat([
        work["high"] - work["low"],
        (work["high"] - prev).abs(),
        (work["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    value = tr.rolling(min(period, len(work)), min_periods=2).mean().iloc[-1]
    return 0.0 if pd.isna(value) else float(value)


def get_candle_data(candle: pd.Series) -> Optional[Dict[str, float]]:
    ohlc = _get_ohlc(candle)
    if ohlc is None:
        return None
    o, h, l, c = ohlc
    rng = h - l
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    if rng <= 0:
        return {"open": o, "close": c, "high": h, "low": l,
                "range": 0.0, "body": 0.0, "upper_wick": 0.0,
                "lower_wick": 0.0, "body_ratio": 0.0,
                "upper_wick_ratio": 0.0, "lower_wick_ratio": 0.0,
                "close_position": 0.5}
    return {"open": o, "close": c, "high": h, "low": l,
            "range": rng, "body": body, "upper_wick": upper,
            "lower_wick": lower, "body_ratio": body / rng,
            "upper_wick_ratio": upper / rng,
            "lower_wick_ratio": lower / rng,
            "close_position": (c - l) / rng}


def analyze_structure(df: pd.DataFrame) -> Dict[str, Any]:
    result = {"direction": "NEUTRAL", "score": 0, "bullish_score": 0,
              "bearish_score": 0, "reason": "estructura insuficiente"}
    work = safe_dataframe(df).tail(TREND_LOOKBACK).reset_index(drop=True)
    if len(work) < 8:
        return result
    highs, lows, closes = work.high.tolist(), work.low.tolist(), work.close.tolist()
    hh = sum(highs[i] > highs[i-1] for i in range(1, len(highs)))
    hl = sum(lows[i] > lows[i-1] for i in range(1, len(lows)))
    lh = sum(highs[i] < highs[i-1] for i in range(1, len(highs)))
    ll = sum(lows[i] < lows[i-1] for i in range(1, len(lows)))
    upclose = sum(closes[i] > closes[i-1] for i in range(1, len(closes)))
    dnclose = sum(closes[i] < closes[i-1] for i in range(1, len(closes)))
    bull = (3 if hh >= 7 else 0) + (3 if hl >= 7 else 0) + (2 if upclose >= 7 else 0) + (2 if closes[-1] > closes[0] else 0)
    bear = (3 if lh >= 7 else 0) + (3 if ll >= 7 else 0) + (2 if dnclose >= 7 else 0) + (2 if closes[-1] < closes[0] else 0)
    result.update(bullish_score=bull, bearish_score=bear)
    if bull >= MIN_STRUCTURE_SCORE and bull > bear:
        result.update(direction="BULLISH", score=bull, reason="estructura alcista con HH/HL")
    elif bear >= MIN_STRUCTURE_SCORE and bear > bull:
        result.update(direction="BEARISH", score=bear, reason="estructura bajista con LH/LL")
    else:
        result.update(direction="NEUTRAL", score=max(bull, bear), reason="estructura lateral o mezclada")
    return result


def recent_structure_quality(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    result = {"valid": False, "score": 0, "reason": "estructura reciente insuficiente"}
    work = safe_dataframe(df).tail(STRUCTURE_LOOKBACK).reset_index(drop=True)
    if len(work) < 6 or direction not in ("BULLISH", "BEARISH"):
        return result
    highs, lows, closes = work.high.tolist(), work.low.tolist(), work.close.tolist()
    score = 0
    if direction == "BULLISH":
        score += 3 if sum(highs[i] > highs[i-1] for i in range(1, len(highs))) >= 3 else 0
        score += 3 if sum(lows[i] > lows[i-1] for i in range(1, len(lows))) >= 3 else 0
        score += 2 if closes[-1] > closes[-2] else 0
    else:
        score += 3 if sum(highs[i] < highs[i-1] for i in range(1, len(highs))) >= 3 else 0
        score += 3 if sum(lows[i] < lows[i-1] for i in range(1, len(lows))) >= 3 else 0
        score += 2 if closes[-1] < closes[-2] else 0
    result["score"] = score
    result["valid"] = score >= MIN_RECENT_STRUCTURE_SCORE
    result["reason"] = f"estructura reciente score={score}"
    return result


def _find_impulse(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    """Find the latest meaningful structural break, not simply candle #1."""
    work = safe_dataframe(df).tail(IMPULSE_LOOKBACK + 2).reset_index(drop=True)
    result = {"found": False, "index": -1, "age": 99, "score": 0,
              "extension_atr": 0.0, "reason": "sin ruptura estructural reciente"}
    if len(work) < 7:
        return result
    atr = calculate_atr(work.iloc[:-1])
    if atr <= 0:
        return result
    # Break a local structure of the preceding 3 candles. This is much stricter
    # than comparing only with the immediately previous candle.
    found = -1
    for i in range(3, len(work)):
        c = get_candle_data(work.iloc[i])
        if c is None or c["body_ratio"] < MIN_IMPULSE_BODY_RATIO:
            continue
        prior = work.iloc[max(0, i-3):i]
        if direction == "BULLISH" and c["close"] > float(prior["high"].max()) and c["close"] > c["open"]:
            found = i
        elif direction == "BEARISH" and c["close"] < float(prior["low"].min()) and c["close"] < c["open"]:
            found = i
    if found < 0:
        return result
    age = len(work) - 1 - found
    segment = work.iloc[found:]
    extension = (float(segment.high.max()) - float(segment.low.min())) / atr
    score = 5
    score += 4 if age <= 1 else 3 if age == 2 else 2 if age <= 3 else 0
    score += 3 if extension <= 1.8 else 1 if extension <= MAX_IMPULSE_TOTAL_ATR else 0
    result.update(found=True, index=found, age=age, extension_atr=extension, score=score)
    result["reason"] = f"ruptura estructural age={age}, extension={extension:.2f} ATR"
    return result


def analyze_impulse_start(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    base = _find_impulse(df, direction)
    result = {"valid": base["found"], "score": base["score"], "age": base["age"],
              "extended": base["extension_atr"] > MAX_IMPULSE_TOTAL_ATR,
              "extension_atr": base["extension_atr"], "reason": base["reason"]}
    if not base["found"]:
        return result
    if base["age"] > MAX_IMPULSE_AGE or result["extended"]:
        result["valid"] = False
        result["reason"] = f"impulso demasiado avanzado/extendido age={base['age']}"
        return result

    # Filtro solicitado: solo operar con extensión entre 1.00 y 1.60 ATR.
    extension = float(base["extension_atr"])
    if extension < MIN_IMPULSE_EXTENSION_ATR or extension > MAX_IMPULSE_EXTENSION_ATR:
        result["valid"] = False
        result["reason"] = (
            f"extensión ATR fuera de rango: {extension:.2f} ATR "
            f"(permitido {MIN_IMPULSE_EXTENSION_ATR:.2f}-{MAX_IMPULSE_EXTENSION_ATR:.2f})"
        )
        return result

    return result


def detect_late_trend(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    work = safe_dataframe(df).tail(MAX_CONSECUTIVE_DIRECTION_CANDLES)
    consecutive = 0
    for _, row in work.iloc[::-1].iterrows():
        if direction == "BULLISH" and row.close > row.open:
            consecutive += 1
        elif direction == "BEARISH" and row.close < row.open:
            consecutive += 1
        else:
            break
    late = consecutive >= MAX_CONSECUTIVE_DIRECTION_CANDLES
    return {"late": late, "penalty": 12 if late else 0,
            "consecutive": consecutive,
            "reason": f"{consecutive} velas consecutivas" if late else "sin tendencia demasiado avanzada"}


def _continuation_setup(df: pd.DataFrame, direction: str, impulse: Dict[str, Any]) -> Dict[str, Any]:
    """Require a pause/retest before continuation. This prevents buying the climax."""
    result = {"valid": False, "score": 0, "pullback": False, "reason": "sin retroceso/control"}
    work = safe_dataframe(df).reset_index(drop=True)
    if len(work) < 5 or not impulse.get("valid"):
        return result
    age = int(impulse.get("age", 99))
    # For a very young impulse, require at least one controlled candle after the break.
    if age == 0:
        result["reason"] = "ruptura acaba de cerrar; falta vela de continuidad controlada"
        return result
    recent = work.tail(min(age + 3, 7)).reset_index(drop=True)
    candles = [get_candle_data(recent.iloc[i]) for i in range(len(recent))]
    candles = [x for x in candles if x]
    if len(candles) < 3:
        return result
    score = 0
    pause = 0
    continuation = 0
    for c in candles[:-1]:
        if direction == "BULLISH":
            if c["close"] <= c["open"] or c["body_ratio"] < MIN_CONTINUITY_BODY_RATIO:
                pause += 1
        else:
            if c["close"] >= c["open"] or c["body_ratio"] < MIN_CONTINUITY_BODY_RATIO:
                pause += 1
    last = candles[-1]
    if direction == "BULLISH" and last["close"] > last["open"]:
        continuation = 1
    if direction == "BEARISH" and last["close"] < last["open"]:
        continuation = 1
    if pause >= 1:
        score += 3
    if continuation:
        score += 3
    if last["body_ratio"] >= MIN_CONTINUITY_BODY_RATIO:
        score += 2
    result.update(score=score, pullback=pause >= 1, valid=score >= MIN_PULLBACK_SCORE and continuation == 1)
    result["reason"] = f"continuación tras pausa score={score}" if result["valid"] else f"sin continuidad controlada score={score}"
    return result


def check_continuity(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    work = safe_dataframe(df).tail(CONTINUITY_LOOKBACK).reset_index(drop=True)
    result = {"valid": False, "score": 0, "reason": "sin continuidad"}
    if len(work) < 4:
        return result
    highs, lows, closes = work.high.tolist(), work.low.tolist(), work.close.tolist()
    if direction == "BULLISH":
        score = (3 if sum(highs[i] >= highs[i-1] for i in range(1, len(highs))) >= 3 else 0) + (3 if sum(lows[i] >= lows[i-1] for i in range(1, len(lows))) >= 3 else 0) + (2 if closes[-1] > closes[-2] else 0)
    elif direction == "BEARISH":
        score = (3 if sum(highs[i] <= highs[i-1] for i in range(1, len(highs))) >= 3 else 0) + (3 if sum(lows[i] <= lows[i-1] for i in range(1, len(lows))) >= 3 else 0) + (2 if closes[-1] < closes[-2] else 0)
    else:
        score = 0
    result.update(score=score, valid=score >= 5, reason=f"continuidad {direction.lower()} score={score}")
    return result


def detect_end_of_trend(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    work = safe_dataframe(df)
    result = {"exhausted": False, "penalty": 0, "reason": "sin agotamiento evidente"}
    if len(work) < 3:
        return result
    last = get_candle_data(work.iloc[-1]); prev = get_candle_data(work.iloc[-2])
    if not last or not prev:
        return result
    penalty = 0; reasons = []
    if direction == "BULLISH":
        if last["upper_wick_ratio"] >= 0.50: penalty += 8; reasons.append("rechazo superior")
        if last["body_ratio"] < 0.20: penalty += 6; reasons.append("cuerpo débil")
        if last["close"] < prev["low"]: penalty += 12; reasons.append("pérdida de estructura")
    elif direction == "BEARISH":
        if last["lower_wick_ratio"] >= 0.50: penalty += 8; reasons.append("rechazo inferior")
        if last["body_ratio"] < 0.20: penalty += 6; reasons.append("cuerpo débil")
        if last["close"] > prev["high"]: penalty += 12; reasons.append("pérdida de estructura")
    result.update(exhausted=penalty >= 10, penalty=penalty, reason=", ".join(reasons) or "sin agotamiento evidente")
    return result


def check_support_resistance(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    result = {"blocked": False, "penalty": 0, "reason": "sin bloqueo S/R", "support": None, "resistance": None}
    work = safe_dataframe(df)
    if len(work) < 6:
        return result
    hist = work.iloc[:-1].tail(SR_LOOKBACK)
    atr = calculate_atr(hist)
    if atr <= 0:
        return result
    price = float(work.iloc[-1].close)
    support = float(hist.low.min()); resistance = float(hist.high.max())
    result.update(support=support, resistance=resistance)
    tol = atr * SR_TOLERANCE_ATR
    if direction == "BULLISH" and resistance - price <= tol:
        result.update(blocked=True, penalty=12, reason="CALL cerca de resistencia")
    elif direction == "BEARISH" and price - support <= tol:
        result.update(blocked=True, penalty=12, reason="PUT cerca de soporte")
    return result


def confirmation_score(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    result = {"score": 0, "valid": False, "reason": "", "range_atr": 0.0, "body_atr": 0.0}
    work = safe_dataframe(df)
    if len(work) < 2:
        result["reason"] = "pocas velas"
        return result
    candle = get_candle_data(work.iloc[-1])
    atr = calculate_atr(work.iloc[:-1]) or (candle["range"] if candle else 0.0)
    if not candle or atr <= 0:
        result["reason"] = "vela/ATR inválido"
        return result
    result["range_atr"] = candle["range"] / atr; result["body_atr"] = candle["body"] / atr
    score = 0; reasons = []
    if direction == "BULLISH":
        score += 5 if candle["close"] > candle["open"] else 0
        score += 5 if candle["body_ratio"] >= MIN_CONTINUITY_BODY_RATIO else 0
        score += 4 if candle["close_position"] >= 0.65 else 0
        score += 3 if candle["upper_wick_ratio"] <= MAX_COUNTER_WICK_RATIO else 0
    elif direction == "BEARISH":
        score += 5 if candle["close"] < candle["open"] else 0
        score += 5 if candle["body_ratio"] >= MIN_CONTINUITY_BODY_RATIO else 0
        score += 4 if candle["close_position"] <= 0.35 else 0
        score += 3 if candle["lower_wick_ratio"] <= MAX_COUNTER_WICK_RATIO else 0
    if result["range_atr"] > MAX_ENTRY_CANDLE_RANGE_ATR:
        score -= 10; reasons.append("vela demasiado extendida")
    if result["body_atr"] > MAX_ENTRY_CANDLE_BODY_ATR:
        score -= 8; reasons.append("cuerpo demasiado extendido")
    if candle["body_ratio"] <= INDECISION_BODY_RATIO:
        score -= 8; reasons.append("vela indecisa")
    result["score"] = max(0, score)
    result["valid"] = result["score"] >= 12 and not reasons
    result["reason"] = ", ".join(reasons) if reasons else f"confirmación score={result['score']}"
    return result


def analyze_live_candle(candle_1m: pd.Series) -> Dict[str, Any]:
    result = {"direction": "NEUTRAL", "state": "INDEFINITION", "score": 0,
              "open": None, "close": None, "high": None, "low": None,
              "range": 0.0, "body": 0.0, "body_ratio": 0.0,
              "upper_wick": 0.0, "lower_wick": 0.0, "close_position": 0.5}
    data = get_candle_data(candle_1m)
    if not data:
        return result
    result.update(data)
    direction = "BULLISH" if data["close"] > data["open"] else "BEARISH" if data["close"] < data["open"] else "NEUTRAL"
    result["direction"] = direction
    score = 0
    if direction == "BULLISH":
        score += 5 if data["body_ratio"] >= 0.40 else 0
        score += 5 if data["close_position"] >= 0.65 else 0
        score += 3 if data["upper_wick_ratio"] <= 0.30 else 0
    elif direction == "BEARISH":
        score += 5 if data["body_ratio"] >= 0.40 else 0
        score += 5 if data["close_position"] <= 0.35 else 0
        score += 3 if data["lower_wick_ratio"] <= 0.30 else 0
    result["state"] = "DOJI" if data["body_ratio"] <= DOJI_BODY_RATIO else "INDECISION" if data["body_ratio"] <= INDECISION_BODY_RATIO else "LIVE_CONTINUITY" if score >= 10 else "MOVEMENT"
    result["score"] = score
    return result


def analyze_market(candle_1m: Optional[pd.Series] = None, candles_5s: Optional[pd.DataFrame] = None, previous_m1: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"signal": None, "valid": False, "score": 0, "direction": "NEUTRAL", "state": "NO_SIGNAL", "reason": "sin análisis", "minute_timestamp": None, "minute_open": None, "minute_close": None, "structure": {}, "recent_structure": {}, "impulse": {}, "late_trend": {}, "continuity": {}, "confirmation": {}, "exhaustion": {}, "support_resistance": {}, "setup": {}}
    if candle_1m is None:
        result["reason"] = "vela M1 no disponible"; return result
    current = get_candle_data(candle_1m)
    if not current:
        result["reason"] = "OHLC inválido"; return result
    if "from" in candle_1m.index:
        try: result["minute_timestamp"] = int(float(candle_1m["from"]))
        except Exception: pass
    result["minute_open"] = current["open"]; result["minute_close"] = current["close"]
    historical = safe_dataframe(previous_m1)
    if historical.empty:
        historical = pd.DataFrame([dict(candle_1m)])
    # The caller passes the closed candle inside history. Do NOT append a duplicate.
    historical = safe_dataframe(historical)
    if len(historical) < 10:
        result["reason"] = "historial insuficiente"; return result
    analysis_df = historical.copy()
    if len(analysis_df) >= 2:
        current_row = analysis_df.iloc[-1]
        # analyze the closed candle against candles before it; confirmation uses it as entry candidate.
        structure_df = analysis_df.iloc[:-1]
    else:
        structure_df = analysis_df
    structure = analyze_structure(structure_df)
    result["structure"] = structure; direction = structure["direction"]; result["direction"] = direction
    if direction == "NEUTRAL":
        result.update(state="RANGE", reason="mercado sin estructura clara"); return result
    recent = recent_structure_quality(structure_df, direction); result["recent_structure"] = recent
    if not recent["valid"]:
        result.update(state="WEAK_RECENT_STRUCTURE", reason="estructura reciente débil"); return result
    impulse = analyze_impulse_start(structure_df, direction); result["impulse"] = impulse
    if not impulse["valid"]:
        result.update(state="NO_EARLY_IMPULSE", reason=impulse["reason"]); return result
    late = detect_late_trend(structure_df, direction); result["late_trend"] = late
    if late["late"]:
        result.update(state="LATE_TREND", reason=late["reason"]); return result
    # New key protection: continuation must come after a pause/retest, not simply candle count.
    setup = _continuation_setup(analysis_df, direction, impulse); result["setup"] = setup
    if not setup["valid"]:
        result.update(state="NO_CONTROLLED_CONTINUATION", reason=setup["reason"]); return result
    continuity = check_continuity(analysis_df, direction)
    confirmation = confirmation_score(analysis_df, direction)
    exhaustion = detect_end_of_trend(analysis_df, direction)
    sr = check_support_resistance(analysis_df, direction)
    result.update(continuity=continuity, confirmation=confirmation, exhaustion=exhaustion, support_resistance=sr)
    if not continuity["valid"]:
        result.update(state="NO_CONTINUITY", reason="sin continuidad suficiente"); return result
    if exhaustion["exhausted"]:
        result.update(state="EXHAUSTION", reason=f"tendencia agotada: {exhaustion['reason']}"); return result
    if sr["blocked"]:
        result.update(state="SUPPORT_RESISTANCE", reason=sr["reason"]); return result
    if not confirmation["valid"]:
        result.update(state="WEAK_CONFIRMATION", reason=f"confirmación débil: {confirmation['reason']}"); return result
    score = min(30, structure["score"] * 3)
    score += min(20, continuity["score"] * 2)
    score += min(20, confirmation["score"])
    score += min(15, recent["score"] * 2)
    score += min(10, setup["score"] * 2)
    score += 5 if impulse["age"] <= 2 else 3 if impulse["age"] <= 3 else 0
    score -= exhaustion["penalty"] + sr["penalty"]
    score = max(0, min(MAX_SCORE, int(score)))
    result["score"] = score
    if score < MIN_FINAL_SCORE:
        result.update(state="LOW_SCORE", reason=f"calidad insuficiente score={score}"); return result
    result["signal"] = "call" if direction == "BULLISH" else "put"
    result["valid"] = True
    result["state"] = "BULLISH_CONTINUITY" if direction == "BULLISH" else "BEARISH_CONTINUITY"
    result["reason"] = f"{result['signal'].upper()} continuidad estructural controlada score={score}"
    return result


def analyze_minute(candle_1m: pd.Series, candles_5s: Optional[pd.DataFrame] = None, previous_m1: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    return analyze_market(candle_1m, candles_5s, previous_m1)


def build_n1_signal(candle_1m: pd.Series, candles_5s: Optional[pd.DataFrame] = None, previous_m1: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    return analyze_market(candle_1m, candles_5s, previous_m1)


def get_signal(candle_1m: pd.Series, candles_5s: Optional[pd.DataFrame] = None, previous_m1: Optional[pd.DataFrame] = None) -> Optional[str]:
    return analyze_market(candle_1m, candles_5s, previous_m1).get("signal")


def signal(candle_1m: pd.Series, candles_5s: Optional[pd.DataFrame] = None, previous_m1: Optional[pd.DataFrame] = None) -> Optional[str]:
    return get_signal(candle_1m, candles_5s, previous_m1)


def get_m1_direction(candle_1m=None):
    if candle_1m is None:
        return None
    try:
        if hasattr(candle_1m, "iloc") and hasattr(candle_1m, "columns"):
            if len(candle_1m) == 0: return None
            candle_1m = candle_1m.iloc[-1]
        o = float(candle_1m.get("open")); c = float(candle_1m.get("close"))
    except Exception:
        return None
    return "BULLISH" if c > o else "BEARISH" if c < o else "NEUTRAL"


def check_pattern(candles_5s=None):
    return None


if __name__ == "__main__":
    print("strategy.py cargado correctamente - M1 Structural Continuation")
