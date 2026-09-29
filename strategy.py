from __future__ import annotations

"""Patron exacto de la imagen para entradas M1 con expiracion de 1 minuto.

PUT:
1. Se toma el ultimo maximo confirmado como resistencia.
2. El precio se aleja de esa resistencia.
3. El precio regresa y toca la resistencia.
4. La vela de rechazo toca la resistencia y termina por debajo de ella.
5. La siguiente vela termina roja.
6. Entrada PUT con expiracion de 1 minuto.

CALL es exactamente lo contrario:
1. Ultimo minimo confirmado como soporte.
2. El precio se aleja del soporte.
3. Regresa y toca el soporte.
4. La vela de rechazo toca el soporte y termina por encima.
5. La siguiente vela termina verde.
6. Entrada CALL con expiracion de 1 minuto.

No se usan indicadores ni filtros adicionales.
"""

from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS = 20
SWING_LEFT = 2
SWING_RIGHT = 2
SWING_LOOKBACK = 30
ZONE_TOLERANCE = 0.0015

MODE_CONFIG = {
    "M1_M1": {"analysis_tf": "M1", "entry_tf": "M1", "expiration": 1},
}


def _empty(reason: str = "sin señal", mode: str = "M1_M1") -> Dict[str, Any]:
    return {
        "signal": None,
        "direction": "range",
        "trend": "range",
        "higher_trend": "range",
        "reason": reason,
        "score": 0,
        "blocked": True,
        "mode": mode,
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


def _direction(row: pd.Series) -> str:
    o = float(row["open"])
    c = float(row["close"])
    if c > o:
        return "bullish"
    if c < o:
        return "bearish"
    return "neutral"


def _swings(data: pd.DataFrame) -> Tuple[list, list]:
    highs, lows = [], []
    if len(data) < SWING_LEFT + SWING_RIGHT + 1:
        return highs, lows

    start = max(SWING_LEFT, len(data) - SWING_LOOKBACK - SWING_RIGHT)
    end = len(data) - SWING_RIGHT

    for i in range(start, end):
        h = float(data.iloc[i]["high"])
        l = float(data.iloc[i]["low"])

        left_high = float(data.iloc[i - SWING_LEFT:i]["high"].max())
        right_high = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["high"].max())
        left_low = float(data.iloc[i - SWING_LEFT:i]["low"].min())
        right_low = float(data.iloc[i + 1:i + 1 + SWING_RIGHT]["low"].min())

        if h >= left_high and h >= right_high:
            highs.append((i, h))
        if l <= left_low and l <= right_low:
            lows.append((i, l))

    return highs, lows


def _m15_direction(higher: pd.DataFrame) -> str:
    """Solo conserva el contexto M15 ya usado por el bot."""
    if higher is None or len(higher) < 4:
        return "range"

    highs, lows = _swings(higher)
    if len(highs) < 2 or len(lows) < 2:
        return "range"

    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return "bearish"
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
        return "bullish"
    return "range"


def _put_pattern(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Detecta solamente el patron PUT dibujado en la imagen."""
    # La ultima vela es la confirmacion roja. La anterior es el rechazo.
    if len(data) < 4:
        return None

    rejection_idx = len(data) - 2
    rejection = data.iloc[rejection_idx]

    # El ultimo maximo confirmado antes del recorrido/regreso.
    search_end = rejection_idx - SWING_RIGHT
    if search_end <= SWING_LEFT:
        return None

    search_data = data.iloc[: search_end + 1].copy()
    highs, _ = _swings(search_data)
    if not highs:
        return None

    swing_idx, resistance = highs[-1]

    # Debe existir recorrido despues del maximo antes de volver a tocarlo.
    after_swing = data.iloc[swing_idx + 1:rejection_idx]
    if after_swing.empty:
        return None

    lowest_after_swing = float(after_swing["low"].min())
    if lowest_after_swing >= resistance:
        return None

    # La vela de rechazo toca la resistencia y termina por debajo.
    rejection_high = float(rejection["high"])
    rejection_close = float(rejection["close"])
    if rejection_high < resistance * (1 - ZONE_TOLERANCE):
        return None
    if rejection_close >= resistance:
        return None

    # La vela siguiente debe terminar roja.
    confirmation = data.iloc[-1]
    if _direction(confirmation) != "bearish":
        return None

    return {
        "signal": "put",
        "swing_index": int(swing_idx),
        "level": float(resistance),
        "rejection_index": int(rejection_idx),
        "confirmation_index": int(len(data) - 1),
        "rejection_close": rejection_close,
        "rejection_high": rejection_high,
        "confirmation_close": float(confirmation["close"]),
    }


def _call_pattern(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Detecta solamente el patron CALL inverso al PUT de la imagen."""
    if len(data) < 4:
        return None

    rejection_idx = len(data) - 2
    rejection = data.iloc[rejection_idx]

    search_end = rejection_idx - SWING_RIGHT
    if search_end <= SWING_LEFT:
        return None

    search_data = data.iloc[: search_end + 1].copy()
    _, lows = _swings(search_data)
    if not lows:
        return None

    swing_idx, support = lows[-1]

    # Debe existir recorrido despues del minimo antes de volver a tocarlo.
    after_swing = data.iloc[swing_idx + 1:rejection_idx]
    if after_swing.empty:
        return None

    highest_after_swing = float(after_swing["high"].max())
    if highest_after_swing <= support:
        return None

    # La vela de rechazo toca el soporte y termina por encima.
    rejection_low = float(rejection["low"])
    rejection_close = float(rejection["close"])
    if rejection_low > support * (1 + ZONE_TOLERANCE):
        return None
    if rejection_close <= support:
        return None

    # La vela siguiente debe terminar verde.
    confirmation = data.iloc[-1]
    if _direction(confirmation) != "bullish":
        return None

    return {
        "signal": "call",
        "swing_index": int(swing_idx),
        "level": float(support),
        "rejection_index": int(rejection_idx),
        "confirmation_index": int(len(data) - 1),
        "rejection_close": rejection_close,
        "rejection_low": rejection_low,
        "confirmation_close": float(confirmation["close"]),
    }


def analyze_market(
    df: Optional[pd.DataFrame] = None,
    pair: Optional[str] = None,
    mode: str = "M1_M1",
    higher_tf_df: Optional[pd.DataFrame] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    mode = "M1_M1"
    data = _normalize(df)

    if len(data) < MIN_BARS:
        return _empty(f"historial M1 insuficiente {len(data)}/{MIN_BARS}", mode)

    higher = _normalize(higher_tf_df)
    m15_trend = _m15_direction(higher)

    # La imagen define la señal solo por el patron de nivel + recorrido +
    # regreso + rechazo + vela siguiente. No se agrega otro filtro a la señal.
    put = _put_pattern(data)
    call = _call_pattern(data)

    pattern = put or call
    if pattern is None:
        return {
            **_empty("patron de la imagen no confirmado", mode),
            "higher_trend": m15_trend,
            "analysis": {
                "pair": pair,
                "m15_trend": m15_trend,
            },
        }

    signal = pattern["signal"]
    level_name = "RESISTENCIA" if signal == "put" else "SOPORTE"
    rejection_color = "bajista" if signal == "put" else "alcista"
    confirmation_color = "roja" if signal == "put" else "verde"

    reason = (
        f"{signal.upper()} | ultimo nivel {level_name} | "
        f"recorrido y regreso | rechazo {rejection_color} "
        f"cerrado {'debajo' if signal == 'put' else 'encima'} del nivel | "
        f"siguiente vela {confirmation_color} | expiracion 1m"
    )

    return {
        "signal": signal,
        "direction": signal,
        "trend": m15_trend,
        "higher_trend": m15_trend,
        "reason": reason,
        "score": 0,
        "blocked": False,
        "mode": mode,
        "analysis_timeframe": "M1",
        "entry_timeframe": "M1",
        "target_expiration_minutes": 1,
        "analysis": {
            "pair": pair,
            "m15_trend": m15_trend,
            "pattern": pattern,
        },
    }


def get_signal(df: pd.DataFrame) -> Optional[str]:
    return analyze_market(df=df, mode="M1_M1").get("signal")


def signal(df: pd.DataFrame) -> Optional[str]:
    return get_signal(df)
