"""Bot OTC M1: presión completa y entrada de reversión al abrir la vela siguiente, expiración de 1 minuto.

- Obtiene únicamente velas M1 cerradas desde IQ Option.
- Telegram recibe confirmaciones de órdenes y ofrece botones para activar/detener operaciones.
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
from strategy import normalize_candles, analyze_market


PAIR = os.getenv("PAIR", "ARBUSD-OTC").strip().upper()
# Analiza seis pares OTC abiertos en TURBO. Los preferidos van primero;
# la lista se vuelve a comprobar cada 15 minutos.
PREFERRED_PAIRS = ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "EURJPY-OTC", "GBPJPY-OTC"]
PAIRS = []
MAX_OTC_PAIRS = 6
AUTO_DISCOVER_PAIRS = True
# La lista rota únicamente después de que los dos pares activos ejecuten
# una operación aceptada cada uno.
PAIR_ROTATION_OFFSET = 0
TF = 60
COUNT = 200
EXPIRATION = 1
# Importe predeterminado: mínimo habitual de IQ Option.
# Si Railway tiene AMOUNT configurado, esa variable prevalece.
AMOUNT = float(os.getenv("AMOUNT", "5"))
if AMOUNT <= 0:
    raise ValueError("AMOUNT debe ser mayor que 0")

ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in (
    "1", "true", "yes", "si"
)
BOT_ACTIVE = ENABLE_TRADES
BOT_ACTIVE_LOCK = threading.Lock()
TG_CONTROL_STARTED = False
TG_UPDATE_OFFSET = 0

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

# Evita volver a notificar el mismo ID durante esta ejecución.
NOTIFIED_ORDER_IDS = set()
NOTIFIED_ORDER_LOCK = threading.Lock()

# Historial actual que queda expuesto por /candles.
received_candles = []
API_SERVER = None
API_SERVER_LOCK = threading.Lock()


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
    """Telegram se reserva exclusivamente para órdenes ejecutadas con éxito."""
    global TG_LAST_QUEUE_LOG
    message = str(msg).strip()
    # Descarta reportes, velas, inicio, cambios de pares, errores y órdenes
    # rechazadas. Solo se permite el aviso dedicado generado tras IQ.buy exitoso.
    if not message.startswith("🧪 ORDEN ACEPTADA\nPar:"):
        return False
    # Un ID repetido no vuelve a generar otro aviso de Telegram.
    order_id = None
    for line in message.splitlines():
        if line.startswith("ID de orden:"):
            order_id = line.split(":", 1)[1].strip()
            break
    if not order_id or order_id.lower() in {"none", "false", "0", ""}:
        logging.warning("Telegram bloqueó aviso sin ID de orden válido")
        return False
    with NOTIFIED_ORDER_LOCK:
        if order_id in NOTIFIED_ORDER_IDS:
            logging.warning("Aviso duplicado bloqueado para ID %s", order_id)
            return False
        NOTIFIED_ORDER_IDS.add(order_id)
    if not TOKEN or not CHAT:
        return False

    _ensure_telegram_worker()
    critical = any(marker in message.upper() for marker in (
        "ORDEN ENVIADA", "ORDEN RECHAZADA", "DIRECCIÓN:", "DIRECCION:",
        "ENTRADA BLOQUEADA", "ERROR", "⚠️"
    ))

    try:
        TG_QUEUE.put_nowait(message)
        return True
    except queue.Full:
        # No bloquear el análisis de los pares configurados. Si es una alerta de operación,
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


def _bot_is_active():
    with BOT_ACTIVE_LOCK:
        return BOT_ACTIVE


def _set_bot_active(active):
    global BOT_ACTIVE
    with BOT_ACTIVE_LOCK:
        BOT_ACTIVE = bool(active)
        return BOT_ACTIVE


def _control_keyboard():
    active = _bot_is_active()
    return {
        "inline_keyboard": [[
            {"text": "▶️ ACTIVAR BOT" if not active else "✅ BOT ACTIVO", "callback_data": "bot:start"},
            {"text": "⏹️ DETENER BOT" if active else "⏸️ BOT DETENIDO", "callback_data": "bot:stop"},
        ]]
    }


def _telegram_control_panel():
    """Publica el panel de control de operaciones en el chat configurado."""
    if not TOKEN or not CHAT:
        logging.warning("Botones de Telegram desactivados: falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return
    try:
        response = sess.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT,
                "text": "🎛️ CONTROL DEL BOT QUANT MODE\nEstado: " + ("ACTIVO" if _bot_is_active() else "DETENIDO") + "\n\nUsa los botones para permitir o pausar nuevas operaciones. El análisis continúa en segundo plano.",
                "reply_markup": _control_keyboard(),
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            logging.warning("No se pudo crear el panel de control Telegram: %s", payload)
    except requests.RequestException as exc:
        logging.warning("No se pudo enviar el panel de control Telegram: %s", exc)


def _telegram_control_worker():
    """Lee exclusivamente los botones de control del chat autorizado."""
    global TG_UPDATE_OFFSET
    if not TOKEN or not CHAT:
        return

    base = f"https://api.telegram.org/bot{TOKEN}"
    while True:
        try:
            response = sess.get(
                f"{base}/getUpdates",
                params={"offset": TG_UPDATE_OFFSET, "timeout": 20, "allowed_updates": json.dumps(["callback_query"])},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                time.sleep(2)
                continue

            for update in payload.get("result", []):
                TG_UPDATE_OFFSET = max(TG_UPDATE_OFFSET, int(update.get("update_id", 0)) + 1)
                callback = update.get("callback_query") or {}
                data = callback.get("data")
                message = callback.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if chat_id != str(CHAT) or data not in ("bot:start", "bot:stop"):
                    continue

                active = _set_bot_active(data == "bot:start")
                answer = "Bot ACTIVADO: puede abrir nuevas operaciones." if active else "Bot DETENIDO: no abrirá nuevas operaciones. El análisis continúa."
                try:
                    sess.post(
                        f"{base}/answerCallbackQuery",
                        json={"callback_query_id": callback.get("id"), "text": answer, "show_alert": False},
                        timeout=10,
                    )
                    sess.post(
                        f"{base}/editMessageText",
                        json={
                            "chat_id": CHAT,
                            "message_id": message.get("message_id"),
                            "text": "🎛️ CONTROL DEL BOT QUANT MODE\nEstado: " + ("ACTIVO" if active else "DETENIDO") + "\n\nUsa los botones para permitir o pausar nuevas operaciones. El análisis continúa en segundo plano.",
                            "reply_markup": _control_keyboard(),
                            "disable_web_page_preview": True,
                        },
                        timeout=10,
                    )
                    logging.info("CONTROL_TELEGRAM | bot=%s", "ACTIVO" if active else "DETENIDO")
                except requests.RequestException:
                    logging.exception("No se pudo actualizar el panel de control Telegram")
        except requests.RequestException as exc:
            logging.warning("Control Telegram sin conexión; reintento: %s", exc)
            time.sleep(3)
        except Exception:
            logging.exception("Error en el control Telegram")
            time.sleep(3)


def start_telegram_controls():
    global TG_CONTROL_STARTED
    if TG_CONTROL_STARTED or not TOKEN or not CHAT:
        return
    TG_CONTROL_STARTED = True
    _telegram_control_panel()
    threading.Thread(target=_telegram_control_worker, name="telegram-bot-controls", daemon=True).start()


OPENAPI_SPEC = {
    "openapi": "3.0.1",
    "info": {
        "title": "OTC Candles API",
        "version": "1.0.0",
        "description": (
            "API de solo lectura para consultar las velas M1 "
            "cerradas de EURUSD-OTC."
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
                "summary": "Obtiene las velas M1 cerradas",
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
                    "service": "OTC candles",
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
    """Inicia una sola vez el endpoint HTTP de Railway, incluso tras reconectar."""
    global API_SERVER
    with API_SERVER_LOCK:
        if API_SERVER is not None:
            return API_SERVER
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
        API_SERVER = server

        logging.info(
            "API de velas disponible en puerto %s | /health | /candles | /openapi.json",
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
    """Los reportes de análisis se mantienen en logs; no se envían a Telegram."""
    return
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
                return True, f"{pair} abierto en TURBO | expiracion 1m disponible"

            return False, f"{pair} TURBO cerrado/suspendido"

        return False, f"{pair} no aparece en TURBO; 1m bloqueado"

    except Exception as e:
        logging.exception(
            "No se pudo comprobar TURBO de %s",
            pair,
        )
        return False, f"error disponibilidad 1m: {type(e).__name__}: {e}"


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
    """Comprueba exclusivamente disponibilidad TURBO para expiracion 1m."""
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


def discover_otc_m1_pairs(iq):
    """Selecciona los seis pares OTC abiertos con preferencia a los pares configurados."""
    global PAIR_ROTATION_OFFSET
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

        available = set(found)
        # Prioriza los pares conocidos y completa la lista con otros OTC abiertos.
        # Orden estable para evitar cambios innecesarios entre actualizaciones.
        preferred = [pair for pair in PREFERRED_PAIRS if pair in available]
        others = sorted(pair for pair in available if pair not in set(preferred))
        all_pairs = preferred + others
        if not all_pairs:
            return []

        # Selecciona hasta seis pares disponibles; actualiza la lista cada 15 minutos.
        pairs = all_pairs[:MAX_OTC_PAIRS]

        logging.info(
            "Pares OTC TURBO/M1 encontrados: %d disponibles; seleccionados %d/%d | %s",
            len(all_pairs), len(pairs), MAX_OTC_PAIRS,
            ", ".join(pairs) if pairs else "NINGUNO",
        )
        return pairs

    except Exception:
        logging.exception("Error actualizando pares OTC de 1 minuto")
        return []

def execute_binary_order(iq, pair, signal, amount, expiration):
    """Envía una orden en DEMO y solo la acepta si IQ Option devuelve un ID válido."""
    try:
        if amount <= 0:
            return False, None, "Importe inválido: AMOUNT debe ser mayor que 0"
        if signal not in ("CALL", "PUT"):
            return False, None, f"Dirección inválida: {signal}"

        # Fuerza DEMO justo antes de comprar; nunca cambia a REAL.
        iq.change_balance("PRACTICE")
        result = iq.buy(amount, pair, signal.lower(), expiration)
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            return False, None, f"Respuesta inesperada de IQ Option: {result!r}"

        ok, response = result[0], result[1]
        # La API de IQ Option normalmente devuelve (True, id). No considerar
        # aceptada una respuesta True sin un identificador comprobable.
        if ok is not True or response in (None, False, 0, "", "None"):
            response_text = str(response)
            if "insufficient funds" in response_text.lower():
                return False, None, (
                    "Saldo insuficiente en PRACTICE. Reduce AMOUNT o verifica el saldo DEMO."
                )
            return False, None, f"IQ Option no confirmó la orden: ok={ok!r}, respuesta={response!r}"

        # Normaliza el ID, sin inventarlo ni usar el texto de la respuesta como ID.
        if isinstance(response, dict):
            raw_id = response.get("id", response.get("order_id"))
        else:
            raw_id = response
        try:
            order_id = str(int(raw_id))
        except (TypeError, ValueError, OverflowError):
            return False, None, f"IQ Option respondió sin ID numérico verificable: {response!r}"
        if int(order_id) <= 0:
            return False, None, f"ID de orden no válido: {order_id}"

        logging.info("ORDEN_CONFIRMADA_IQ | par=%s | dirección=%s | id=%s | cuenta=PRACTICE", pair, signal, order_id)
        return True, order_id, "IQ Option aceptó la orden y devolvió un ID válido"
    except Exception as exc:
        response_text = str(exc)
        if "insufficient funds" in response_text.lower():
            return False, None, "Saldo insuficiente en la cuenta PRACTICE."
        logging.exception("Error enviando orden %s %s", pair, signal)
        return False, None, f"{type(exc).__name__}: {exc}"


def main():
    global received_candles, PAIR, PAIRS

    logging.info("MAIN_INICIO | presión completa y reversión en apertura de siguiente M1 | DEMO | importe=%.2f | expiración=%dm", AMOUNT, EXPIRATION)
    start_candles_api()
    iq = connect_retry()
    _ensure_telegram_worker()
    start_telegram_controls()

    last_closed_stamp = {}
    last_order_candle = {}
    submitted_order_ids = set()
    order_lock = threading.Lock()
    last_heartbeat = 0.0
    last_pairs_refresh = time.monotonic()
    refresh_pairs_interval = 900.0
    # Limita a una sola entrada total por minuto de broker, no una por cada par.
    last_global_entry_minute = None
    last_scan_log = {}

    if AUTO_DISCOVER_PAIRS:
        logging.info("INICIO_DESCUBRIMIENTO | buscando 6 pares OTC TURBO")
        PAIRS = discover_otc_m1_pairs(iq)
    else:
        PAIRS = [PAIR]
    logging.info("PARES_ACTIVOS | cantidad=%d | pares=%s", len(PAIRS), ", ".join(PAIRS))

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()

            now_mono = time.monotonic()
            if now_mono - last_heartbeat >= 30.0:
                logging.info("CICLO_ACTIVO | pares=%d | analizando_aunque_haya_operaciones_abiertas", len(PAIRS))
                last_heartbeat = now_mono

            if not PAIRS:
                if AUTO_DISCOVER_PAIRS and now_mono - last_pairs_refresh >= 30.0:
                    discovered = discover_otc_m1_pairs(iq)
                    last_pairs_refresh = now_mono
                    if discovered:
                        PAIRS = discovered[:MAX_OTC_PAIRS]
                time.sleep(POLL)
                continue

            candidates = []
            broker_now = None
            # Siempre analiza los seis pares, aunque haya una operación en curso.
            for pair in list(PAIRS[:MAX_OTC_PAIRS]):
                try:
                    closed, current, server_now = get_candles(iq, pair)
                    broker_now = server_now if broker_now is None else max(broker_now, server_now)
                    if len(closed) < 13 or current is None:
                        continue

                    PAIR = pair
                    received_candles = build_candle_api_history(closed)
                    closed_stamp = closed[-1]["timestamp"]
                    last_closed_stamp[pair] = closed_stamp
                    second = int(server_now - current["timestamp"])
                    current_stamp = current["timestamp"]
                    res = analyze_market(closed[-13:], current=current, pair=pair)

                    # Reduce ruido de logs: una línea por par y vela de broker.
                    if last_scan_log.get(pair) != current_stamp:
                        logging.info(
                            "ANÁLISIS_REVERSIÓN | par=%s | presión=%s | señal=%s | score=%s | %s",
                            pair, res.get("pressure", "NEUTRAL"), res["signal"],
                            res.get("score", 0), res["reason"]
                        )
                        last_scan_log[pair] = current_stamp

                    if res["signal"] not in ("CALL", "PUT"):
                        continue
                    if res.get("entry_candle_timestamp") != current_stamp:
                        continue
                    # Solo tomar las configuraciones fuertes, y solo al inicio
                    # de la vela nueva: si la API llega tarde, se omite la entrada.
                    if res.get("score", 0) < int(os.getenv("MIN_OPPORTUNITY_SCORE", "75")):
                        continue
                    if not (0 <= second <= 1):
                        continue
                    if last_order_candle.get(pair) == current_stamp:
                        continue
                    res = dict(res)
                    res.update({"pair": pair, "second": second, "current_stamp": current_stamp})
                    candidates.append(res)

                except Exception:
                    logging.exception("Error analizando el par %s", pair)
                    continue

            # El botón DETENER pausa nuevas órdenes, pero mantiene el análisis de los pares.
            if not _bot_is_active():
                candidates = []

            # Escoge un único candidato global por minuto. La estrategia ya exige presión,
            # reversión cerrada, rechazo y ubicación antes de permitir una señal.
            if candidates and broker_now is not None:
                minute_key = int(broker_now // 60)
                if last_global_entry_minute != minute_key:
                    candidates.sort(key=lambda item: (item.get("score", 0), item.get("call_score", 0), item.get("put_score", 0)), reverse=True)
                    chosen = candidates[0]
                    pair = chosen["pair"]
                    signal = chosen["signal"]
                    current_stamp = chosen["current_stamp"]
                    with order_lock:
                        # Reservar antes de la llamada para evitar reintentos duplicados
                        # si la respuesta de la API es lenta o ambigua.
                        last_order_candle[pair] = current_stamp
                        last_global_entry_minute = minute_key
                        ok, oid, detail = execute_binary_order(iq, pair, signal, AMOUNT, EXPIRATION)

                    if ok and oid in submitted_order_ids:
                        ok = False
                        detail = f"ID duplicado bloqueado: {oid}"
                    if ok:
                        submitted_order_ids.add(oid)
                        message = (
                            "🧪 ORDEN ACEPTADA\n"
                            f"Par: {pair}\nDirección: {signal}\nImporte: {AMOUNT:g}\n"
                            f"Expiración: {EXPIRATION} minutos\nID de orden: {oid}\n"
                            f"Presión previa: {chosen.get('pressure', 'N/D')}\n"
                            f"Puntuación de oportunidad: {chosen.get('score', 0)}/100\n"
                            f"Ubicación: {chosen.get('location_score', 0)}/20 | "
                            f"Agotamiento: {chosen.get('exhaustion_score', 0)}/35 | "
                            f"Rechazo: {chosen.get('rejection_score', 0)}/20\n"
                            "\n📊 TRES VELAS ANTERIORES A LA ENTRADA\n"
                            + "\n".join(
                                _candle_summary(candle, idx)
                                for idx, candle in enumerate(chosen.get("candles_before_entry", []), start=1)
                            )
                        )
                        tg(message)
                        logging.info("MEJOR_SEÑAL_EJECUTADA | par=%s | dirección=%s | score=%s | id=%s", pair, signal, chosen.get("score", 0), oid)
                    else:
                        logging.warning("ORDEN_NO_CONFIRMADA | par=%s | señal=%s | detalle=%s", pair, signal, detail)
                else:
                    logging.debug("ENTRADA_GLOBAL_BLOQUEADA | ya hubo una entrada en minuto=%s", minute_key)

            # Actualizar seis pares cada 15 minutos; no espera a que las operaciones terminen.
            if AUTO_DISCOVER_PAIRS and now_mono - last_pairs_refresh >= refresh_pairs_interval:
                previous_pairs = list(PAIRS)
                refreshed = discover_otc_m1_pairs(iq)
                last_pairs_refresh = now_mono
                if refreshed:
                    PAIRS = list(refreshed[:MAX_OTC_PAIRS])
                    logging.info("ACTUALIZACION_PARES_15M | anteriores=%s | nuevos=%s", ", ".join(previous_pairs), ", ".join(PAIRS))
                else:
                    logging.warning("No se pudo actualizar la lista de pares; se conservan los actuales.")

        except Exception:
            logging.exception("Error de ciclo principal")
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
