from __future__ import annotations

"""Estrategia M1 basada solamente en Choppiness Index.

Reglas exactas:
- Choppiness Index periodo 14.
- Sobrecompra 61.8.
- Sobreventa 38.2.
- Cruce ARRIBA de 61.8 + vela que produce el cruce ROJA -> CALL en la siguiente M1.
- Cruce ABAJO de 38.2 + vela que produce el cruce VERDE -> PUT en la siguiente M1.
- Expiracion 1 minuto.

No se agregan EMA, RSI, MACD, ATR como filtro, soporte/resistencia,
tendencia, volumen, score ni otras condiciones.
"""

from typing import Any, Dict, Optional
import math
import pandas as pd

MIN_BARS = 30
CI_PERIOD = 14
OVERBOUGHT = 61.8
OVERSOLD = 38.2

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

    return d.dropna(subset=required).drop_duplicates("from" if "from" in d.columns else None).reset_index(drop=True)


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    """Choppiness Index estandar sobre velas ya cerradas."""
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
    valid = tr_sum.notna() & price_range.notna() & (price_range > 0) & (tr_sum > 0)
    denominator = math.log10(period)
    result.loc[valid] = (
        100.0
        * (tr_sum.loc[valid] / price_range.loc[valid]).map(math.log10)
        / denominator
    )
    return result


def _cross_and_candle(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if len(data) < MIN_BARS:
        return None

    ci = _choppiness_index(data)
    current = len(data) - 1
    previous = current - 1

    prev_ci = ci.iloc[previous]
    curr_ci = ci.iloc[current]
    if pd.isna(prev_ci) or pd.isna(curr_ci):
        return None

    row = data.iloc[current]
    op = float(row["open"])
    cl = float(row["close"])
    red = cl < op
    green = cl > op

    # El cruce debe ocurrir entre las dos velas cerradas consecutivas.
    # El valor anterior debe estar en/por debajo del nivel y el actual por encima.
    if float(prev_ci) <= OVERBOUGHT and float(curr_ci) > OVERBOUGHT and red:
        return {
            "signal": "call",
            "ci_previous": float(prev_ci),
            "ci_current": float(curr_ci),
            "threshold": OVERBOUGHT,
            "level": "sobrecompra",
            "candle_color": "roja",
            "reason": (
                f"CALL | Choppiness cruza sobrecompra {OVERBOUGHT:.1f} "
                f"({prev_ci:.2f}->{curr_ci:.2f}) | vela roja | siguiente M1"
            ),
        }

    if float(prev_ci) >= OVERSOLD and float(curr_ci) < OVERSOLD and green:
        return {
            "signal": "put",
            "ci_previous": float(prev_ci),
            "ci_current": float(curr_ci),
            "threshold": OVERSOLD,
            "level": "sobreventa",
            "candle_color": "verde",
            "reason": (
                f"PUT | Choppiness cruza sobreventa {OVERSOLD:.1f} "
                f"({prev_ci:.2f}->{curr_ci:.2f}) | vela verde | siguiente M1"
            ),
        }

    return None


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    data = _normalize(df)
    if len(data) < MIN_BARS:
        return _empty(f"historial insuficiente {len(data)}/{MIN_BARS}")

    pattern = _cross_and_candle(data)
    if pattern is None:
        return _empty("sin cruce CI + color de vela confirmado")

    ts = None
    if "from" in data.columns and pd.notna(data.iloc[-1]["from"]):
        ts = int(data.iloc[-1]["from"])

    return {
        "signal": pattern["signal"],
        "direction": "bullish" if pattern["signal"] == "call" else "bearish",
        "trend": "range",
        "higher_trend": "range",
        "reason": pattern["reason"],
        "score": 0,
        "blocked": False,
        "mode": "M1_M1",
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "candle_timestamp": ts,
        "analysis": pattern,
    }


def get_signal(df):
    return analyze_market(df=df).get("signal")


def signal(df):
    return get_signal(df)
