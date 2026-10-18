"""Bot OTC M5: analiza velas cerradas de 5 minutos y ejecuta en la siguiente vela M5.

- Obtiene únicamente velas M5 cerradas desde IQ Option.
- Telegram solo recibe confirmaciones de órdenes ejecutadas correctamente.
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
from strategy import normalize_candles, analyze_market


PAIR = os.getenv("PAIR", "ARBUSD-OTC").strip().upper()
# Analiza hasta 50 pares OTC que estén abiertos en TURBO.
# Los pares preferidos van primero si están disponibles; los demás se descubren
# automáticamente. La lista se vuelve a comprobar cada 15 minutos.
PREFERRED_PAIRS = ["ARBUSD-OTC", "ATOMUSD-OTC", "AUDCAD-OTC"]
PAIRS = []
MAX_OTC_PAIRS = 2
AUTO_DISCOVER_PAIRS = True
# La lista rota únicamente después de que los dos pares activos ejecuten
# una operación aceptada cada uno.
PAIR_ROTATION_OFFSET = 0
TF = 300
COUNT = 200
EXPIRATION = 5
# Importe predeterminado: mínimo habitual de IQ Option.
# Si Railway tiene AMOUNT configurado, esa variable prevalece.
AMOUNT = float(os.getenv("AMOUNT", "5"))
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
    if not message.startswith("🧪 ORDEN ENVIADA\nPar:"):
        return False
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


OPENAPI_SPEC = {
    "openapi": "3.0.1",
    "info": {
        "title": "OTC Candles API",
        "version": "1.0.0",
        "description": (
            "API de solo lectura para consultar las velas M5 "
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
                "summary": "Obtiene las velas M5 cerradas",
                "description": (
                    "Devuelve el historial actual de velas M5 "
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
                    "timeframe": "M5",
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
                "timeframe": "M5",
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
        f"🕯️ VELA M5 {state}{title_number}\n"
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
    en TURBO queda bloqueado para analisis y entrada de 5 minutos.
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
    """Comprueba TURBO inmediatamente antes de una entrada de 5 minutos."""
    last_reason = "sin respuesta"

    for attempt in range(1, attempts + 1):
        is_open, reason = binary_asset_status(iq, pair)

        logging.info(
            "Disponibilidad 5m %s | intento=%d/%d | abierta=%s | %s",
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


def discover_otc_5m_pairs(iq):
    """Selecciona dos pares OTC abiertos; avanza por la lista disponible en cada rotación."""
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

        # Selecciona dos pares consecutivos y rota a los siguientes en la próxima
        # llamada, evitando quedarse siempre con los mismos dos primeros.
        start = PAIR_ROTATION_OFFSET % len(all_pairs)
        end = start + MAX_OTC_PAIRS
        if end <= len(all_pairs):
            pairs = all_pairs[start:end]
        else:
            pairs = all_pairs[start:] + all_pairs[:end - len(all_pairs)]
        PAIR_ROTATION_OFFSET = (start + MAX_OTC_PAIRS) % len(all_pairs)

        logging.info(
            "Pares OTC TURBO/M5 encontrados: %d disponibles; seleccionados %d/%d | %s",
            len(all_pairs), len(pairs), MAX_OTC_PAIRS,
            ", ".join(pairs) if pairs else "NINGUNO",
        )
        return pairs

    except Exception:
        logging.exception("Error actualizando pares OTC de 5 minutos")
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

    logging.info("MAIN_INICIO | par=%s | M5 | demo | importe=%.2f | expiracion=%dm", PAIR, AMOUNT, EXPIRATION)
    start_candles_api()
    iq = connect_retry()
    _ensure_telegram_worker()

    last_closed_stamp = {}       # último cierre procesado por par
    last_reported_candle = {}    # limita los reportes rutinarios a uno por vela
    last_order_candle = {}       # evita duplicar una orden por par/vela
    last_heartbeat = 0.0
    last_rotation_attempt = 0.0
    cycle_traded_pairs = set()  # pares que ya ejecutaron una operación en el ciclo actual

    if AUTO_DISCOVER_PAIRS:
        logging.info("INICIO_DESCUBRIMIENTO | buscando pares OTC TURBO")
        discovered = discover_otc_5m_pairs(iq)
        PAIRS = discovered
    else:
        PAIRS = [PAIR]
    logging.info("PARES_ACTIVOS | modo=%s | cantidad=%d | pares=%s", "auto" if AUTO_DISCOVER_PAIRS else "fijo", len(PAIRS), ", ".join(PAIRS))
    if AUTO_DISCOVER_PAIRS and not discovered:
        logging.warning("No hay pares OTC disponibles en TURBO; se volverá a comprobar disponibilidad.")

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()

            now_mono = time.monotonic()
            if now_mono - last_heartbeat >= 30.0:
                logging.info("CICLO_ACTIVO | pares=%d | cola_telegram=%d", len(PAIRS), TG_QUEUE.qsize())
                last_heartbeat = now_mono
            if not PAIRS:
                time.sleep(POLL)
                continue

            # Los reportes de análisis se mantienen en los logs; Telegram solo acepta mensajes de ejecución.

            # Cada ciclo revisa únicamente los dos pares activos del ciclo actual.
            for pair in list(PAIRS):
                try:
                    logging.debug("CONSULTANDO_VELAS | par=%s", pair)
                    closed, current, server_now = get_candles(iq, pair)
                    if not closed:
                        logging.warning("SIN_VELAS_CERRADAS | par=%s; se reintentará en el siguiente ciclo", pair)
                        continue

                    PAIR = pair
                    received_candles = build_candle_api_history(closed)

                    logging.info("VELAS_RECIBIDAS | par=%s | cerradas=%d | actual=%s", pair, len(closed), current.get("timestamp") if current else "no disponible")
                    closed_stamp = closed[-1]["timestamp"]
                    if last_closed_stamp.get(pair) != closed_stamp:
                        last_closed_stamp[pair] = closed_stamp

                    if current is None or len(closed) < 10:
                        continue

                    second = int(server_now - current["timestamp"])
                    candle_stamp = current["timestamp"]

                    # La estrategia confirma con las últimas 10 velas M5 CERRADAS.
                    # La undécima vela M5 se reevalúa durante su formación para esperar
                    # confirmación en tiempo real; las órdenes siguen limitadas a una por vela.
                    # Evita ejecutar una señal antigua si IQ Option entregó velas
                    # desfasadas: la vela de entrada esperada debe ser la actual.
                    setup_candles = closed[-10:]
                    res = analyze_market(setup_candles, current=current, pair=pair)
                    sequence_text = "-".join(
                        {"green": "V", "red": "R", "doji": "D"}.get(x, "?")
                        for x in res.get("sequence", [])
                    )
                    logging.info(
                        "%s | segundo=%d | secuencia=%s | señal=%s | %s",
                        pair, second, sequence_text, res["signal"], res["reason"]
                    )

                    order_text = ""
                    # No perseguir una señal tardía: solo se permite entrada en los
                    # primeros 20 segundos de la undécima vela M5.
                    if res["signal"] in ("CALL", "PUT") and second > 20:
                        res["signal"] = "NO SIGNAL"
                        res["reason"] = (
                            "Entrada cancelada: la vela actual confirmó demasiado tarde "
                            f"(segundo {second}; límite 20s). Se espera la siguiente oportunidad."
                        )

                    if res["signal"] in ("CALL", "PUT") and pair in cycle_traded_pairs:
                        order_text = "\n⏸️ Par en pausa: ya ejecutó una operación en este ciclo; espera a que el otro par también opere."

                    if res["signal"] in ("CALL", "PUT") and pair not in cycle_traded_pairs:
                        expected_entry_stamp = res.get("entry_candle_timestamp")
                        if expected_entry_stamp != candle_stamp:
                            order_text = (
                                "\n⛔ ENTRADA BLOQUEADA: la señal no corresponde a la vela M5 actual."
                                f"\nVela esperada: {expected_entry_stamp}; vela actual: {candle_stamp}."
                            )
                        elif last_order_candle.get(pair) == candle_stamp:
                            order_text = "\n⛔ Orden duplicada bloqueada para esta vela."
                        elif not ENABLE_TRADES:
                            order_text = "\n🧪 Señal detectada; operaciones desactivadas."
                        else:
                            # Una sola orden por par y vela M5; cada par solo puede
                            # ejecutar una operación aceptada por ciclo de dos pares.
                            last_order_candle[pair] = candle_stamp
                            ok, oid = execute_binary_order(
                                iq, pair, res["signal"], AMOUNT, EXPIRATION
                            )
                            order_text = (
                                f"\n\n{'🧪 ORDEN ENVIADA' if ok else '⚠️ ORDEN RECHAZADA'}"
                                f"\nPar: {pair}\nDirección: {res['signal']}"
                                f"\nImporte: {AMOUNT:g}\nExpiración: {EXPIRATION} minutos"
                                f"\nID/respuesta: {oid}"
                            )
                            # Un único mensaje por Telegram, únicamente si IQ Option
                            # confirmó que la orden fue aceptada.
                            if ok:
                                tg(
                                    "🧪 ORDEN ENVIADA\n"
                                    f"Par: {pair}\n"
                                    f"Dirección: {res['signal']}\n"
                                    f"Importe: {AMOUNT:g}\n"
                                    f"Expiración: {EXPIRATION} minutos\n"
                                    f"ID/respuesta: {oid}"
                                )
                                cycle_traded_pairs.add(pair)
                                logging.info(
                                    "OPERACION_CICLO | par=%s | pares_con_operacion=%d/%d",
                                    pair, len(cycle_traded_pairs), len(PAIRS)
                                )


                except Exception:
                    logging.exception("Error procesando el par %s", pair)
                    continue

            # La lista de dos pares solo se actualiza cuando AMBOS pares activos
            # han ejecutado una operación aceptada. Hasta entonces, un par que ya
            # operó queda en pausa y el bot sigue esperando al segundo.
            active_pair_set = set(PAIRS)
            if (AUTO_DISCOVER_PAIRS and len(active_pair_set) == 2
                    and active_pair_set.issubset(cycle_traded_pairs)
                    and now_mono - last_rotation_attempt >= 15.0):
                previous_pairs = list(PAIRS)
                last_rotation_attempt = now_mono
                refreshed = discover_otc_5m_pairs(iq)
                if refreshed:
                    PAIRS = list(refreshed[:MAX_OTC_PAIRS])
                    cycle_traded_pairs.clear()
                    active_set = set(PAIRS)
                    for state in (last_closed_stamp, last_reported_candle, last_order_candle):
                        for old_pair in list(state):
                            if old_pair not in active_set:
                                state.pop(old_pair, None)
                    logging.info(
                        "ROTACION_PARES tras operar ambos | anteriores=%s | nuevos=%s",
                        ", ".join(previous_pairs), ", ".join(PAIRS)
                    )
                else:
                    logging.warning(
                        "Ambos pares ya operaron, pero no se pudo actualizar la lista; "
                        "se conservan los pares actuales y se reintentará."
                    )

            # Telegram solo recibe el aviso de una orden aceptada por IQ Option.

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
