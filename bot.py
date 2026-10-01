from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from iqoptionapi.stable_api import IQ_Option

from strategy import M1, WINDOW

# ============================================================
# CONFIGURACION
# ============================================================
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Unico par solicitado para esta etapa.
PAIR = os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip() or "ARBUSD-OTC"
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "30"))
LOOP_SLEEP = float(os.getenv("LOOP_SLEEP", "0.10"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "10"))

# ============================================================
# IQ OPTION - DIGITAL DESACTIVADO
# ============================================================
try:
    IQ_Option.get_digital_underlying_list_data = lambda self: {"underlying": []}
except Exception:
    pass

# ============================================================
# ESTADO
# ============================================================
IQ: Optional[IQ_Option] = None
RUNNING = True
STREAM_STARTED = False
STATE_LOCK = threading.Lock()

# Vela M1 que estamos observando en tiempo real.
CURRENT_CANDLE: Optional[Dict[str, Any]] = None
LAST_CLOSED_TIMESTAMP: Optional[int] = None
LAST_10: list[Dict[str, Any]] = []

# ============================================================
# TELEGRAM
# ============================================================
def telegram_url(method: str = "sendMessage") -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return False
    try:
        response = requests.post(
            telegram_url(),
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=TELEGRAM_TIMEOUT,
        )
        if not response.ok:
            print(f"Telegram HTTP {response.status_code}: {response.text[:300]}")
        return bool(response.ok)
    except Exception as exc:
        print(f"Telegram error: {exc}")
        return False


def fmt(value: float, digits: int = 8) -> str:
    return f"{float(value):.{digits}f}"


def pct(value: float) -> str:
    return f"{float(value):.2f}%"


def fmt_time(ts: Optional[int]) -> str:
    if ts is None:
        return "N/D"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ============================================================
# VELA INTRAMINUTO
# ============================================================
def new_candle(bucket: int, opening: float) -> Dict[str, Any]:
    opening = float(opening)
    return {
        "timestamp": int(bucket),
        "open": opening,
        "high": opening,
        "low": opening,
        "close": opening,
        "first_price": opening,
        "last_price": opening,
        "tick_count": 0,
        "max_time": None,
        "min_time": None,
        "max_price": opening,
        "min_price": opening,
        "path_distance": 0.0,
        "up_moves": 0,
        "down_moves": 0,
        "flat_moves": 0,
        "up_distance": 0.0,
        "down_distance": 0.0,
        "quarter_prices": {},
        "last_update_ts": int(bucket),
    }


def update_candle(candle: Dict[str, Any], ts: int, price: float) -> None:
    """Acumula cada snapshot disponible del stream durante la M1."""
    price = float(price)
    ts = int(ts)
    previous = float(candle["last_price"])

    candle["tick_count"] += 1
    candle["last_price"] = price
    candle["close"] = price
    candle["high"] = max(float(candle["high"]), price)
    candle["low"] = min(float(candle["low"]), price)

    delta = price - previous
    candle["path_distance"] += abs(delta)
    if delta > 0:
        candle["up_moves"] += 1
        candle["up_distance"] += delta
    elif delta < 0:
        candle["down_moves"] += 1
        candle["down_distance"] += abs(delta)
    else:
        candle["flat_moves"] += 1

    if price >= float(candle["max_price"]):
        candle["max_price"] = price
        candle["max_time"] = ts
    if price <= float(candle["min_price"]):
        candle["min_price"] = price
        candle["min_time"] = ts

    candle["last_update_ts"] = ts

    # Aproximaciones de 25%, 50% y 75% del minuto basadas en timestamp.
    start = int(candle["timestamp"])
    elapsed = max(0, min(M1 - 1, ts - start))
    quarter = 25 if elapsed >= 45 else 50 if elapsed >= 30 else 25 if elapsed >= 15 else 0
    if quarter:
        candle["quarter_prices"][quarter] = price


def finalize_candle(candle: Dict[str, Any]) -> Dict[str, Any]:
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    close = float(candle["close"])

    # Aseguramos que OHLC incluya cualquier max/min que haya entregado el stream.
    h = max(h, float(candle.get("max_price", h)))
    l = min(l, float(candle.get("min_price", l)))

    body = abs(close - o)
    total = max(h - l, 0.0)
    upper = max(0.0, h - max(o, close))
    lower = max(0.0, min(o, close) - l)
    color = "VERDE" if close > o else "ROJA" if close < o else "DOJI"

    return {
        **candle,
        "high": h,
        "low": l,
        "close": close,
        "body": body,
        "range": total,
        "upper_wick": upper,
        "lower_wick": lower,
        "color": color,
        "body_pct": body / total * 100 if total else 0.0,
        "upper_wick_pct": upper / total * 100 if total else 0.0,
        "lower_wick_pct": lower / total * 100 if total else 0.0,
        "max_from_open": h - o,
        "min_from_open": o - l,
        "close_vs_open": close - o,
        "close_vs_high": close - h,
        "close_vs_low": close - l,
    }

# ============================================================
# DATOS DEL STREAM
# ============================================================
def server_timestamp() -> int:
    try:
        return int(IQ.get_server_timestamp())
    except Exception:
        return int(time.time())


def start_stream() -> None:
    global STREAM_STARTED
    if STREAM_STARTED:
        return
    IQ.start_candles_stream(PAIR, M1, CANDLE_COUNT_M1)
    STREAM_STARTED = True
    print(f"Stream M1 iniciado: {PAIR}")


def stop_stream() -> None:
    global STREAM_STARTED
    if not STREAM_STARTED:
        return
    try:
        IQ.stop_candles_stream(PAIR, M1)
    except Exception:
        pass
    STREAM_STARTED = False


def candle_timestamp(data: Dict[str, Any], fallback: int) -> Optional[int]:
    try:
        return int(data.get("from", fallback))
    except Exception:
        return None


def candle_bucket(ts: int) -> int:
    return int(ts) - (int(ts) % M1)


def extract_price(data: Dict[str, Any]) -> Optional[float]:
    # En realtime_candles, close suele ser el precio actualizado de la vela.
    for key in ("close", "price", "ask", "bid"):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def extract_open(data: Dict[str, Any], fallback: float) -> float:
    for key in ("open", "open_price"):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return float(fallback)


def add_closed_candle(closed: Dict[str, Any]) -> None:
    global LAST_CLOSED_TIMESTAMP, LAST_10

    ts = int(closed["timestamp"])
    if LAST_CLOSED_TIMESTAMP == ts:
        return

    LAST_CLOSED_TIMESTAMP = ts
    LAST_10.append(closed)
    if len(LAST_10) > WINDOW:
        LAST_10 = LAST_10[-WINDOW:]

    # Un mensaje completo de la vela cerrada.
    send_telegram(candle_report(closed))

    # Las últimas 10 velas se envían cuando ya existe una ventana completa.
    if len(LAST_10) == WINDOW:
        send_telegram(window_report(LAST_10))

# ============================================================
# REPORTES TELEGRAM
# ============================================================
def candle_report(c: Dict[str, Any]) -> str:
    tick_total = max(int(c.get("tick_count", 0)), 1)
    up_pct = c["up_moves"] / tick_total * 100
    down_pct = c["down_moves"] / tick_total * 100
    direction = (
        "ALCISTA"
        if c["up_distance"] > c["down_distance"]
        else "BAJISTA"
        if c["down_distance"] > c["up_distance"]
        else "MIXTO"
    )

    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {PAIR}\n"
        f"Timestamp: {int(c['timestamp'])}\n"
        f"Inicio: {fmt_time(int(c['timestamp']))}\n"
        f"Color: {c['color']}\n\n"
        "💰 OHLC + ANATOMÍA\n"
        f"Apertura: {fmt(c['open'])}\n"
        f"Máximo: {fmt(c['high'])}\n"
        f"Mínimo: {fmt(c['low'])}\n"
        f"Cierre: {fmt(c['close'])}\n"
        f"Cuerpo: {fmt(c['body'])}\n"
        f"Mecha inferior: {fmt(c['lower_wick'])}\n"
        f"Mecha superior: {fmt(c['upper_wick'])}\n"
        f"Rango total: {fmt(c['range'])}\n\n"
        "📐 PROPORCIONES\n"
        f"Cuerpo/Rango: {pct(c['body_pct'])}\n"
        f"MI/Rango: {pct(c['lower_wick_pct'])}\n"
        f"MS/Rango: {pct(c['upper_wick_pct'])}\n\n"
        "📈 MOVIMIENTO DURANTE EL MINUTO\n"
        f"Máximo desde apertura: +{fmt(c['max_from_open'])}\n"
        f"Mínimo desde apertura: -{fmt(c['min_from_open'])}\n"
        f"Recorrido acumulado: {fmt(c['path_distance'])}\n"
        f"Distancia alcista: {fmt(c['up_distance'])}\n"
        f"Distancia bajista: {fmt(c['down_distance'])}\n"
        f"Balance del recorrido: {fmt(c['up_distance'] - c['down_distance'])}\n"
        f"Dirección por recorrido: {direction}\n\n"
        "🔢 ACTUALIZACIONES DEL STREAM\n"
        f"Actualizaciones recibidas: {c['tick_count']}\n"
        f"Movimientos ↑: {c['up_moves']} ({up_pct:.1f}%)\n"
        f"Movimientos ↓: {c['down_moves']} ({down_pct:.1f}%)\n"
        f"Movimientos =: {c['flat_moves']}\n\n"
        "⏱️ EXTREMOS\n"
        f"Hora máximo: {fmt_time(c.get('max_time'))}\n"
        f"Hora mínimo: {fmt_time(c.get('min_time'))}\n"
        f"Cierre vs apertura: {fmt(c['close_vs_open'])}\n"
        f"Cierre debajo del máximo: {fmt(abs(c['close_vs_high']))}\n"
        f"Cierre encima del mínimo: {fmt(abs(c['close_vs_low']))}\n\n"
        "Solo precio. Sin indicadores, S/R ni rechazo.\n"
        "Operaciones DESACTIVADAS."
    )


def window_report(window: list[Dict[str, Any]]) -> str:
    seq = " ".join(
        "V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D"
        for x in window
    )
    lines = [
        f"📊 ÚLTIMAS {len(window)} VELAS M1",
        "",
        f"Par: {PAIR}",
        f"Secuencia: {seq}",
        "",
    ]
    for i, c in enumerate(window, 1):
        lines.append(
            f"{i:02d} {'V' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D'} | "
            f"O={fmt(c['open'])} | H={fmt(c['high'])} | L={fmt(c['low'])} | "
            f"C={fmt(c['close'])} | MI={fmt(c['lower_wick'])} | MS={fmt(c['upper_wick'])} | "
            f"Body={fmt(c['body'])} | R={fmt(c['range'])} | Body/R={pct(c['body_pct'])}"
        )
    lines.extend([
        "",
        "Estas 10 velas se conservan para estudiar qué características preceden a la siguiente vela.",
        "Operaciones DESACTIVADAS.",
    ])
    return "\n".join(lines)

# ============================================================
# PROCESAMIENTO ROBUSTO DEL STREAM
# ============================================================
def process_stream() -> None:
    global CURRENT_CANDLE

    raw = IQ.get_realtime_candles(PAIR, M1) or {}
    if not raw:
        return

    # Elegimos solamente la vela más reciente del stream.
    candidates: list[tuple[int, Dict[str, Any]]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        ts = candle_timestamp(value, int(key) if str(key).isdigit() else 0)
        if ts is None:
            continue
        candidates.append((candle_bucket(ts), value))

    if not candidates:
        return

    bucket, data = max(candidates, key=lambda x: x[0])
    price = extract_price(data)
    if price is None:
        return

    opening = extract_open(data, price)
    now_bucket = candle_bucket(server_timestamp())

    # En el arranque, NO enviamos historial. Solo comenzamos a observar la M1 actual.
    if CURRENT_CANDLE is None:
        if bucket < now_bucket:
            return
        CURRENT_CANDLE = new_candle(bucket, opening)
        # Incorporamos el OHLC disponible del stream sin inventar recorrido.
        CURRENT_CANDLE["high"] = max(float(CURRENT_CANDLE["high"]), float(data.get("max", data.get("high", price))))
        CURRENT_CANDLE["low"] = min(float(CURRENT_CANDLE["low"]), float(data.get("min", data.get("low", price))))
        update_candle(CURRENT_CANDLE, bucket, price)
        return

    # Si llegamos a una M1 posterior, cerramos exactamente la que estábamos siguiendo.
    if bucket > int(CURRENT_CANDLE["timestamp"]):
        closed = finalize_candle(CURRENT_CANDLE)
        add_closed_candle(closed)

        CURRENT_CANDLE = new_candle(bucket, opening)
        CURRENT_CANDLE["high"] = max(float(CURRENT_CANDLE["high"]), float(data.get("max", data.get("high", price))))
        CURRENT_CANDLE["low"] = min(float(CURRENT_CANDLE["low"]), float(data.get("min", data.get("low", price))))
        update_candle(CURRENT_CANDLE, bucket, price)
        return

    # Datos atrasados del stream: no retrocedemos de vela.
    if bucket < int(CURRENT_CANDLE["timestamp"]):
        return

    # Misma M1: acumulamos el nuevo snapshot.
    update_candle(CURRENT_CANDLE, bucket, price)

# ============================================================
# LOGIN
# ============================================================
def connect_iq() -> None:
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD en las variables de entorno.")

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")

    try:
        IQ.change_balance("PRACTICE")
    except Exception:
        pass

    print("Conectado a IQ Option.")
    print(f"Par configurado: {PAIR}")

# ============================================================
# TELEGRAM COMMANDS
# ============================================================
def telegram_get_updates(offset: Optional[int] = None):
    if not TELEGRAM_TOKEN:
        return []
    try:
        params: Dict[str, Any] = {"timeout": 1}
        if offset is not None:
            params["offset"] = offset
        response = requests.get(
            telegram_url("getUpdates"),
            params=params,
            timeout=TELEGRAM_TIMEOUT,
        )
        return response.json().get("result", []) if response.ok else []
    except Exception:
        return []


def telegram_commands_loop() -> None:
    global RUNNING
    offset: Optional[int] = None

    while True:
        for update in telegram_get_updates(offset):
            try:
                offset = int(update["update_id"]) + 1
            except Exception:
                continue

            message = update.get("message", {})
            text = str(message.get("text", "")).strip().lower()

            if text == "/start":
                RUNNING = True
                send_telegram("▶️ Recolección M1 ACTIVADA. Operaciones siguen DESACTIVADAS.")
            elif text == "/stop":
                RUNNING = False
                send_telegram("⏸️ Recolección M1 PAUSADA. Operaciones siguen DESACTIVADAS.")
            elif text == "/status":
                send_telegram(
                    f"📊 STATUS\nPar: {PAIR}\n"
                    f"Recolección: {'ACTIVA' if RUNNING else 'PAUSADA'}\n"
                    f"Velas almacenadas: {len(LAST_10)}/{WINDOW}\n"
                    "Operaciones: DESACTIVADAS"
                )

        time.sleep(0.5)

# ============================================================
# MAIN
# ============================================================
def main() -> None:
    connect_iq()
    start_stream()

    if TELEGRAM_TOKEN:
        threading.Thread(target=telegram_commands_loop, daemon=True).start()
        send_telegram(
            "🟢 QUANT MODE INICIADO\n\n"
            f"Par: {PAIR}\n"
            "Timeframe: M1\n"
            "Modo: RECOLECCIÓN DE DATOS\n\n"
            "Se registrará el movimiento disponible de cada vela durante todo el minuto.\n"
            "Operaciones: DESACTIVADAS."
        )

    try:
        while True:
            if RUNNING:
                try:
                    process_stream()
                except Exception as exc:
                    print(f"Stream error: {exc}")
            time.sleep(LOOP_SLEEP)
    except KeyboardInterrupt:
        print("Detenido por usuario.")
    finally:
        stop_stream()


if __name__ == "__main__":
    main()
