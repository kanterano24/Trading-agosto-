from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from strategy import M1, WINDOW

# ============================================================
# QUANT MODE - SOLO RECOLECCION
# ============================================================
# Un solo par M1. No ejecuta CALL/PUT y no usa indicadores/SR/rechazo.

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REQUESTED_PAIR = os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip() or "ARBUSD-OTC"
PAIR = REQUESTED_PAIR
CANDLE_COUNT_M1 = max(int(os.getenv("CANDLE_COUNT_M1", "30")), 10)
LOOP_SLEEP = max(float(os.getenv("LOOP_SLEEP", "0.20")), 0.05)
TELEGRAM_TIMEOUT = max(float(os.getenv("TELEGRAM_TIMEOUT", "10")), 3.0)
PAIR_RETRY_SECONDS = max(float(os.getenv("PAIR_RETRY_SECONDS", "15")), 5.0)
STREAM_RETRY_SECONDS = max(float(os.getenv("STREAM_RETRY_SECONDS", "5")), 2.0)

# ============================================================
# DESACTIVAR DIGITAL
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

CURRENT_CANDLE: Optional[Dict[str, Any]] = None
LAST_CLOSED_TIMESTAMP: Optional[int] = None
LAST_10: list[Dict[str, Any]] = []
LAST_STREAM_ERROR_AT = 0.0

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
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "N/D"


def pct(value: float) -> str:
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "N/D"


def fmt_time(ts: Optional[int]) -> str:
    if ts is None:
        return "N/D"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ============================================================
# CATALOGO DE ACTIVOS
# ============================================================
def is_otc(name: str) -> bool:
    n = str(name).upper().strip()
    return n.endswith("-OTC") or n.endswith("_OTC") or "OTC" in n


def normalize_asset_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    value = name.strip()
    # Algunas respuestas pueden traer prefijos tipo "binary.AUDUSD-OTC".
    if "." in value:
        value = value.split(".")[-1].strip()
    return value


def scan_asset_records(obj: Any, found: Dict[str, int]) -> None:
    """Busca recursivamente registros {id/name} en get_all_init_v2()."""
    if isinstance(obj, dict):
        # Caso habitual: {active_id: {name: "AUDUSD-OTC", ...}}
        name = normalize_asset_name(obj.get("name"))
        if name and is_otc(name):
            possible_ids = (
                obj.get("active_id"),
                obj.get("id"),
                obj.get("asset_id"),
            )
            for candidate in possible_ids:
                try:
                    found[name] = int(candidate)
                    break
                except (TypeError, ValueError):
                    pass

        # Si el propio dict usa el ID como clave.
        if name and is_otc(name):
            for key, value in obj.items():
                if key in {"name", "active_id", "id", "asset_id"}:
                    continue
                try:
                    key_id = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict) and normalize_asset_name(value.get("name")) == name:
                    found[name] = key_id

        for value in obj.values():
            scan_asset_records(value, found)

    elif isinstance(obj, (list, tuple)):
        for value in obj:
            scan_asset_records(value, found)


def load_otc_catalog() -> Dict[str, int]:
    """Carga el catálogo actual y sincroniza OP_code.ACTIVES."""
    if IQ is None:
        return {}

    try:
        data = IQ.get_all_init_v2()
    except Exception as exc:
        print(f"No se pudo leer catalogo OTC: {exc}")
        return {}

    found: Dict[str, int] = {}
    scan_asset_records(data, found)

    # Algunas versiones exponen los activos en atributos auxiliares.
    try:
        for name, active_id in getattr(OP_code, "ACTIVES", {}).items():
            normalized = normalize_asset_name(name)
            if is_otc(normalized):
                try:
                    found.setdefault(normalized, int(active_id))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    for name, active_id in found.items():
        try:
            OP_code.ACTIVES[name] = int(active_id)
        except Exception:
            pass

    return found


def resolve_pair() -> Tuple[bool, str]:
    """Resuelve el par solicitado sin permitir que un activo desconocido rompa el bot."""
    global PAIR

    catalog = load_otc_catalog()
    requested = normalize_asset_name(REQUESTED_PAIR)

    if requested in catalog:
        PAIR = requested
        print(f"OTC confirmado: {PAIR} | active_id={catalog[PAIR]}")
        return True, PAIR

    # Fallback seguro: si el par solicitado no aparece hoy, usamos un OTC
    # disponible para que el proceso no entre en un crash loop.
    if catalog:
        available = sorted(catalog)
        PAIR = available[0]
        msg = (
            "⚠️ PAR SOLICITADO NO DISPONIBLE\n\n"
            f"Solicitado: {requested}\n"
            f"Par usado temporalmente: {PAIR}\n"
            "Motivo: el catálogo actual de IQ Option no devolvió el par solicitado.\n"
            "Operaciones: DESACTIVADAS."
        )
        print(msg)
        send_telegram(msg)
        return True, PAIR

    print("No se encontraron activos OTC en el catalogo actual.")
    return False, requested

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
        "max_time": int(bucket),
        "min_time": int(bucket),
        "max_price": opening,
        "min_price": opening,
        "path_distance": 0.0,
        "up_moves": 0,
        "down_moves": 0,
        "flat_moves": 0,
        "up_distance": 0.0,
        "down_distance": 0.0,
        "sample_25": None,
        "sample_50": None,
        "sample_75": None,
        "last_update_ts": int(bucket),
    }


def update_sample(candle: Dict[str, Any], ts: int, price: float) -> None:
    start = int(candle["timestamp"])
    elapsed = max(0, int(ts) - start)
    if elapsed >= 15 and candle.get("sample_25") is None:
        candle["sample_25"] = float(price)
    if elapsed >= 30 and candle.get("sample_50") is None:
        candle["sample_50"] = float(price)
    if elapsed >= 45 and candle.get("sample_75") is None:
        candle["sample_75"] = float(price)


def update_candle(
    candle: Dict[str, Any],
    ts: int,
    price: float,
    stream_high: Optional[float] = None,
    stream_low: Optional[float] = None,
) -> None:
    """Acumula cada snapshot disponible del stream durante la M1."""
    price = float(price)
    ts = int(ts)
    previous = float(candle["last_price"])

    candle["tick_count"] += 1
    candle["last_price"] = price
    candle["close"] = price

    # El stream entrega max/min de la vela; no debemos perderlos.
    candle["high"] = max(float(candle["high"]), price)
    candle["low"] = min(float(candle["low"]), price)
    if stream_high is not None:
        candle["high"] = max(float(candle["high"]), float(stream_high))
    if stream_low is not None:
        candle["low"] = min(float(candle["low"]), float(stream_low))

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
    update_sample(candle, ts, price)


def finalize_candle(candle: Dict[str, Any]) -> Dict[str, Any]:
    o = float(candle["open"])
    h = max(float(candle["high"]), float(candle.get("max_price", o)))
    l = min(float(candle["low"]), float(candle.get("min_price", o)))
    close = float(candle["close"])

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
        "body_pct": body / total * 100.0 if total else 0.0,
        "upper_wick_pct": upper / total * 100.0 if total else 0.0,
        "lower_wick_pct": lower / total * 100.0 if total else 0.0,
        "max_from_open": h - o,
        "min_from_open": o - l,
        "close_vs_open": close - o,
        "close_vs_high": close - h,
        "close_vs_low": close - l,
    }

# ============================================================
# STREAM IQ OPTION
# ============================================================
def server_timestamp() -> int:
    try:
        return int(IQ.get_server_timestamp())
    except Exception:
        return int(time.time())


def candle_bucket(ts: int) -> int:
    return int(ts) - (int(ts) % M1)


def start_stream() -> bool:
    global STREAM_STARTED, LAST_STREAM_ERROR_AT

    if STREAM_STARTED:
        return True
    if IQ is None:
        return False

    # Evita llamar start_candles_stream con un activo que no existe en
    # OP_code.ACTIVES: la implementacion de iqoptionapi usa ese diccionario
    # internamente y puede terminar en "NoneType is not iterable".
    ok, _ = resolve_pair()
    if not ok:
        return False

    try:
        IQ.start_candles_stream(PAIR, M1, CANDLE_COUNT_M1)
        STREAM_STARTED = True
        print(f"Stream M1 iniciado correctamente: {PAIR}")
        return True
    except Exception as exc:
        STREAM_STARTED = False
        LAST_STREAM_ERROR_AT = time.time()
        print(f"ERROR iniciando stream {PAIR}: {type(exc).__name__}: {exc}")
        try:
            OP_code.ACTIVES.pop(PAIR, None)
        except Exception:
            pass
        return False


def stop_stream() -> None:
    global STREAM_STARTED
    if not STREAM_STARTED or IQ is None:
        return
    try:
        IQ.stop_candles_stream(PAIR, M1)
    except Exception as exc:
        print(f"Aviso al detener stream: {exc}")
    STREAM_STARTED = False


def extract_price(data: Dict[str, Any]) -> Optional[float]:
    for key in ("close", "price", "ask", "bid"):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
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


def extract_extreme(data: Dict[str, Any], key_a: str, key_b: str) -> Optional[float]:
    value = data.get(key_a, data.get(key_b))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

# ============================================================
# HISTORICO INICIAL
# ============================================================
def seed_last_10() -> None:
    """Carga hasta 10 velas cerradas como contexto inicial.

    Esto solo inicializa la ventana. La vela en tiempo real sigue viniendo del
    stream y no se usa get_candles para decidir entradas.
    """
    global LAST_10, LAST_CLOSED_TIMESTAMP

    if IQ is None:
        return

    try:
        data = IQ.get_candles(PAIR, M1, max(CANDLE_COUNT_M1, WINDOW + 2), server_timestamp())
    except Exception as exc:
        print(f"No se pudo cargar historico inicial: {exc}")
        return

    if not isinstance(data, list):
        return

    now_bucket = candle_bucket(server_timestamp())
    closed = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            ts = candle_bucket(int(row.get("from")))
            if ts >= now_bucket:
                continue
            o = float(row["open"])
            h = float(row.get("max", row.get("high")))
            l = float(row.get("min", row.get("low")))
            c = float(row["close"])
        except (TypeError, ValueError, KeyError):
            continue

        body = abs(c - o)
        rng = max(h - l, 0.0)
        upper = max(0.0, h - max(o, c))
        lower = max(0.0, min(o, c) - l)
        closed.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "color": "VERDE" if c > o else "ROJA" if c < o else "DOJI",
            "body": body,
            "range": rng,
            "upper_wick": upper,
            "lower_wick": lower,
            "body_pct": body / rng * 100.0 if rng else 0.0,
            "upper_wick_pct": upper / rng * 100.0 if rng else 0.0,
            "lower_wick_pct": lower / rng * 100.0 if rng else 0.0,
            "max_from_open": h - o,
            "min_from_open": o - l,
            "close_vs_open": c - o,
            "close_vs_high": c - h,
            "close_vs_low": c - l,
            "tick_count": 0,
            "up_moves": 0,
            "down_moves": 0,
            "flat_moves": 0,
            "up_distance": 0.0,
            "down_distance": 0.0,
            "path_distance": 0.0,
            "max_time": None,
            "min_time": None,
            "sample_25": None,
            "sample_50": None,
            "sample_75": None,
        })

    closed.sort(key=lambda x: x["timestamp"])
    LAST_10 = closed[-WINDOW:]
    if LAST_10:
        LAST_CLOSED_TIMESTAMP = LAST_10[-1]["timestamp"]
    print(f"Contexto inicial: {len(LAST_10)}/{WINDOW} velas cerradas")

# ============================================================
# REPORTES TELEGRAM
# ============================================================
def add_closed_candle(closed: Dict[str, Any]) -> None:
    global LAST_CLOSED_TIMESTAMP, LAST_10

    ts = int(closed["timestamp"])
    if LAST_CLOSED_TIMESTAMP is not None and ts <= LAST_CLOSED_TIMESTAMP:
        return

    LAST_CLOSED_TIMESTAMP = ts
    previous_window = list(LAST_10)

    LAST_10.append(closed)
    if len(LAST_10) > WINDOW:
        LAST_10 = LAST_10[-WINDOW:]

    send_telegram(candle_report(closed, previous_window))
    if len(LAST_10) == WINDOW:
        send_telegram(window_report(LAST_10))


def candle_report(c: Dict[str, Any], previous_window: list[Dict[str, Any]]) -> str:
    tick_total = max(int(c.get("tick_count", 0)), 1)
    up_pct = float(c.get("up_moves", 0)) / tick_total * 100.0
    down_pct = float(c.get("down_moves", 0)) / tick_total * 100.0
    direction = (
        "ALCISTA" if c["up_distance"] > c["down_distance"]
        else "BAJISTA" if c["down_distance"] > c["up_distance"]
        else "MIXTO"
    )
    prev_color = "N/D" if not previous_window else previous_window[-1].get("color", "N/D")
    seq = " ".join(
        "V" if x.get("color") == "VERDE" else "R" if x.get("color") == "ROJA" else "D"
        for x in previous_window
    )

    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {PAIR}\n"
        f"Timestamp: {int(c['timestamp'])}\n"
        f"Inicio: {fmt_time(int(c['timestamp']))}\n"
        f"Fin: {fmt_time(int(c['timestamp']) + M1)}\n"
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
        "📈 MOVIMIENTO OBSERVADO DURANTE EL MINUTO\n"
        f"Máximo desde apertura: +{fmt(c['max_from_open'])}\n"
        f"Mínimo desde apertura: -{fmt(c['min_from_open'])}\n"
        f"Recorrido acumulado: {fmt(c['path_distance'])}\n"
        f"Distancia alcista: {fmt(c['up_distance'])}\n"
        f"Distancia bajista: {fmt(c['down_distance'])}\n"
        f"Balance recorrido: {fmt(c['up_distance'] - c['down_distance'])}\n"
        f"Dirección observada: {direction}\n\n"
        "⏱️ MUESTRAS INTRAMINUTO\n"
        f"~25%: {fmt(c['sample_25']) if c.get('sample_25') is not None else 'N/D'}\n"
        f"~50%: {fmt(c['sample_50']) if c.get('sample_50') is not None else 'N/D'}\n"
        f"~75%: {fmt(c['sample_75']) if c.get('sample_75') is not None else 'N/D'}\n\n"
        "🔢 ACTUALIZACIONES DEL STREAM\n"
        f"Actualizaciones: {c['tick_count']}\n"
        f"Movimientos ↑: {c['up_moves']} ({up_pct:.1f}%)\n"
        f"Movimientos ↓: {c['down_moves']} ({down_pct:.1f}%)\n"
        f"Movimientos =: {c['flat_moves']}\n\n"
        "⏱️ EXTREMOS\n"
        f"Hora máximo: {fmt_time(c.get('max_time'))}\n"
        f"Hora mínimo: {fmt_time(c.get('min_time'))}\n"
        f"Cierre vs apertura: {fmt(c['close_vs_open'])}\n"
        f"Distancia al máximo: {fmt(abs(c['close_vs_high']))}\n"
        f"Distancia al mínimo: {fmt(abs(c['close_vs_low']))}\n\n"
        "🔎 CONTEXTO ANTERIOR\n"
        f"Color de la vela anterior: {prev_color}\n"
        f"Secuencia disponible: {seq if seq else 'N/D'}\n\n"
        "Solo precio. Sin indicadores, S/R ni rechazo.\n"
        "🚫 OPERACIONES DESACTIVADAS."
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
        "Las 10 velas se conservan para estudiar que caracteristicas preceden a la siguiente.",
        "🚫 Operaciones DESACTIVADAS.",
    ])
    return "\n".join(lines)

# ============================================================
# PROCESAMIENTO DEL STREAM
# ============================================================
def process_stream() -> None:
    global CURRENT_CANDLE

    if IQ is None or not STREAM_STARTED:
        return

    try:
        raw = IQ.get_realtime_candles(PAIR, M1) or {}
    except Exception as exc:
        print(f"Error leyendo realtime_candles: {exc}")
        return

    if not isinstance(raw, dict) or not raw:
        return

    candidates: list[Tuple[int, Dict[str, Any]]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            fallback = int(key)
        except (TypeError, ValueError):
            fallback = 0
        try:
            ts = int(value.get("from", fallback))
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        candidates.append((candle_bucket(ts), value))

    if not candidates:
        return

    bucket, data = max(candidates, key=lambda item: item[0])
    price = extract_price(data)
    if price is None:
        return

    opening = extract_open(data, price)
    stream_high = extract_extreme(data, "max", "high")
    stream_low = extract_extreme(data, "min", "low")
    now_bucket = candle_bucket(server_timestamp())

    # Nunca iniciamos sobre una vela histórica. Si el stream entrega solo
    # historial al arrancar, esperamos hasta que aparezca la M1 actual.
    if CURRENT_CANDLE is None:
        if bucket < now_bucket:
            return
        CURRENT_CANDLE = new_candle(bucket, opening)
        update_candle(CURRENT_CANDLE, bucket, price, stream_high, stream_low)
        return

    current_bucket = int(CURRENT_CANDLE["timestamp"])

    if bucket > current_bucket:
        # La llegada de una vela nueva confirma que la anterior terminó.
        closed = finalize_candle(CURRENT_CANDLE)
        add_closed_candle(closed)

        CURRENT_CANDLE = new_candle(bucket, opening)
        update_candle(CURRENT_CANDLE, bucket, price, stream_high, stream_low)
        return

    if bucket < current_bucket:
        return

    update_candle(CURRENT_CANDLE, bucket, price, stream_high, stream_low)

# ============================================================
# CONEXION
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

    # Resolver antes de iniciar cualquier stream. Esto evita el error de
    # OP_code.ACTIVES[PAIR] == None que provoca el crash observado.
    ok, selected = resolve_pair()
    if not ok:
        raise RuntimeError(
            f"No hay activos OTC disponibles. Par solicitado: {selected}"
        )

    seed_last_10()

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
        if not response.ok:
            return []
        payload = response.json()
        return payload.get("result", []) if isinstance(payload, dict) else []
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

            message = update.get("message", {}) or {}
            chat_id = str((message.get("chat", {}) or {}).get("id", ""))
            if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                continue

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
                    f"Stream: {'ACTIVO' if STREAM_STARTED else 'DETENIDO'}\n"
                    f"Velas almacenadas: {len(LAST_10)}/{WINDOW}\n"
                    "Operaciones: DESACTIVADAS"
                )

        time.sleep(0.5)

# ============================================================
# MAIN ROBUSTO
# ============================================================
def main() -> None:
    global IQ, STREAM_STARTED, CURRENT_CANDLE, LAST_STREAM_ERROR_AT

    required = (IQ_EMAIL, IQ_PASSWORD)
    if not all(required):
        print("Faltan IQ_EMAIL o IQ_PASSWORD en las variables de entorno.")
        return

    if TELEGRAM_TOKEN:
        threading.Thread(target=telegram_commands_loop, daemon=True).start()

    # Conexao + stream com retry controlado. Um erro do pacote nao deve
    # provocar o restart loop de Railway.
    while True:
        try:
            if IQ is None:
                connect_iq()
                send_telegram(
                    "🟢 QUANT MODE INICIADO\n\n"
                    f"Par: {PAIR}\n"
                    "Timeframe: M1\n"
                    "Modo: RECOLECCIÓN DE DATOS\n\n"
                    "Se capturará el movimiento disponible del stream durante cada minuto.\n"
                    "🚫 Operaciones DESACTIVADAS."
                )

            if not STREAM_STARTED:
                if not start_stream():
                    now = time.time()
                    if now - LAST_STREAM_ERROR_AT >= STREAM_RETRY_SECONDS:
                        print("Stream no disponible; se volvera a resolver el catalogo.")
                    time.sleep(STREAM_RETRY_SECONDS)
                    continue

            if RUNNING:
                try:
                    process_stream()
                except Exception as exc:
                    print(f"Error procesando stream: {type(exc).__name__}: {exc}")

            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            print("Detenido por usuario.")
            break
        except Exception as exc:
            print(f"ERROR PRINCIPAL: {type(exc).__name__}: {exc}")
            send_telegram(
                "⚠️ QUANT MODE EN PAUSA POR ERROR\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "No se ejecutan operaciones. Se intentara reconectar."
            )
            try:
                stop_stream()
            except Exception:
                pass
            IQ = None
            CURRENT_CANDLE = None
            time.sleep(PAIR_RETRY_SECONDS)

    stop_stream()


if __name__ == "__main__":
    main()
