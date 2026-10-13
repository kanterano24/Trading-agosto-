"""Bot OTC M1: ejecuta señales confirmadas en la siguiente vela M1.

- Obtiene únicamente velas M1 cerradas desde IQ Option.
- Envía cada vela cerrada a Telegram.
- NO usa Telegram getUpdates.
- Expone /health, /candles y /openapi.json para consultas externas.
- /candles puede protegerse con CANDLES_API_KEY.
"""

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from iqoptionapi.stable_api import IQ_Option
from iqoptionapi import constants as OP_code
try:
    from strategy import (
        normalize_candles, analyze_market,
        STOCH_K_PERIOD, STOCH_K_SMOOTH, STOCH_D_PERIOD,
        STOCH_OVERBOUGHT, STOCH_OVERSOLD,
    )
except ModuleNotFoundError as import_error:
    if import_error.name != "strategy":
        raise
    # Respaldo idéntico para que bot.py conserve compatibilidad si falta strategy.py.
    logging.basicConfig(level=logging.INFO)
    logging.warning("No se encontró strategy.py; usando la estrategia de velas integrada en bot.py")
    from math import isfinite

    STOCH_K_PERIOD = 13
    STOCH_K_SMOOTH = 3
    STOCH_D_PERIOD = 3
    STOCH_OVERBOUGHT = 80.0
    STOCH_OVERSOLD = 20.0
    TIMEFRAME_SECONDS = 60
    STRONG_BODY_RATIO = 0.60

    def normalize_candles(raw):
        out = []
        for candle in raw or []:
            try:
                ts = int(candle.get("from", candle.get("at", candle.get("timestamp", 0))))
                op = float(candle["open"])
                close = float(candle["close"])
                low = float(candle.get("min", candle.get("low", min(op, close))))
                high = float(candle.get("max", candle.get("high", max(op, close))))
                if ts <= 0 or not all(isfinite(x) for x in (op, close, low, high)):
                    continue
                if low > min(op, close) or high < max(op, close) or low > high:
                    continue
                rng = high - low
                body = abs(close - op)
                out.append({
                    "timestamp": ts, "open": op, "close": close,
                    "min": low, "max": high, "low": low, "high": high,
                    "range": rng, "body": body,
                    "body_ratio": body / rng if rng > 0 else 0.0,
                    "lower_wick": min(op, close) - low,
                    "upper_wick": high - max(op, close),
                    "color": "green" if close > op else "red" if close < op else "doji",
                })
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                continue
        unique = {c["timestamp"]: c for c in sorted(out, key=lambda x: x["timestamp"])}
        return list(unique.values())

    def analyze_market(raw, current_timestamp=None):
        candles = normalize_candles(raw)
        result = {
            "signal": "NO SIGNAL", "bias": "NEUTRAL", "call_score": 0, "put_score": 0,
            "sequence": [], "sequence_text": "", "entry_timing": "NEXT_M1_CANDLE",
            "signal_candle_timestamp": None, "confirmation_candle_timestamp": None,
            "entry_candle_timestamp": None, "stoch_k": None, "stoch_d": None,
            "stoch_k_previous": None, "stoch_d_previous": None,
            "reason": "Se necesitan tres velas M1 cerradas para evaluar el patrón.",
            "first_candle": None, "reversal_candle": None, "continuation_candle": None,
        }
        if len(candles) < 3:
            result["reason"] = f"Historial insuficiente: {len(candles)}/3 velas cerradas."
            return result
        force, reversal, continuation = candles[-3:]
        result.update({
            "sequence": [force["color"], reversal["color"], continuation["color"]],
            "sequence_text": "-".join({"green": "V", "red": "R", "doji": "D"}[c["color"]]
                                      for c in (force, reversal, continuation)),
            "signal_candle_timestamp": reversal["timestamp"],
            "confirmation_candle_timestamp": continuation["timestamp"],
            "entry_candle_timestamp": int(current_timestamp) if current_timestamp is not None
                                      else continuation["timestamp"] + TIMEFRAME_SECONDS,
            "first_candle": force, "reversal_candle": reversal,
            "continuation_candle": continuation,
        })
        strong_force = force["range"] > 0 and force["body_ratio"] >= STRONG_BODY_RATIO
        put_pattern = (strong_force and force["color"] == "green"
                       and reversal["color"] == "red" and continuation["color"] == "red"
                       and continuation["high"] <= force["open"])
        call_pattern = (strong_force and force["color"] == "red"
                        and reversal["color"] == "green" and continuation["color"] == "green"
                        and continuation["low"] >= force["open"])
        if put_pattern:
            result.update({"signal": "PUT", "bias": "PUT", "put_score": 1,
                "reason": (f"PUT válido: fuerza verde ({force['body_ratio']:.0%} del rango), "
                           f"reversión roja y continuidad roja. Máximo de continuidad "
                           f"({continuation['high']}) <= apertura de fuerza ({force['open']}). "
                           "Entrada al abrir la siguiente vela M1.")})
        elif call_pattern:
            result.update({"signal": "CALL", "bias": "CALL", "call_score": 1,
                "reason": (f"CALL válido: fuerza roja ({force['body_ratio']:.0%} del rango), "
                           f"reversión verde y continuidad verde. Mínimo de continuidad "
                           f"({continuation['low']}) >= apertura de fuerza ({force['open']}). "
                           "Entrada al abrir la siguiente vela M1.")})
        else:
            result["reason"] = ("Sin señal: fuerza >= 60% del rango, reversión de color contrario "
                "y continuidad del color de reversión. PUT=verde-roja-roja con máximo de "
                "continuidad <= apertura de fuerza; CALL=roja-verde-verde con mínimo de "
                f"continuidad >= apertura de fuerza. Secuencia={result['sequence_text']}; "
                f"cuerpo/rango de fuerza={force['body_ratio']:.0%}.")
        return result

PAIR = os.getenv("PAIR", "EURUSD-OTC").strip() or "EURUSD-OTC"
PAIRS = []  # Se completa automáticamente con los pares OTC disponibles
MAX_OTC_PAIRS = 20
PAIR_REFRESH_SECONDS = 900.0  # Actualizar la lista cada 15 minutos
PAIR_SCAN_INTERVAL_SECONDS = 1.0
TF = 60
COUNT = 200
EXPIRATION = 1
# Importe predeterminado solicitado: 5. Si AMOUNT está definido en Railway, prevalece.
AMOUNT = float(os.getenv("AMOUNT", "8"))
if AMOUNT <= 0:
    raise ValueError("AMOUNT debe ser mayor que 0")

ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in (
    "1", "true", "yes", "si"
)

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
POLL = max(0.20, float(os.getenv("POLL_SECONDS", "0.20")))

API_PORT = int(os.getenv("PORT", "8080"))
CANDLES_API_KEY = os.getenv("CANDLES_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

sess = requests.Session()
tg_last = 0.0

# Telegram se procesa en segundo plano para que enviar mensajes nunca retrase
# el análisis ni la entrada de las operaciones.
TG_MIN_INTERVAL = max(1.05, float(os.getenv("TG_MIN_INTERVAL", "1.10")))
TG_RETRY_MAX = max(1, int(os.getenv("TG_RETRY_MAX", "3")))
TG_QUEUE_MAX = max(100, int(os.getenv("TG_QUEUE_MAX", "500")))
TG_429_LOG_COOLDOWN = max(10.0, float(os.getenv("TG_429_LOG_COOLDOWN", "30")))
TG_QUEUE = queue.Queue(maxsize=TG_QUEUE_MAX)
TG_LAST_429_LOG = 0.0
TG_LAST_QUEUE_LOG = 0.0
TG_WORKER_STARTED = False
TG_WORKER_LOCK = threading.Lock()

# Historial actual que queda expuesto por /candles.
received_candles = []


def _telegram_worker():
    """Emisor único y lento; respeta Telegram sin bloquear el bot."""
    global tg_last, TG_LAST_429_LOG

    while True:
        msg = TG_QUEUE.get()
        try:
            if not TOKEN or not CHAT:
                continue

            for attempt in range(1, TG_RETRY_MAX + 1):
                wait = TG_MIN_INTERVAL - (time.monotonic() - tg_last)
                if wait > 0:
                    time.sleep(wait)

                try:
                    r = sess.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={
                            "chat_id": CHAT,
                            "text": msg,
                            "disable_web_page_preview": True,
                        },
                        timeout=15,
                    )

                    if r.status_code == 429:
                        try:
                            payload = r.json()
                        except ValueError:
                            payload = {}

                        retry_after = payload.get("parameters", {}).get("retry_after", 2)
                        try:
                            retry_after = max(1.0, float(retry_after))
                        except (TypeError, ValueError):
                            retry_after = 2.0

                        if time.monotonic() - TG_LAST_429_LOG >= TG_429_LOG_COOLDOWN:
                            logging.warning(
                                "Telegram 429: limite alcanzado; esperando %.1fs",
                                retry_after,
                            )
                            TG_LAST_429_LOG = time.monotonic()

                        if attempt >= TG_RETRY_MAX:
                            logging.warning(
                                "Telegram: se descarta el mensaje después de %d reintentos por 429",
                                TG_RETRY_MAX,
                            )
                            break

                        time.sleep(retry_after + 0.25)
                        continue

                    r.raise_for_status()
                    payload = r.json()
                    if not payload.get("ok"):
                        logging.warning("Telegram rechazo mensaje: %s", payload)

                    tg_last = time.monotonic()
                    break

                except requests.RequestException as exc:
                    if attempt >= TG_RETRY_MAX:
                        logging.warning("Telegram no disponible: %s", exc)
                        break
                    time.sleep(min(2.0 * attempt, 5.0))
        finally:
            TG_QUEUE.task_done()


def _ensure_telegram_worker():
    global TG_WORKER_STARTED

    if TG_WORKER_STARTED or not TOKEN or not CHAT:
        return

    with TG_WORKER_LOCK:
        if TG_WORKER_STARTED:
            return

        thread = threading.Thread(
            target=_telegram_worker,
            name="telegram-sender",
            daemon=True,
        )
        thread.start()
        TG_WORKER_STARTED = True


def tg(msg):
    """Encola sin bloquear el trading; protege alertas importantes si se llena."""
    global TG_LAST_QUEUE_LOG
    if not TOKEN or not CHAT:
        return False

    _ensure_telegram_worker()
    message = str(msg)
    critical = any(marker in message.upper() for marker in (
        "ORDEN ENVIADA", "ORDEN RECHAZADA", "DIRECCIÓN:", "DIRECCION:",
        "ENTRADA BLOQUEADA", "ERROR", "⚠️"
    ))

    try:
        TG_QUEUE.put_nowait(message)
        return True
    except queue.Full:
        # No bloquear el análisis de los 50 pares. Si es una alerta de operación,
        # libera un lugar descartando el mensaje más antiguo; los mensajes de
        # rutina nuevos se omiten mientras Telegram esté saturado.
        if critical:
            try:
                TG_QUEUE.get_nowait()
                TG_QUEUE.task_done()
            except queue.Empty:
                pass
            try:
                TG_QUEUE.put_nowait(message)
                return True
            except queue.Full:
                pass

        now = time.monotonic()
        if now - TG_LAST_QUEUE_LOG >= TG_429_LOG_COOLDOWN:
            logging.warning(
                "Cola Telegram llena; se omiten mensajes rutinarios y se priorizan alertas de operación"
            )
            TG_LAST_QUEUE_LOG = now
        return False


OPENAPI_SPEC = {
    "openapi": "3.0.1",
    "info": {
        "title": "EURUSD-OTC Candles API",
        "version": "1.0.0",
        "description": (
            "API de solo lectura para consultar las velas M1 "
            "cerradas del par configurado."
        ),
    },
    "servers": [
        {
            "url": "https://worker-production-be7f.up.railway.app"
        }
    ],
    "paths": {
        "/health": {
            "get": {
                "operationId": "health",
                "summary": "Estado del servicio",
                "responses": {
                    "200": {
                        "description": "Servicio activo"
                    }
                },
            }
        },
        "/candles": {
            "get": {
                "operationId": "getCandles",
                "summary": "Obtiene las velas M1 cerradas del único par configurado",
                "description": (
                    "Devuelve el historial actual de velas M1 "
                    "cerradas de EURUSD-OTC."
                ),
                "security": [
                    {"bearerAuth": []}
                ],
                "responses": {
                    "200": {
                        "description": "Historial de velas",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {
                                            "type": "boolean"
                                        },
                                        "pair": {
                                            "type": "string"
                                        },
                                        "timeframe": {
                                            "type": "string"
                                        },
                                        "count": {
                                            "type": "integer"
                                        },
                                        "candles": {
                                            "type": "array",
                                            "items": {
                                                "$ref": (
                                                    "#/components/schemas/"
                                                    "Candle"
                                                )
                                            }
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "401": {
                        "description": "No autorizado"
                    },
                },
            }
        },
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "API key",
            }
        },
        "schemas": {
            "Candle": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "timestamp": {"type": "integer"},
                    "time": {"type": "string"},
                    "pair": {"type": "string"},
                    "timeframe": {"type": "string"},
                    "color": {"type": "string"},
                    "open": {"type": "number"},
                    "close": {"type": "number"},
                    "high": {"type": "number"},
                    "low": {"type": "number"},
                    "range": {"type": "number"},
                    "body": {"type": "number"},
                    "lower_wick": {"type": "number"},
                    "upper_wick": {"type": "number"},
                },
            }
        },
    },
}


class CandlesAPIHandler(BaseHTTPRequestHandler):
    """API de solo lectura para un cliente autorizado."""

    def _authorized(self):
        if not CANDLES_API_KEY:
            return True

        return self.headers.get("Authorization", "") == (
            f"Bearer {CANDLES_API_KEY}"
        )

    def _send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "EURUSD candles",
                },
            )
            return

        if path == "/openapi.json":
            self._send_json(200, OPENAPI_SPEC)
            return

        if path != "/candles":
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": "not found",
                },
            )
            return

        if not self._authorized():
            self._send_json(
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                },
            )
            return

        candles = []

        for c in received_candles[-COUNT:]:
            candles.append(
                {
                    "number": c.get("number"),
                    "timestamp": c.get("timestamp"),
                    "time": c.get("time"),
                    "pair": PAIR,
                    "timeframe": "M1",
                    "color": c.get("color"),
                    "open": c.get("open"),
                    "close": c.get("close"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "range": c.get("range"),
                    "body": c.get("body"),
                    "lower_wick": c.get("lower_wick"),
                    "upper_wick": c.get("upper_wick"),
                }
            )

        self._send_json(
            200,
            {
                "ok": True,
                "pair": PAIR,
                "timeframe": "M1",
                "count": len(candles),
                "candles": candles,
            },
        )

    def log_message(self, fmt, *args):
        logging.info(
            "HTTP %s - %s",
            self.address_string(),
            fmt % args,
        )


def start_candles_api():
    """Inicia una sola vez el endpoint HTTP de Railway."""
    server = ThreadingHTTPServer(
        ("0.0.0.0", API_PORT),
        CandlesAPIHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        name="candles-api",
        daemon=True,
    )
    thread.start()

    logging.info(
        "API de velas disponible en puerto %s | "
        "/health | /candles | /openapi.json",
        API_PORT,
    )

    return server


def connect():
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Faltan IQ_EMAIL/IQ_PASSWORD en Railway"
        )

    iq = IQ_Option(EMAIL, PASSWORD)

    # Este bot solo usa Binary/Turbo.
    # Algunas versiones de iqoptionapi lanzan un hilo Digital que puede
    # fallar con None en get_digital_underlying_list_data().
    # Lo desactivamos para que no interfiera con las entradas binarias.
    def _no_digital_underlying():
        return {"underlying": []}

    iq.get_digital_underlying_list_data = _no_digital_underlying

    ok, why = iq.connect()

    if not ok:
        raise RuntimeError(
            f"Conexión fallida: {why}"
        )

    iq.change_balance("PRACTICE")
    logging.info("Conectado a PRACTICE")

    return iq


def connect_retry():
    delay = 5

    while True:
        try:
            return connect()

        except Exception as e:
            logging.exception(
                "No conectó; reintento en %ss",
                delay,
            )

            tg(
                f"⚠️ Conexión fallida ({type(e).__name__}); "
                f"reintento en {delay}s"
            )

            time.sleep(delay)
            delay = min(delay * 2, 60)


def get_candles(iq, pair):
    """Devuelve (velas cerradas, vela actual, timestamp del servidor)."""
    try:
        now = int(iq.get_server_timestamp())
    except Exception:
        now = int(time.time())
    raw = iq.get_candles(pair, TF, COUNT + 20, now) or []
    current_start = now - now % TF
    closed_raw, current_raw = [], []
    for candle in raw:
        try:
            start = int(candle.get("from", candle.get("at", candle.get("timestamp", 0))))
            if start <= 0:
                continue
            if start + TF <= current_start:
                closed_raw.append(candle)
            elif start == current_start:
                current_raw.append(candle)
        except (TypeError, ValueError):
            continue
    closed = normalize_candles(closed_raw)[-COUNT:]
    current_list = normalize_candles(current_raw)
    current = current_list[-1] if current_list else None
    return closed, current, now


def build_candle_api_history(candles):
    """Convierte las velas normalizadas al formato del endpoint."""
    result = []

    for i, c in enumerate(candles, start=1):
        result.append(
            {
                "number": i,
                "timestamp": c["timestamp"],
                "time": datetime.fromtimestamp(
                    c["timestamp"]
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "open": c["open"],
                "close": c["close"],
                "high": c["high"],
                "low": c["low"],
                "range": c.get(
                    "range",
                    c["high"] - c["low"],
                ),
                "body": c.get(
                    "body",
                    abs(c["close"] - c["open"]),
                ),
                "lower_wick": c.get(
                    "lower_wick",
                    min(c["open"], c["close"]) - c["low"],
                ),
                "upper_wick": c.get(
                    "upper_wick",
                    c["high"] - max(c["open"], c["close"]),
                ),
                "color": c["color"],
            }
        )

    return result


def _color_label(color):
    return {"green": "VERDE", "red": "ROJA", "doji": "DOJI"}.get(
        color, str(color).upper()
    )


def _fmt_price(value):
    """Formato compacto sin perder precisión útil del precio."""
    try:
        return f"{float(value):.10g}"
    except (TypeError, ValueError):
        return str(value)


def candle_detail(candle, pair, number=None, state="CERRADA"):
    """Detalle completo de una vela: OHLC, rango, cuerpo y ambas mechas."""
    dt = datetime.fromtimestamp(int(candle["timestamp"])).strftime("%Y-%m-%d %H:%M:%S")
    title_number = f" #{number}" if number is not None else ""
    candle_range = candle.get("range", candle["high"] - candle["low"])
    body = candle.get("body", abs(candle["close"] - candle["open"]))
    lower = candle.get("lower_wick", min(candle["open"], candle["close"]) - candle["low"])
    upper = candle.get("upper_wick", candle["high"] - max(candle["open"], candle["close"]))
    return (
        f"🕯️ VELA M1 {state}{title_number}\n"
        f"Par: {pair}\nHora apertura: {dt}\n"
        f"Color actual: {_color_label(candle.get('color', 'desconocido'))}\n"
        f"Apertura: {_fmt_price(candle['open'])} | Cierre: {_fmt_price(candle['close'])}\n"
        f"Máximo: {_fmt_price(candle['high'])} | Mínimo: {_fmt_price(candle['low'])}\n"
        f"Rango: {_fmt_price(candle_range)} | Cuerpo: {_fmt_price(body)}\n"
        f"Mecha inferior: {_fmt_price(lower)}\nMecha superior: {_fmt_price(upper)}"
    )


def candle_movement_report(candles, result, pair):
    """Reporte único por vela cerrada con OHLC, movimiento y estocástico."""
    candle = candles[-1]
    previous = candles[-2] if len(candles) >= 2 else None
    dt = datetime.fromtimestamp(int(candle["timestamp"])).strftime("%Y-%m-%d %H:%M:%S")
    body = float(candle.get("body", abs(candle["close"] - candle["open"])))
    candle_range = float(candle.get("range", candle["high"] - candle["low"]))
    body_pct = (body / candle_range * 100.0) if candle_range > 0 else 0.0
    if previous:
        delta = float(candle["close"]) - float(previous["close"])
        delta_pct = (delta / float(previous["close"]) * 100.0) if previous["close"] else 0.0
        movement = (
            f"Movimiento vs cierre anterior: {delta:+.10g} ({delta_pct:+.5f}%)\n"
            f"Rango vs vela anterior: {candle_range:.10g} / "
            f"{float(previous.get('range', previous['high'] - previous['low'])):.10g}\n"
            f"Color anterior: {_color_label(previous.get('color'))}"
        )
    else:
        movement = "Movimiento vs cierre anterior: sin dato (primera vela del historial)"

    k = result.get("stoch_k")
    d = result.get("stoch_d")
    kp = result.get("stoch_k_previous")
    dp = result.get("stoch_d_previous")
    kd = result.get("stoch_k_delta")
    dd = result.get("stoch_d_delta")
    fmt = lambda x: "N/D" if x is None else f"{float(x):.2f}"
    return (
        f"📊 ANÁLISIS DE VELA CERRADA M1\nPar: {pair}\nHora apertura: {dt}\n"
        f"Color: {_color_label(candle.get('color'))}\n"
        f"Apertura: {_fmt_price(candle['open'])} | Cierre: {_fmt_price(candle['close'])}\n"
        f"Máximo: {_fmt_price(candle['high'])} | Mínimo: {_fmt_price(candle['low'])}\n"
        f"Rango: {_fmt_price(candle_range)} | Cuerpo: {_fmt_price(body)} ({body_pct:.1f}% del rango)\n"
        f"Mecha inferior: {_fmt_price(candle.get('lower_wick', 0))} | "
        f"Mecha superior: {_fmt_price(candle.get('upper_wick', 0))}\n"
        f"{movement}\n\n"
        f"📈 ESTOCÁSTICO K13 / suavizado 3 / D3\n"
        f"K azul: {fmt(kp)} → {fmt(k)} (Δ {fmt(kd)})\n"
        f"D roja: {fmt(dp)} → {fmt(d)} (Δ {fmt(dd)})\n"
        f"Cruce K hacia arriba de 80: {'SÍ' if result.get('k_crosses_overbought_up') else 'NO'}\n"
        f"Cruce D hacia abajo de 20: {'SÍ' if result.get('d_crosses_oversold_down') else 'NO'}\n"
        f"SEÑAL: {result.get('signal', 'NO SIGNAL')}\n"
        f"Motivo: {result.get('reason', 'Sin motivo')}"
    )


def candle_message(candle, number, pair=None):
    """Compatibilidad con mensajes anteriores, ahora con par explícito."""
    return candle_detail(candle, pair or PAIR, number=number, state="CERRADA")


def _candle_summary(candle, index):
    """Resumen detallado de una vela usada por la estrategia."""
    color = {"green": "V", "red": "R", "doji": "D"}.get(candle.get("color"), "?")
    return (
        f"V{index}={color} O:{_fmt_price(candle['open'])} C:{_fmt_price(candle['close'])} "
        f"H:{_fmt_price(candle['high'])} L:{_fmt_price(candle['low'])} "
        f"cuerpo:{_fmt_price(candle.get('body', 0))} "
        f"rango:{_fmt_price(candle.get('range', 0))} "
        f"mechaInf:{_fmt_price(candle.get('lower_wick', 0))} "
        f"mechaSup:{_fmt_price(candle.get('upper_wick', 0))}"
    )


def send_telegram_batches(reports, title="📊 REPORTE DE ANÁLISIS"):
    """Agrupa informes de muchos pares para evitar una inundación de Telegram."""
    if not reports:
        return
    max_chars = max(2500, min(3500, int(os.getenv("TG_BATCH_MAX_CHARS", "3500"))))
    chunks = []
    current = title + "\n"
    for report in reports:
        report = str(report).strip()
        addition = report + "\n\n"
        if len(current) + len(addition) > max_chars and current.strip() != title:
            chunks.append(current.rstrip())
            current = title + " (continuación)\n"
        current += addition
    if current.strip() != title:
        chunks.append(current.rstrip())
    for chunk in chunks:
        tg(chunk)


def binary_asset_status(iq, pair):
    """
    Comprueba exclusivamente TURBO.

    TURBO es el mercado de opciones binarias que admite expiraciones
    de 1 a 5 minutos en iqoptionapi; por eso un par que no este abierto
    en TURBO queda bloqueado para analisis y entrada de 1 minuto.
    """
    try:
        data = iq.get_all_init_v2()

        if not isinstance(data, dict):
            return False, f"{pair} sin datos Binary/Turbo"

        actives = _get_turbo_actives(data)

        if not isinstance(actives, dict) or not actives:
            return False, f"{pair} sin datos TURBO"

        for aid, active in actives.items():
            if not isinstance(active, dict):
                continue

            name = str(active.get("name", ""))
            parts = name.split(".")
            name = parts[-1] if parts else name

            if name != pair:
                continue

            try:
                active_id = int(aid)
            except (TypeError, ValueError):
                active_id = aid

            OP_code.ACTIVES[pair] = active_id

            enabled = bool(active.get("enabled", False))
            suspended = bool(active.get("is_suspended", False))
            is_open = enabled and not suspended

            if is_open:
                return True, f"{pair} abierto en TURBO | expiracion 5m disponible"

            return False, f"{pair} TURBO cerrado/suspendido"

        return False, f"{pair} no aparece en TURBO; 5m bloqueado"

    except Exception as e:
        logging.exception(
            "No se pudo comprobar TURBO de %s",
            pair,
        )
        return False, f"error disponibilidad 5m: {type(e).__name__}: {e}"


def wait_binary_asset(iq, pair, attempts=2, delay=0.35):
    """Comprueba TURBO inmediatamente antes de una entrada de 1 minuto."""
    last_reason = "sin respuesta"

    for attempt in range(1, attempts + 1):
        is_open, reason = binary_asset_status(iq, pair)

        logging.info(
            "Disponibilidad 1m %s | intento=%d/%d | abierta=%s | %s",
            pair,
            attempt,
            attempts,
            is_open,
            reason,
        )

        last_reason = reason

        if is_open:
            return True, reason

        if attempt < attempts:
            time.sleep(delay)

    return False, last_reason


def check_binary_availability(iq, pair):
    """Comprueba exclusivamente disponibilidad TURBO para expiracion 5m."""
    return binary_asset_status(iq, pair)


def _get_turbo_actives(data):
    """Obtiene actives TURBO de las variantes de respuesta de iqoptionapi."""
    if not isinstance(data, dict):
        return {}

    candidates = [
        data.get("turbo"),
        data.get("result", {}).get("turbo") if isinstance(data.get("result"), dict) else None,
    ]

    for turbo in candidates:
        if not isinstance(turbo, dict):
            continue
        actives = turbo.get("actives")
        if isinstance(actives, dict):
            return actives

    return {}


def _active_pair_name(active):
    """Normaliza el nombre del activo recibido por IQ Option."""
    if not isinstance(active, dict):
        return ""

    name = str(active.get("name", "")).strip()
    if not name:
        return ""

    # IQ Option puede devolver nombres como TURBO.EURUSD-OTC,
    # binary.EURUSD-OTC o con otros prefijos separados por '.'.
    return name.split(".")[-1]


def discover_otc_1m_pairs(iq):
    """
    Descubre hasta 50 pares OTC que estén ABIERTOS en TURBO.

    TURBO es el modo que admite expiraciones cortas de 1 a 5 minutos.
    Por eso un par que no esté abierto/sin suspendido en TURBO queda
    fuera de la lista: no se analiza y no se permite una entrada de 1m.
    """
    try:
        data = iq.get_all_init_v2()
        actives = _get_turbo_actives(data)

        if not actives:
            logging.warning(
                "IQ Option no devolvio actives TURBO en get_all_init_v2()"
            )
            return []

        found = []

        for aid, active in actives.items():
            if not isinstance(active, dict):
                continue

            pair = _active_pair_name(active)

            if not pair.endswith("-OTC"):
                continue

            enabled = bool(active.get("enabled", False))
            suspended = bool(active.get("is_suspended", False))

            if not enabled or suspended:
                continue

            try:
                active_id = int(aid)
            except (TypeError, ValueError):
                active_id = aid

            OP_code.ACTIVES[pair] = active_id
            found.append(pair)

        pairs = sorted(set(found))[:MAX_OTC_PAIRS]

        logging.info(
            "Pares OTC TURBO/1m encontrados: %d/%d | %s",
            len(pairs),
            MAX_OTC_PAIRS,
            ", ".join(pairs) if pairs else "NINGUNO",
        )

        return pairs

    except Exception:
        logging.exception("Error actualizando pares OTC de 1 minuto")
        return []

def execute_binary_order(iq, pair, signal, amount, expiration):
    """Envía una sola orden y explica claramente los rechazos por saldo insuficiente."""
    try:
        if amount <= 0:
            return False, "Importe inválido: AMOUNT debe ser mayor que 0"
        # Este bot trabaja en cuenta de práctica; no cambia a REAL.
        iq.change_balance("PRACTICE")
        ok, response = iq.buy(amount, pair, signal.lower(), expiration)
        response_text = str(response)
        if not ok and "insufficient funds" in response_text.lower():
            return False, (
                "Saldo insuficiente en la cuenta PRACTICE para el importe "
                f"{amount:g}. Reduce AMOUNT en Railway (por ejemplo, a 1) "
                "o verifica el saldo disponible de práctica."
            )
        return bool(ok), response
    except Exception as exc:
        response_text = str(exc)
        if "insufficient funds" in response_text.lower():
            return False, (
                "Saldo insuficiente en la cuenta PRACTICE. Reduce AMOUNT en "
                "Railway (por ejemplo, a 1) o verifica el saldo disponible."
            )
        logging.exception("Error enviando orden %s %s", pair, signal)
        return False, f"{type(exc).__name__}: {exc}"


def main():
    global received_candles, PAIR, PAIRS

    start_candles_api()
    iq = connect_retry()
    _ensure_telegram_worker()

    # Estado independiente para cada par: evita repetir velas/órdenes.
    last_closed_stamp = {}
    last_evaluated_candle = {}
    last_order_candle = {}
    last_pair_refresh = 0.0
    last_scan_started = 0.0

    tg(
        "🟢 BOT INICIADO | HASTA 20 PARES OTC\n"
        "Actualización de pares: cada 15 minutos\n"
        "Temporalidad: M1 | Estocástico K=13, suavizado=3, D=3; niveles 20/80\n"
        "PUT: K azul cruza arriba de 80 en vela roja (:03), la vela :04 confirma roja.\n"
        "CALL: D roja cruza abajo de 20 en vela verde (:03), la vela :04 confirma verde.\n"
        "Entrada al límite siguiente de 5 minutos (:00, :05, :10...).\n"
        f"Expiración: {EXPIRATION} minutos | Importe: {AMOUNT:g} | Cuenta: PRACTICE"
    )

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()

            now_mono = time.monotonic()
            if now_mono - last_pair_refresh >= PAIR_REFRESH_SECONDS or not PAIRS:
                discovered = discover_otc_1m_pairs(iq)
                if discovered:
                    old_pairs = list(PAIRS)
                    PAIRS = discovered[:MAX_OTC_PAIRS]
                    PAIR = PAIRS[0]
                    added = [p for p in PAIRS if p not in old_pairs]
                    removed = [p for p in old_pairs if p not in PAIRS]
                    tg(
                        "🔄 ACTUALIZACIÓN DE PARES OTC\n"
                        f"Disponibles seleccionados: {len(PAIRS)}/{MAX_OTC_PAIRS}\n"
                        f"Añadidos: {', '.join(added) if added else 'ninguno'}\n"
                        f"Retirados: {', '.join(removed) if removed else 'ninguno'}\n"
                        f"Lista: {', '.join(PAIRS)}"
                    )
                    last_pair_refresh = now_mono
                else:
                    logging.warning("No se encontraron pares OTC disponibles; se reintentará en 60 segundos")
                    tg("⚠️ No se encontraron pares OTC abiertos. Se volverá a intentar en 60 segundos.")
                    last_pair_refresh = now_mono - PAIR_REFRESH_SECONDS + 60.0
                    time.sleep(POLL)
                    continue

            # Evita saturar la API con solicitudes repetidas sin esperar al ciclo.
            if now_mono - last_scan_started < PAIR_SCAN_INTERVAL_SECONDS:
                time.sleep(POLL)
                continue
            last_scan_started = now_mono

            for pair in list(PAIRS):
                try:
                    closed, current, server_now = get_candles(iq, pair)
                    if not closed or current is None:
                        logging.warning("%s: no se recibieron velas cerradas/actuales", pair)
                        continue

                    current_stamp = int(current["timestamp"])
                    closed_stamp = int(closed[-1]["timestamp"])

                    # Telegram y evaluación solo una vez por nueva vela M1 y por par.
                    if last_evaluated_candle.get(pair) == current_stamp:
                        continue
                    last_evaluated_candle[pair] = current_stamp

                    received_candles = build_candle_api_history(closed)
                    result = analyze_market(closed, current_timestamp=current_stamp)

                    if last_closed_stamp.get(pair) != closed_stamp:
                        last_closed_stamp[pair] = closed_stamp
                        tg(candle_movement_report(closed, result, pair))

                    logging.info(
                        "%s | vela_cerrada=%s | actual=%s | K=%s D=%s | señal=%s | %s",
                        pair, closed_stamp, current_stamp, result.get("stoch_k"),
                        result.get("stoch_d"), result.get("signal"), result.get("reason")
                    )

                    # Entrada en la vela M1 siguiente a la tercera vela cerrada del patrón.
                    if result.get("signal") in ("CALL", "PUT") and result.get("entry_candle_timestamp") == current_stamp:
                        age = int(server_now) - current_stamp
                        if age < 0 or age > 3:
                            tg(
                                f"⛔ ENTRADA BLOQUEADA POR RETRASO\nPar: {pair}\n"
                                f"Señal: {result['signal']}\nRetraso: {age}s. No se ejecutará tarde."
                            )
                        elif last_order_candle.get(pair) == current_stamp:
                            logging.warning("%s: ya se intentó una orden en la vela M1 %s", pair, current_stamp)
                        elif not ENABLE_TRADES:
                            tg(
                                f"🧪 SEÑAL VÁLIDA SIN EJECUTAR\nPar: {pair}\n"
                                f"Dirección: {result['signal']}\nMotivo: ENABLE_TRADES está desactivado."
                            )
                            last_order_candle[pair] = current_stamp
                        else:
                            last_order_candle[pair] = current_stamp
                            ok, order_id = execute_binary_order(
                                iq, pair, result["signal"], AMOUNT, EXPIRATION
                            )
                            tg(
                                f"{'✅ ORDEN ENVIADA' if ok else '⚠️ ORDEN RECHAZADA'}\n"
                                f"Par: {pair}\nDirección: {result['signal']}\n"
                                f"Importe: {AMOUNT:g}\nExpiración: {EXPIRATION} minutos\n"
                                f"Vela de cruce: {result.get('signal_candle_timestamp')}\n"
                                f"Vela de confirmación: {result.get('confirmation_candle_timestamp')}\n"
                                f"Vela de entrada: {current_stamp}\n"
                                f"Motivo: {result.get('reason')}\nID/respuesta: {order_id}"
                            )
                except Exception:
                    logging.exception("Error analizando el par %s; se continúa con los demás", pair)

        except Exception:
            logging.exception("Error del ciclo principal")
            try:
                if not iq.check_connect():
                    iq = connect_retry()
            except Exception:
                iq = connect_retry()

        time.sleep(POLL)


if __name__ == "__main__":
    while True:
        try:
            main()

        except KeyboardInterrupt:
            break

        except Exception:
            logging.exception(
                "Fallo principal; reinicio en 10s"
            )
            time.sleep(10)
