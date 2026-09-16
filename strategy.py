from typing import Dict, Any
import pandas as pd

# ==============================
# CONFIG
# ==============================

MIN_SCORE_TO_TRADE = 70
MIN_BODY_RATIO = 0.40

ALLOWED_ENTRY_TYPES = {
    "force",
    "continuity",
    "rest",
    "indecision"
}

# ==============================
# HELPERS
# ==============================

def _empty_result(reason=""):
    return {
        "signal": None,
        "score": 0,
        "entry_type": None,
        "blocked": True,
        "reason": reason
    }

def candle_info(c):
    body = abs(c["close"] - c["open"])
    total = c["high"] - c["low"] if c["high"] != c["low"] else 1e-6
    return {
        "body_ratio": body / total
    }

def detect_structure(df: pd.DataFrame):
    highs = df["high"].tail(5)
    lows = df["low"].tail(5)

    if highs.is_monotonic_increasing and lows.is_monotonic_increasing:
        return "bullish"
    if highs.is_monotonic_decreasing and lows.is_monotonic_decreasing:
        return "bearish"
    return "neutral"

# ==============================
# MAIN ANALYSIS
# ==============================

def analyze_market(data: pd.DataFrame) -> Dict[str, Any]:

    if data is None or len(data) < 10:
        return _empty_result("No hay suficientes velas")

    # Última cerrada
    c = data.iloc[-1]

    # Historial sin la actual
    history = data.iloc[:-1].copy()

    # ========================================================
    # FILTRO DE 3 VELAS EXTERIORES
    # ========================================================

    last3 = history.tail(3)

    bulls = sum(1 for i in last3.itertuples() if i.close > i.open)
    bears = sum(1 for i in last3.itertuples() if i.close < i.open)

    if bulls >= 2:
        three_candle_bias = "bullish"
    elif bears >= 2:
        three_candle_bias = "bearish"
    else:
        three_candle_bias = "neutral"

    # ========================================================
    # ESTRUCTURA
    # ========================================================

    structure = detect_structure(history)

    if three_candle_bias != "neutral" and structure != three_candle_bias:
        return _empty_result("Conflicto estructura vs 3 velas")

    # ========================================================
    # DIRECCIÓN BASE
    # ========================================================

    if structure == "bullish":
        signal = "call"
    elif structure == "bearish":
        signal = "put"
    else:
        return _empty_result("Sin estructura clara")

    # ========================================================
    # INFO DE VELA ACTUAL
    # ========================================================

    info = candle_info(c)
    body_ratio = info["body_ratio"]

    if body_ratio < MIN_BODY_RATIO:
        return _empty_result("Vela débil")

    # ========================================================
    # SCORE
    # ========================================================

    score = 50

    # fuerza de vela
    if body_ratio >= 0.6:
        score += 20
    elif body_ratio >= 0.5:
        score += 10

    # confirmación 3 velas
    if three_candle_bias == structure:
        score += 15

    # micro continuidad
    prev = history.iloc[-1]
    if signal == "call" and prev.close > prev.open:
        score += 10
    if signal == "put" and prev.close < prev.open:
        score += 10

    # límite
    score = min(score, 100)

    # ========================================================
    # ENTRY TYPE
    # ========================================================

    if score >= 85:
        entry_type = "force"
    elif score >= 75:
        entry_type = "continuity"
    elif score >= 70:
        entry_type = "rest"
    else:
        return _empty_result("Score bajo")

    # ========================================================
    # FILTRO FINAL
    # ========================================================

    if entry_type not in ALLOWED_ENTRY_TYPES:
        return _empty_result("Tipo no permitido")

    return {
        "signal": signal,
        "score": score,
        "entry_type": entry_type,
        "blocked": False,
        "reason": f"{entry_type} + estructura + 3 velas"
    }
