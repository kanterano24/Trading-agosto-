"""strategy.py - Señales ATR + confirmación M5.

Lógica:
- El análisis se realiza en M1.
- CALL: el precio cruza la línea ATR hacia abajo.
- PUT: el precio cruza la línea ATR hacia arriba.
- El cruce debe producirse dentro de una ventana de 5 minutos.
- Se abre la siguiente vela M5 y se espera a su cierre.
- CALL solamente si esa M5 cierra roja.
- PUT solamente si esa M5 cierra verde.
- Si la confirmación no coincide, no hay operación.

Nota:
La línea ATR implementada aquí es una línea ATR trailing-stop:
    ATR line = precio de referencia +/- ATR * multiplicador
con periodo 14 y multiplicador 1.0.
Si el indicador de tu gráfico usa otro periodo/multiplicador, cambia
ATR_PERIOD y ATR_MULTIPLIER para hacer coincidir la línea.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math
from datetime import datetime, timezone


MIN_CANDLES = 20
DEFAULT_LOOKBACK = 60
DEFAULT_MIN_SCORE = 75

# ==========================================================
# ATR TRAILING LINE
# ==========================================================

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.0

# Ventana máxima en la que aceptamos el cruce para preparar
# la siguiente vela M5.
CROSS_WINDOW_MINUTES = 5

# Estado de señales pendientes, separado por par.
# Se conserva entre llamadas de analyze_market().
_PENDING: Dict[str, Dict[str, Any]] = {}


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    support: Optional[float] = None
    resistance: Optional[float] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _ohlc(candle: Dict[str, Any]) -> Tuple[float, float, float, float]:
    open_price = _number(candle.get("open", candle.get("open_price")))
    close_price = _number(candle.get("close", candle.get("close_price")))
    low = _number(candle.get("min", candle.get("low")))
    high = _number(candle.get("max", candle.get("high")))
    return open_price, close_price, low, high


def _as_records(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []

    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
            return [dict(item) for item in records]
        except (TypeError, ValueError):
            return []

    if isinstance(value, dict):
        return [dict(value)]

    try:
        return [dict(item) for item in value if isinstance(item, dict)]
    except TypeError:
        return []


def _timestamp(candle: Dict[str, Any]) -> Any:
    return candle.get("from", candle.get("timestamp", candle.get("time")))


def _timestamp_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        # Epoch milliseconds.
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        return number

    text = str(value).strip()
    if not text:
        return None

    try:
        number = float(text)
        if math.isfinite(number):
            if abs(number) > 10_000_000_000:
                number /= 1000.0
            return number
    except ValueError:
        pass

    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _m5_bucket(timestamp_value: Any) -> Optional[int]:
    seconds = _timestamp_seconds(timestamp_value)
    if seconds is None:
        return None
    return int(seconds // 300)


def _atr_values(
    candles: List[Dict[str, Any]],
    period: int = ATR_PERIOD,
) -> List[float]:
    """ATR Wilder-style/simple rolling mean of True Range."""
    if len(candles) < 2:
        return []

    true_ranges: List[float] = []

    for index in range(1, len(candles)):
        _, previous_close, _, _ = _ohlc(candles[index - 1])
        _, _, low, high = _ohlc(candles[index])

        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )

    result: List[float] = []

    for i in range(len(true_ranges)):
        values = true_ranges[max(0, i - period + 1): i + 1]
        result.append(sum(values) / len(values) if values else 0.0)

    return result


def _atr(candles: List[Dict[str, Any]], period: int = ATR_PERIOD) -> float:
    values = _atr_values(candles, period)
    return values[-1] if values else 0.0


def _atr_trailing_line(
    candles: List[Dict[str, Any]],
    period: int = ATR_PERIOD,
    multiplier: float = ATR_MULTIPLIER,
) -> List[float]:
    """
    Calcula una línea ATR trailing-stop.

    La línea se mueve debajo del precio cuando la tendencia es alcista
    y encima del precio cuando la tendencia es bajista.

    Esta es la línea contra la que se detecta el cruce del precio.
    """
    if len(candles) < 2:
        return []

    atr_values = _atr_values(candles, period)
    lines: List[float] = []

    previous_line = 0.0
    direction = 1  # 1 = línea inferior, -1 = línea superior

    for i, candle in enumerate(candles):
        open_price, close_price, low, high = _ohlc(candle)

        atr_value = (
            atr_values[i - 1]
            if i - 1 < len(atr_values)
            else 0.0
        )

        if atr_value <= 0:
            if i == 0:
                lines.append(close_price)
            else:
                lines.append(previous_line)
            continue

        # Línea ATR basada en el cierre.
        long_stop = close_price - multiplier * atr_value
        short_stop = close_price + multiplier * atr_value

        if i == 1 or previous_line == 0:
            direction = 1
            line = long_stop
        elif direction == 1:
            # Mientras siga por encima de la línea inferior,
            # la línea solo puede subir.
            line = max(previous_line, long_stop)

            # Cruce hacia abajo de la línea: cambia a línea superior.
            if close_price < previous_line:
                direction = -1
                line = short_stop
        else:
            # Mientras siga por debajo de la línea superior,
            # la línea solo puede bajar.
            line = min(previous_line, short_stop)

            # Cruce hacia arriba de la línea: cambia a línea inferior.
            if close_price > previous_line:
                direction = 1
                line = long_stop

        previous_line = line
        lines.append(line)

    return lines


def _detect_atr_cross(
    candles: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Detecta el cruce usando las dos últimas M1 cerradas.

    CALL:
        precio cruza la línea ATR hacia abajo.

    PUT:
        precio cruza la línea ATR hacia arriba.
    """
    if len(candles) < ATR_PERIOD + 3:
        return None

    lines = _atr_trailing_line(candles)

    if len(lines) < 2:
        return None

    previous = candles[-2]
    current = candles[-1]

    previous_close = _ohlc(previous)[1]
    current_close = _ohlc(current)[1]

    previous_line = lines[-2]
    current_line = lines[-1]

    if previous_line <= 0 or current_line <= 0:
        return None

    # Cruce hacia abajo:
    # el cierre anterior estaba por encima/en la línea
    # y el cierre actual termina por debajo.
    cross_down = (
        previous_close >= previous_line
        and current_close < current_line
    )

    # Cruce hacia arriba:
    # el cierre anterior estaba por debajo/en la línea
    # y el cierre actual termina por encima.
    cross_up = (
        previous_close <= previous_line
        and current_close > current_line
    )

    if not cross_down and not cross_up:
        return None

    direction = "call" if cross_down else "put"

    return {
        "direction": direction,
        "timestamp": _timestamp(current),
        "timestamp_seconds": _timestamp_seconds(_timestamp(current)),
        "m5_bucket": _m5_bucket(_timestamp(current)),
        "price": current_close,
        "atr": _atr(candles),
        "atr_line": current_line,
        "previous_atr_line": previous_line,
    }


def _zones(
    candles: List[Dict[str, Any]],
    lookback: int = DEFAULT_LOOKBACK,
) -> Tuple[float, float]:
    sample = candles[-max(1, lookback):]

    lows = [_ohlc(candle)[2] for candle in sample]
    highs = [_ohlc(candle)[3] for candle in sample]

    return (
        (min(lows), max(highs))
        if lows and highs
        else (0.0, 0.0)
    )


def _completed_m5(
    records: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Devuelve la última vela M5 COMPLETAMENTE cerrada a partir de M1.

    Se agrupan las M1 por bloques de 5 minutos.
    """
    if len(records) < 5:
        return None

    buckets: Dict[int, List[Dict[str, Any]]] = {}

    for candle in records:
        bucket = _m5_bucket(_timestamp(candle))
        if bucket is None:
            continue
        buckets.setdefault(bucket, []).append(candle)

    if not buckets:
        return None

    complete: List[Tuple[int, List[Dict[str, Any]]]] = []

    for bucket, group in buckets.items():
        group = sorted(
            group,
            key=lambda x: (
                _timestamp_seconds(_timestamp(x))
                if _timestamp_seconds(_timestamp(x)) is not None
                else 0
            ),
        )

        if len(group) >= 5:
            complete.append((bucket, group[-5:]))

    if not complete:
        return None

    bucket, group = sorted(complete, key=lambda x: x[0])[-1]

    first_open = _ohlc(group[0])[0]
    last_close = _ohlc(group[-1])[1]
    low = min(_ohlc(c)[2] for c in group)
    high = max(_ohlc(c)[3] for c in group)

    return {
        "bucket": bucket,
        "open": first_open,
        "close": last_close,
        "low": low,
        "high": high,
        "red": last_close < first_open,
        "green": last_close > first_open,
        "candles": group,
        "timestamp": _timestamp(group[-1]),
    }


def _empty_result(reason: str = "no_valid_atr_signal") -> Dict[str, Any]:
    return {
        "signal": None,
        "score": 0,
        "score_100": 0,
        "entry_quality": 0,
        "quality": 0,
        "entry_type": "none",
        "direction": "range",
        "reason": reason,
        "analysis": {
            "force": False,
            "structure": "range",
            "atr": 0.0,
            "atr_line": 0.0,
            "support": None,
            "resistance": None,
            "tolerance": 0.0,
            "entry_quality": 0,
            "atr_cross": None,
            "pending_confirmation": False,
            "confirmation_m5": None,
        },
    }


def _pair_key(pair: Optional[str]) -> str:
    return str(pair or "__default__").upper()


def _store_cross(
    pair: str,
    cross: Dict[str, Any],
) -> None:
    bucket = cross.get("m5_bucket")

    _PENDING[pair] = {
        **cross,
        "created_bucket": bucket,
        "created_seconds": cross.get("timestamp_seconds"),
    }


def _pending_is_valid(
    pending: Dict[str, Any],
    current_bucket: Optional[int],
) -> bool:
    if current_bucket is None:
        return False

    created_bucket = pending.get("created_bucket")

    if created_bucket is None:
        return False

    # El cruce debe pertenecer al bloque de 5 minutos inmediatamente
    # anterior al bloque M5 que vamos a confirmar.
    return current_bucket == created_bucket + 1


def analyze_rejection(
    candles: Any,
    min_score: int = DEFAULT_MIN_SCORE,
    lookback: int = DEFAULT_LOOKBACK,
    zone_atr_factor: float = 0.35,
    stochastic_k: float = 50.0,
    stochastic_d: float = 50.0,
) -> Signal:
    """
    Compatibilidad con el nombre antiguo.

    La señal ya no utiliza Stochastic ni soporte/resistencia.
    """
    records = _as_records(candles)

    if len(records) < ATR_PERIOD + 3:
        return Signal("none", 0, "insufficient_candles")

    cross = _detect_atr_cross(records)

    if cross is None:
        return Signal("none", 0, "no_atr_cross")

    return Signal(
        cross["direction"],
        90,
        (
            "atr_cross_down_pending_m5_confirmation"
            if cross["direction"] == "call"
            else "atr_cross_up_pending_m5_confirmation"
        ),
    )


def analyze_market(
    candle_1m: Any = None,
    previous_m1: Any = None,
    pair: Optional[str] = None,
    df: Any = None,
    min_score: int = DEFAULT_MIN_SCORE,
    **_: Any,
) -> Dict[str, Any]:

    # ----------------------------------------------------------
    # Construcción de las M1 cerradas disponibles.
    # ----------------------------------------------------------

    if previous_m1 is not None:
        records = _as_records(previous_m1)

        if candle_1m is not None:
            current = (
                dict(candle_1m)
                if isinstance(candle_1m, dict)
                else {}
            )
            if current:
                records.append(current)

    elif df is not None:
        all_records = _as_records(df)

        # Mantiene el comportamiento habitual: si la última vela
        # puede estar abierta, no se utiliza como cerrada.
        records = (
            all_records[:-1]
            if len(all_records) > 1
            else []
        )

    else:
        all_records = _as_records(candle_1m)

        records = (
            all_records[:-1]
            if len(all_records) > 1
            else []
        )

    result = _empty_result()

    if len(records) < ATR_PERIOD + 5:
        result["reason"] = "insufficient_candles"
        return result

    pair_key = _pair_key(pair)

    # ----------------------------------------------------------
    # 1. Detectar el cruce ATR en M1.
    # ----------------------------------------------------------

    cross = _detect_atr_cross(records)

    if cross is not None:
        _store_cross(pair_key, cross)

    pending = _PENDING.get(pair_key)

    # ----------------------------------------------------------
    # 2. Detectar la M5 cerrada.
    # ----------------------------------------------------------

    m5 = _completed_m5(records)

    if m5 is None:
        result["reason"] = (
            "atr_cross_detected_waiting_m5_close"
            if pending
            else "waiting_m5_confirmation"
        )

        if pending:
            result["analysis"]["pending_confirmation"] = True
            result["analysis"]["atr_cross"] = pending

        return result

    current_bucket = m5["bucket"]

    # ----------------------------------------------------------
    # 3. Confirmar solamente la M5 que abre después del cruce.
    # ----------------------------------------------------------

    if pending is not None and _pending_is_valid(
        pending,
        current_bucket,
    ):
        direction = pending["direction"]

        call_confirmed = (
            direction == "call"
            and m5["red"]
        )

        put_confirmed = (
            direction == "put"
            and m5["green"]
        )

        confirmed = call_confirmed or put_confirmed

        if confirmed:
            score = 90

            timestamp = _timestamp(records[-1])

            reason = (
                "atr_cross_down_m1_confirmed_m5_red"
                if direction == "call"
                else "atr_cross_up_m1_confirmed_m5_green"
            )

            # La señal se consume: un mismo cruce no puede
            # generar dos operaciones.
            _PENDING.pop(pair_key, None)

            analysis = {
                "force": True,
                "structure": (
                    "bullish"
                    if direction == "call"
                    else "bearish"
                ),
                "atr": pending.get("atr", 0.0),
                "atr_line": pending.get("atr_line", 0.0),
                "support": None,
                "resistance": None,
                "tolerance": 0.0,
                "entry_quality": score,
                "atr_cross": pending,
                "pending_confirmation": False,
                "confirmation_m5": {
                    "bucket": m5["bucket"],
                    "open": m5["open"],
                    "close": m5["close"],
                    "red": m5["red"],
                    "green": m5["green"],
                    "timestamp": m5["timestamp"],
                },
                "rejection_timestamp": pending.get("timestamp"),
                "confirmation_timestamp": m5["timestamp"],
                "rejection_candle": (
                    records[-1]
                    if records
                    else None
                ),
                "confirmation_candle": m5["candles"][-1],
            }

            return {
                "signal": direction,
                "score": score,
                "score_100": score,
                "entry_quality": score,
                "quality": score,
                "entry_type": "force",
                "direction": (
                    "bullish"
                    if direction == "call"
                    else "bearish"
                ),
                "reason": reason,
                "analysis": analysis,
                "candle_timestamp": timestamp,
                "pair": pair,
            }

        # La M5 cerró con el color contrario:
        # se descarta el cruce, NO se invierte la operación.
        _PENDING.pop(pair_key, None)

        result["reason"] = (
            "atr_cross_call_m5_not_red_no_trade"
            if direction == "call"
            else "atr_cross_put_m5_not_green_no_trade"
        )
        result["analysis"]["pending_confirmation"] = False
        result["analysis"]["atr_cross"] = pending
        result["analysis"]["confirmation_m5"] = {
            "bucket": m5["bucket"],
            "open": m5["open"],
            "close": m5["close"],
            "red": m5["red"],
            "green": m5["green"],
        }
        return result

    # ----------------------------------------------------------
    # 4. Si la señal pendiente quedó vieja, se elimina.
    # ----------------------------------------------------------

    if pending is not None:
        created_bucket = pending.get("created_bucket")

        if (
            created_bucket is not None
            and current_bucket > created_bucket + 1
        ):
            _PENDING.pop(pair_key, None)

    result["reason"] = (
        "atr_cross_detected_waiting_next_m5"
        if cross is not None
        else "no_valid_atr_signal"
    )

    if pending is not None:
        result["analysis"]["pending_confirmation"] = True
        result["analysis"]["atr_cross"] = pending

    return result


def get_signal(df: Any) -> Optional[str]:
    return analyze_market(df=df).get("signal")


def signal(df: Any) -> Optional[str]:
    return get_signal(df)
