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
from strategy import normalize_candles, analyze_market, STOCH_K_PERIOD, STOCH_K_SMOOTH, STOCH_D_PERIOD, STOCH_OVERBOUGHT, STOCH_OVERSOLD


PAIR = "EURUSD-OTC"
PAIRS = []
MAX_OTC_PAIRS = max(1, min(3, int(os.getenv("MAX_OTC_PAIRS", "3"))))
PAIR_REFRESH_SECONDS = 300.0
TF = 60
COUNT = 200
EXPIRATION = 5
# Importe predeterminado: mínimo habitual de IQ Option.
# Si Railway tiene AMOUNT configurado, esa variable prevalece.
AMOUNT = float(os.getenv("AMOUNT", "3"))
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

    last_closed_stamp = {}       # último cierre procesado por par
    last_evaluated_candle = {}   # evita evaluar dos veces la misma vela
    last_order_candle = {}       # evita duplicar una orden por par/vela
    last_global_order_candle = None  # máximo una orden intentada en todo el bot por minuto M1
    pending_signals = {}  # señales confirmadas que esperan su vela de entrada programada
    last_pair_refresh = 0.0

    PAIRS = discover_otc_1m_pairs(iq)
    if not PAIRS:
        tg("⚠️ No se encontraron pares OTC abiertos en TURBO. El bot seguirá intentando actualizar la lista.")

    tg(
        "🟢 Bot iniciado\n"
        f"Pares OTC configurados: hasta {MAX_OTC_PAIRS}\n"
        f"Pares disponibles al iniciar: {len(PAIRS)}\n"
        "Temporalidad de análisis: M1\n"
        "Estrategia: cruce estocástico en zona extrema + color de vela\n"
        "CALL: K cruza arriba de D en sobreventa 20 y vela verde\n"
        "PUT: K cruza abajo de D en sobrecompra 80 y vela roja\n"
        f"Estocástico: K={STOCH_K_PERIOD}, suavizado={STOCH_K_SMOOTH}, D={STOCH_D_PERIOD}, niveles={STOCH_OVERSOLD}/{STOCH_OVERBOUGHT}\n"
        f"Entrada: segunda vela posterior a la señal M1 | Expiración: {EXPIRATION} minutos\n"
        f"Importe por operación: {AMOUNT:g}\n"
        "Indicadores: ninguno | S/R: no | Rechazo: no"
    )

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()
                last_pair_refresh = 0.0

            now_mono = time.monotonic()
            if now_mono - last_pair_refresh >= PAIR_REFRESH_SECONDS:
                refreshed = discover_otc_1m_pairs(iq)
                if refreshed:
                    old = set(PAIRS)
                    PAIRS = refreshed
                    # Elimina estados de pares que ya no están disponibles.
                    active_set = set(PAIRS)
                    for state in (last_closed_stamp, last_evaluated_candle, last_order_candle):
                        for old_pair in list(state):
                            if old_pair not in active_set:
                                state.pop(old_pair, None)
                    if set(PAIRS) != old:
                        logging.info("Lista OTC actualizada: %d pares | %s", len(PAIRS), ", ".join(PAIRS))
                        tg(f"🔄 Pares OTC actualizados: {len(PAIRS)}\n" + ", ".join(PAIRS))
                else:
                    logging.warning("No se pudo actualizar la lista OTC; se conserva la lista anterior")
                last_pair_refresh = now_mono

            if not PAIRS:
                time.sleep(POLL)
                continue

            # Se agrupan los informes del ciclo para enviar varias velas por mensaje
            # y evitar que Telegram se sature cuando se analizan hasta 50 pares.
            telegram_reports = []

            # Cada ciclo revisa todos los pares descubiertos, no solo EURUSD-OTC.
            # La disponibilidad cambia; discover_otc_1m_pairs filtra activos TURBO abiertos.
            for pair in list(PAIRS):
                try:
                    closed, current, server_now = get_candles(iq, pair)
                    if not closed:
                        continue

                    PAIR = pair
                    received_candles = build_candle_api_history(closed)

                    closed_stamp = closed[-1]["timestamp"]
                    if last_closed_stamp.get(pair) != closed_stamp:
                        last_closed_stamp[pair] = closed_stamp
                        telegram_reports.append(
                            candle_message(closed[-1], len(closed), pair=pair)
                            + f"\nHistorial cerrado disponible: {len(closed)} velas"
                        )

                    if current is None or len(closed) < 30:
                        continue

                    second = int(server_now - current["timestamp"])
                    candle_stamp = current["timestamp"]
                    if last_evaluated_candle.get(pair) == candle_stamp:
                        continue
                    last_evaluated_candle[pair] = candle_stamp

                    # Primero ejecuta una señal pendiente en la segunda vela posterior
                    # a la vela de señal: señal S, espera el cierre de S+1, entra en S+2.
                    pending = pending_signals.get(pair)
                    order_text = ""
                    if pending and pending.get("entry_candle_timestamp") == candle_stamp:
                        if last_global_order_candle == candle_stamp:
                            order_text = "⛔ Entrada bloqueada: ya se intentó una operación global en este minuto M1."
                        elif not ENABLE_TRADES:
                            order_text = "🧪 Señal pendiente válida; operaciones desactivadas."
                        else:
                            last_global_order_candle = candle_stamp
                            last_order_candle[pair] = candle_stamp
                            ok, oid = execute_binary_order(iq, pair, pending["signal"], AMOUNT, EXPIRATION)
                            order_text = (
                                f"{'🧪 ORDEN ENVIADA' if ok else '⚠️ ORDEN RECHAZADA'}\n"
                                f"Par: {pair}\nDirección: {pending['signal']}\nImporte: {AMOUNT:g}\n"
                                f"Expiración: {EXPIRATION} minutos\nID/respuesta: {oid}"
                            )
                        pending_signals.pop(pair, None)
                        telegram_reports.append(
                            f"🎯 EJECUCIÓN PROGRAMADA\nPar: {pair}\nSeñal original: {pending['signal']}\n"
                            f"Vela de señal: {pending['signal_candle_timestamp']}\n"
                            f"Vela de entrada: {candle_stamp}\n{order_text}"
                        )
                    elif pending and candle_stamp > pending.get("entry_candle_timestamp", 0):
                        # Si se perdió la apertura programada, no se persigue una entrada tardía.
                        pending_signals.pop(pair, None)
                        telegram_reports.append(f"⛔ Señal caducada en {pair}: pasó la vela programada; no se persigue la entrada.")

                    # La señal se calcula solo sobre velas cerradas. Al confirmarse en S,
                    # se programa entrada en S+2; S+1 debe cerrar primero.
                    res = analyze_market(closed)
                    sequence_text = res.get("sequence_text", "")
                    logging.info("%s | segundo=%d | estocástico K=%s D=%s | señal=%s | %s",
                                 pair, second, res.get("stoch_k"), res.get("stoch_d"), res["signal"], res["reason"])

                    signal_stamp = res.get("signal_candle_timestamp")
                    if res["signal"] in ("CALL", "PUT") and signal_stamp == closed[-1]["timestamp"]:
                        # Evita volver a registrar la misma señal si el ciclo se repite.
                        existing = pending_signals.get(pair)
                        if not existing or existing.get("signal_candle_timestamp") != signal_stamp:
                            pending_signals[pair] = {
                                "signal": res["signal"],
                                "signal_candle_timestamp": signal_stamp,
                                "entry_candle_timestamp": signal_stamp + 2 * TF,
                                "reason": res["reason"],
                            }
                            telegram_reports.append(
                                f"📌 SEÑAL ESTOCÁSTICA CONFIRMADA\nPar: {pair}\nSeñal: {res['signal']}\n"
                                f"Vela señal cerrada: {signal_stamp}\nK={res.get('stoch_k'):.2f} | D={res.get('stoch_d'):.2f}\n"
                                f"Entrada programada: {signal_stamp + 2 * TF} (segunda vela posterior)\n{res['reason']}"
                            )

                    # Reporte de vela cerrada y valores del estocástico, sin mensajes por cada polling.
                    telegram_reports.append(
                        f"📊 ANÁLISIS M1 | {pair}\nVela cerrada: {closed[-1]['timestamp']}\n"
                        f"Color: {closed[-1]['color']} | Cierre: {_fmt_price(closed[-1]['close'])}\n"
                        f"Estocástico K={res.get('stoch_k')} D={res.get('stoch_d')}\n"
                        f"Señal: {res['signal']}\nMotivo: {res['reason']}"
                    )

                except Exception:
                    logging.exception("Error procesando el par %s", pair)
                    continue

            # Un lote por ciclo (dividido en mensajes seguros para Telegram).
            # Esto reduce drásticamente el número de envíos frente a 50 mensajes
            # individuales por cada vela/par.
            send_telegram_batches(telegram_reports)

        except Exception:
            logging.exception("Error de ciclo principal")
            try:
                if not iq.check_connect():
                    iq = connect_retry()
                    last_pair_refresh = 0.0
            except Exception:
                iq = connect_retry()
                last_pair_refresh = 0.0

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
