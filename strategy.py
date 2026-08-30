from __future__ import annotations

from typing import Any, Dict, List, Optional
import pandas as pd

# ============================================================
# VELA DE DESCANSO + DIVERGENCIA RSI ESTRUCTURAL
# ============================================================

MIN_CANDLES = 22

MIN_PREVIOUS_BODY_PERCENT = 60.0
MAX_REST_BODY_PERCENT = 50.0
MIN_DOMINANCE = 60.0
MIN_CLOSE_POSITION = 65.0

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

PIVOT_STRENGTH = 2
MIN_PIVOT_DISTANCE = 3
MAX_PIVOT_DISTANCE = 20
MIN_RSI_DIFFERENCE = 3.0
MIN_DIVERGENCE_SCORE = 70

EPS = 1e-12


def safe_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    required = {"open", "close", "high", "low"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    data = df.copy()
    for col in required:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data.dropna(subset=list(required), inplace=True)
    if "from" in data.columns:
        data["from"] = pd.to_numeric(data["from"], errors="coerce")
        data.dropna(subset=["from"], inplace=True)
        data["from"] = data["from"].astype(int)
        data.sort_values("from", inplace=True)
        data.drop_duplicates("from", keep="last", inplace=True)
    data.reset_index(drop=True, inplace=True)
    return data


def candle_info(candle: pd.Series) -> Dict[str, Any]:
    o = float(candle["open"])
    c = float(candle["close"])
    h = float(candle["high"])
    l = float(candle["low"])
    rng = h - l
    body = abs(c - o)
    upper = max(h - max(o, c), 0.0)
    lower = max(min(o, c) - l, 0.0)
    if rng <= EPS:
        return {"open": o, "close": c, "high": h, "low": l, "bull": False,
                "bear": False, "neutral": True, "range": 0.0, "body": 0.0,
                "body_ratio": 0.0, "body_percent": 0.0, "upper_wick": 0.0,
                "lower_wick": 0.0, "upper_wick_ratio": 0.0,
                "lower_wick_ratio": 0.0, "close_position": 50.0}
    return {
        "open": o, "close": c, "high": h, "low": l,
        "bull": c > o, "bear": c < o, "neutral": c == o,
        "range": rng, "body": body,
        "body_ratio": body / rng,
        "body_percent": body / rng * 100.0,
        "upper_wick": upper, "lower_wick": lower,
        "upper_wick_ratio": upper / body if body > EPS else 0.0,
        "lower_wick_ratio": lower / body if body > EPS else 0.0,
        "close_position": (c - l) / rng * 100.0,
    }


def calculate_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, EPS)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss > EPS, 100.0)
    rsi = rsi.where(~((avg_gain <= EPS) & (avg_loss <= EPS)), 50.0)
    return rsi


def _pivot_low(data: pd.DataFrame, idx: int) -> bool:
    if idx <= 0 or idx >= len(data) - 1:
        return False
    return bool(data.iloc[idx]["low"] < data.iloc[idx - 1]["low"] and
                data.iloc[idx]["low"] <= data.iloc[idx + 1]["low"])


def _pivot_high(data: pd.DataFrame, idx: int) -> bool:
    if idx <= 0 or idx >= len(data) - 1:
        return False
    return bool(data.iloc[idx]["high"] > data.iloc[idx - 1]["high"] and
                data.iloc[idx]["high"] >= data.iloc[idx + 1]["high"])


def find_structural_pivots(data: pd.DataFrame) -> Dict[str, Any]:
    result = {"current_low_idx": None, "previous_low_idx": None,
              "current_high_idx": None, "previous_high_idx": None}
    if len(data) < MAX_PIVOT_DISTANCE + 5:
        return result

    # Igual que el script Lua: el pivote de la señal está en [2].
    current_low = len(data) - 3
    current_high = len(data) - 3
    if _pivot_low(data, current_low):
        result["current_low_idx"] = current_low
        for i in range(len(data) - 4, max(-1, len(data) - (MAX_PIVOT_DISTANCE + 5)), -1):
            distance = current_low - i
            if distance < MIN_PIVOT_DISTANCE or distance > MAX_PIVOT_DISTANCE:
                continue
            if _pivot_low(data, i):
                result["previous_low_idx"] = i
                break
    if _pivot_high(data, current_high):
        result["current_high_idx"] = current_high
        for i in range(len(data) - 4, max(-1, len(data) - (MAX_PIVOT_DISTANCE + 5)), -1):
            distance = current_high - i
            if distance < MIN_PIVOT_DISTANCE or distance > MAX_PIVOT_DISTANCE:
                continue
            if _pivot_high(data, i):
                result["previous_high_idx"] = i
                break
    return result


def structural_divergence(data: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "bullish": False, "bearish": False,
        "bullish_score": 0, "bearish_score": 0,
        "bullish_reason": [], "bearish_reason": [],
        "current_low": None, "previous_low": None,
        "current_high": None, "previous_high": None,
        "current_low_rsi": None, "previous_low_rsi": None,
        "current_high_rsi": None, "previous_high_rsi": None,
        "bull_rsi_difference": 0.0, "bear_rsi_difference": 0.0,
        "pivot_low_distance": None, "pivot_high_distance": None,
    }
    if len(data) < MIN_CANDLES:
        return out
    work = data.copy()
    work["rsi"] = calculate_rsi(work["close"], RSI_PERIOD)
    pivots = find_structural_pivots(work)

    lo = pivots["current_low_idx"]
    plo = pivots["previous_low_idx"]
    if lo is not None and plo is not None:
        cp = float(work.iloc[lo]["low"]); pp = float(work.iloc[plo]["low"])
        cr = float(work.iloc[lo]["rsi"]); pr = float(work.iloc[plo]["rsi"])
        if pd.notna(cr) and pd.notna(pr):
            diff = cr - pr
            lower_low = cp < pp
            higher_rsi = cr > pr
            zone = pr <= RSI_OVERSOLD or cr <= RSI_OVERSOLD
            score = (30 if lower_low else 0) + (30 if higher_rsi else 0) + (20 if diff >= MIN_RSI_DIFFERENCE else 0) + (20 if zone else 0)
            out.update({"current_low": cp, "previous_low": pp, "current_low_rsi": cr,
                        "previous_low_rsi": pr, "bull_rsi_difference": diff,
                        "pivot_low_distance": lo - plo, "bullish_score": score})
            if lower_low: out["bullish_reason"].append("LOW actual menor que LOW anterior")
            if higher_rsi: out["bullish_reason"].append("RSI actual mayor que RSI anterior")
            if diff >= MIN_RSI_DIFFERENCE: out["bullish_reason"].append(f"divergencia RSI +{diff:.1f}")
            if zone: out["bullish_reason"].append("RSI en zona de sobreventa")
            out["bullish"] = bool(lower_low and higher_rsi and diff >= MIN_RSI_DIFFERENCE and zone and score >= MIN_DIVERGENCE_SCORE)

    hi = pivots["current_high_idx"]
    phi = pivots["previous_high_idx"]
    if hi is not None and phi is not None:
        cp = float(work.iloc[hi]["high"]); pp = float(work.iloc[phi]["high"])
        cr = float(work.iloc[hi]["rsi"]); pr = float(work.iloc[phi]["rsi"])
        if pd.notna(cr) and pd.notna(pr):
            diff = pr - cr
            higher_high = cp > pp
            lower_rsi = cr < pr
            zone = pr >= RSI_OVERBOUGHT or cr >= RSI_OVERBOUGHT
            score = (30 if higher_high else 0) + (30 if lower_rsi else 0) + (20 if diff >= MIN_RSI_DIFFERENCE else 0) + (20 if zone else 0)
            out.update({"current_high": cp, "previous_high": pp, "current_high_rsi": cr,
                        "previous_high_rsi": pr, "bear_rsi_difference": diff,
                        "pivot_high_distance": hi - phi, "bearish_score": score})
            if higher_high: out["bearish_reason"].append("HIGH actual mayor que HIGH anterior")
            if lower_rsi: out["bearish_reason"].append("RSI actual menor que RSI anterior")
            if diff >= MIN_RSI_DIFFERENCE: out["bearish_reason"].append(f"divergencia RSI -{diff:.1f}")
            if zone: out["bearish_reason"].append("RSI en zona de sobrecompra")
            out["bearish"] = bool(higher_high and lower_rsi and diff >= MIN_RSI_DIFFERENCE and zone and score >= MIN_DIVERGENCE_SCORE)
    return out


def analyze_market(candle_1m: Any, previous_m1: Optional[pd.DataFrame] = None,
                   pair: Optional[str] = None, candles_5s: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """Decide exclusivamente con la M1 N cerrada. N+1 no participa."""
    result: Dict[str, Any] = {
        "valid": False, "signal": None, "score": 0, "direction": "NEUTRAL",
        "pair": pair, "pattern": "DESCANSO_DIV_RSI", "reason": "",
        "structure": "NEUTRAL", "support": 0.0, "resistance": 0.0,
        "bullish_score": 0, "bearish_score": 0, "confidence": 0,
        "micro_analysis": {}, "analysis": {},
    }
    hist = safe_dataframe(previous_m1)
    current_df = safe_dataframe(pd.DataFrame([candle_1m])) if candle_1m is not None else pd.DataFrame()
    if not current_df.empty:
        if hist.empty or "from" not in hist.columns or "from" not in current_df.columns or int(current_df.iloc[-1]["from"]) not in set(hist["from"].astype(int)):
            hist = pd.concat([hist, current_df], ignore_index=True)
    hist = safe_dataframe(hist)
    if len(hist) < MIN_CANDLES:
        result["reason"] = f"Historial insuficiente: {len(hist)}/{MIN_CANDLES}"
        return result

    last = candle_info(hist.iloc[-1])
    previous = candle_info(hist.iloc[-2])
    prev_strong = previous["range"] > 0 and previous["body_percent"] >= MIN_PREVIOUS_BODY_PERCENT
    current_rest = (last["range"] > 0 and last["body_percent"] <= MAX_REST_BODY_PERCENT and last["body"] < previous["body"])
    bull_dom = (last["close_position"] * 0.70 + (last["lower_wick"] / last["range"] * 100.0) * 0.30) if last["range"] > 0 else 0.0
    bear_dom = ((100.0 - last["close_position"]) * 0.70 + (last["upper_wick"] / last["range"] * 100.0) * 0.30) if last["range"] > 0 else 0.0
    diff_dom = abs(bull_dom - bear_dom)
    bull_dominant = bull_dom >= MIN_DOMINANCE and bull_dom > bear_dom and diff_dom >= 10 and last["close_position"] >= MIN_CLOSE_POSITION
    bear_dominant = bear_dom >= MIN_DOMINANCE and bear_dom > bull_dom and diff_dom >= 10 and last["close_position"] <= 100 - MIN_CLOSE_POSITION
    bull_recovery = last["bull"] and last["close_position"] >= MIN_CLOSE_POSITION and last["close"] > float(hist.iloc[-2]["low"])
    bear_recovery = last["bear"] and last["close_position"] <= 100 - MIN_CLOSE_POSITION and last["close"] < float(hist.iloc[-2]["high"])

    div = structural_divergence(hist)
    bullish = bool(prev_strong and current_rest and div["bullish"] and bull_recovery and bull_dominant)
    bearish = bool(prev_strong and current_rest and div["bearish"] and bear_recovery and bear_dominant)
    direction = "BULLISH" if bullish else ("BEARISH" if bearish else "NEUTRAL")
    signal = "call" if bullish else ("put" if bearish else None)
    score = div["bullish_score"] if bullish else (div["bearish_score"] if bearish else max(div["bullish_score"], div["bearish_score"]))

    result.update({
        "valid": signal is not None, "signal": signal, "score": int(score),
        "confidence": int(score), "direction": direction,
        "bullish_score": int(div["bullish_score"]), "bearish_score": int(div["bearish_score"]),
        "support": float(hist["low"].tail(10).min()), "resistance": float(hist["high"].tail(10).max()),
        "open": last["open"], "close": last["close"], "high": last["high"], "low": last["low"],
        "range": last["range"], "body": last["body"], "body_ratio": last["body_ratio"],
        "body_percent": last["body_percent"], "upper_wick": last["upper_wick"], "lower_wick": last["lower_wick"],
        "close_position": last["close_position"],
    })
    conditions = {
        "previous_strong": prev_strong, "current_rest": current_rest,
        "bull_dominance": bull_dominant, "bear_dominance": bear_dominant,
        "bull_recovery": bull_recovery, "bear_recovery": bear_recovery,
    }
    result["analysis"] = {"divergence": div, "conditions": conditions,
                           "candle": last, "previous_candle": previous}
    if signal == "call":
        result["reason"] = "CALL: divergencia alcista estructural + vela de descanso + recuperación + dominancia compradora"
    elif signal == "put":
        result["reason"] = "PUT: divergencia bajista estructural + vela de descanso + recuperación + dominancia vendedora"
    elif div["bullish"] or div["bearish"]:
        result["reason"] = "Divergencia detectada, pero faltó confirmación de vela descanso/recuperación/dominancia"
    else:
        result["reason"] = "Sin divergencia estructural válida"
    return result


def get_signal(candle_1m, previous_m1=None, pair: Optional[str] = None, candles_5s=None):
    return analyze_market(candle_1m, previous_m1, pair, candles_5s).get("signal")


def signal(candle_1m, previous_m1=None, pair: Optional[str] = None, candles_5s=None):
    return get_signal(candle_1m, previous_m1, pair, candles_5s)


def detect_structure(df: pd.DataFrame) -> str:
    data = safe_dataframe(df)
    if len(data) < MIN_CANDLES:
        return "NEUTRAL"
    div = structural_divergence(data)
    if div["bullish"]: return "BULLISH_DIVERGENCE"
    if div["bearish"]: return "BEARISH_DIVERGENCE"
    return "NEUTRAL"


if __name__ == "__main__":
    print("Estrategia VELA DE DESCANSO + DIVERGENCIA RSI ESTRUCTURAL cargada correctamente")
