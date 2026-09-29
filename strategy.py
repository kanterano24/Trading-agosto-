from __future__ import annotations

"""Estrategia M1 basada EXCLUSIVAMENTE en Choppiness Index.

Configuracion exacta del usuario:
- Choppiness Index: periodo 14.
- Sobrecompra: 61.8.
- Sobreventa: 38.2.
- Si el CI cruza hacia ARRIBA 61.8 y la vela que produce el cruce termina ROJA:
  entrada CALL en la siguiente vela M1.
- Si el CI cruza hacia ABAJO 38.2 y la vela que produce el cruce termina VERDE:
  entrada PUT en la siguiente vela M1.
- Expiracion: 1 minuto.

No utiliza RSI, EMA, MACD, Bollinger, ATR como filtro independiente, score,
soporte/resistencia, tendencia, volumen ni ninguna otra condicion de entrada.
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

    return d.dropna(subset=required).reset_index(drop=True)


def _choppiness_index(data: pd.DataFrame, period: int = CI_PERIOD) -> pd.Series:
    """Calcula Choppiness Index estandar con TR y ventana de periodo."""
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

    atr_sum = true_range.rolling(period, min_periods=period).sum()
    highest_high = high.rolling(period, min_periods=period).max()
    lowest_low = low.rolling(period, min_periods=period).min()
    price_range = highest_high - lowest_low

    denominator = math.log10(period)
    values = pd.Series(float("nan"), index=data.index, dtype=float)

    valid = (
        atr_sum.notna()
        & highest_high.notna()
        & lowest_low.notna()
        & (price_range > 0)
        & (atr_sum > 0)
    )

    values.loc[valid] = (
        100.0
        * (atr_sum.loc[valid] / price_range.loc[valid]).map(math.log10)
        / denominator
    )

    return values


def _cross_and_candle(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if len(data) < MIN_BARS:
        return None

    ci = _choppiness_index(data)
    current = len(data) - 1
    previous = current - 1

    ci_prev = ci.iloc[previous]
    ci_now = ci.iloc[current]
    if pd.isna(ci_prev) or pd.isna(ci_now):
        return None

    row = data.iloc[current]
    open_price = float(row["open"])
    close_price = float(row["close"])
    red = close_price < open_price
    green = close_price > open_price

    # Sobrecompra: CI cruza 61.8 hacia arriba + vela roja -> CALL siguiente vela.
    crossed_overbought = float(ci_prev) <= OVERBOUGHT and float(ci_now) > OVERBOUGHT
    if crossed_overbought and red:
        return {
            "signal": "call",
            "ci_previous": float(ci_prev),
            "ci_current": float(ci_now),
            "threshold": OVERBOUGHT,
            "level": "sobrecompra",
            "candle_color": "roja",
            "reason": (
                f"CALL | Choppiness cruza sobrecompra {OVERBOUGHT:.1f} "
                f"({ci_prev:.2f}->{ci_now:.2f}) | vela roja | siguiente M1"
            ),
        }

    # Sobreventa: CI cruza 38.2 hacia abajo + vela verde -> PUT siguiente vela.
    crossed_oversold = float(ci_prev) >= OVERSOLD and float(ci_now) < OVERSOLD
    if crossed_oversold and green:
        return {
            "signal": "put",
            "ci_previous": float(ci_prev),
            "ci_current": float(ci_now),
            "threshold": OVERSOLD,
            "level": "sobreventa",
            "candle_color": "verde",
            "reason": (
                f"PUT | Choppiness cruza sobreventa {OVERSOLD:.1f} "
                f"({ci_prev:.2f}->{ci_now:.2f}) | vela verde | siguiente M1"
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
