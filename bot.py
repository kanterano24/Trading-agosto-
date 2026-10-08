"""Bot EURUSD-OTC M1: REVERSIÓN sobre EURUSD-OTC.

- Obtiene únicamente velas M1 cerradas desde IQ Option.
- Envía cada vela cerrada a Telegram.
- NO usa Telegram getUpdates.
- Expone /health, /candles y /openapi.json para consultas externas.
- /candles puede protegerse con CANDLES_API_KEY.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from iqoptionapi.stable_api import IQ_Option
from iqoptionapi import constants as OP_code
from strategy import normalize_candles, analyze_market


PAIR = "EURUSD-OTC"
PAIRS = []
MAX_OTC_PAIRS = 50
PAIR_REFRESH_SECONDS = 300.0
TF = 60
COUNT = 200
EXPIRATION = 2
AMOUNT = 333.0

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

# Historial actual que queda expuesto por /candles.
received_candles = []


def tg(msg):
    """Envía mensajes al grupo. NO usa getUpdates."""
    global tg_last

    if not TOKEN or not CHAT:
        return False

    try:
        wait = 1.2 - (time.monotonic() - tg_last)
        if wait > 0:
            time.sleep(wait)

        r = sess.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": msg},
            timeout=15,
        )
        r.raise_for_status()

        if not r.json().get("ok"):
            raise RuntimeError(str(r.json()))

        tg_last = time.monotonic()
        return True

    except Exception:
        logging.exception("Error Telegram")
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
    """Obtiene únicamente velas M1 ya cerradas desde IQ Option."""
    try:
        now = int(iq.get_server_timestamp())
    except Exception:
        now = int(time.time())

    raw = iq.get_candles(
        pair,
        TF,
        COUNT + 20,
        now,
    ) or []

    minute = now - now % TF
    closed = []

    for c in raw:
        try:
            start = int(
                c.get(
                    "from",
                    c.get("at", 0),
                )
            )

            if start > 0 and start + TF <= minute:
                closed.append(c)

        except (TypeError, ValueError):
            pass

    return normalize_candles(closed)[-COUNT:], now


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


def candle_message(candle, number):
    dt = datetime.fromtimestamp(
        candle["timestamp"]
    ).strftime("%Y-%m-%d %H:%M:%S")

    color = {
        "green": "VERDE",
        "red": "ROJA",
        "doji": "DOJI",
    }.get(
        candle["color"],
        candle["color"].upper(),
    )

    return (
        f"🕯️ VELA M1 CERRADA #{number}\n"
        f"Par: {PAIR}\n"
        f"Hora apertura: {dt}\n"
        f"Color: {color}\n"
        f"Apertura: {candle['open']}\n"
        f"Cierre: {candle['close']}\n"
        f"Máximo: {candle['high']}\n"
        f"Mínimo: {candle['low']}\n"
        f"Rango: {candle['range']}\n"
        f"Cuerpo: {candle['body']}\n"
        f"Mecha inferior: {candle['lower_wick']}\n"
        f"Mecha superior: {candle['upper_wick']}"
    )


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

        mode_data = data.get("turbo", {})
        actives = mode_data.get("actives", {})

        if not isinstance(actives, dict):
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


def discover_otc_1m_pairs(iq):
    """
    Descubre hasta 50 pares OTC abiertos en TURBO.
    Solo estos pares se analizan y pueden recibir entradas de 1 minuto.
    """
    try:
        data = iq.get_all_init_v2()

        if not isinstance(data, dict):
            logging.warning("No se recibieron datos de inicializacion")
            return []

        turbo = data.get("turbo", {})
        actives = turbo.get("actives", {})

        if not isinstance(actives, dict):
            return []

        found = []

        for aid, active in actives.items():
            if not isinstance(active, dict):
                continue

            name = str(active.get("name", ""))
            parts = name.split(".")
            pair = parts[-1] if parts else name

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
            "Pares OTC con expiracion 1m disponibles: %d/%d | %s",
            len(pairs),
            MAX_OTC_PAIRS,
            ", ".join(pairs),
        )

        return pairs

    except Exception:
        logging.exception("Error actualizando pares OTC de 1 minuto")
        return []

def execute_binary_order(iq, pair, signal, amount, expiration):
    """
    Intenta la entrada hasta 3 veces.
    Si IQ Option devuelve 'asset is not available', refresca el estado
    del activo y vuelve a intentar inmediatamente.
    """
    action = signal.lower()
    last_response = None

    for attempt in range(1, 4):
        is_open, status = check_binary_availability(iq, pair)

        logging.info(
            "PRE-ORDEN %s | intento=%d/3 | signal=%s | disponible=%s | %s",
            pair, attempt, signal, is_open, status,
        )

        if not is_open:
            last_response = f"Activo no disponible: {status}"
        else:
            try:
                iq.change_balance("PRACTICE")
                ok, oid = iq.buy(
                    amount,
                    pair,
                    action,
                    expiration,
                )

                if ok:
                    return True, oid

                last_response = str(oid)

                if "asset is not available" not in last_response.lower():
                    return False, last_response

            except Exception as e:
                last_response = f"{type(e).__name__}: {e}"
                logging.exception(
                    "Excepción enviando BUY %s | intento=%d/3",
                    pair, attempt,
                )

        if attempt < 3:
            time.sleep(0.25)

    return False, last_response or "sin respuesta de IQ Option"


def main():
    global received_candles, PAIR, PAIRS

    # IMPORTANTE:
    # Este bot NO llama getUpdates.
    # No compite con el receptor de Telegram que ya tienes.
    start_candles_api()

    iq = connect_retry()
    last_candles = {}
    candle_numbers = {}
    last_pair_refresh = 0.0
    last_minute = None

    PAIRS = discover_otc_1m_pairs(iq)

    tg(
        "🟢 Bot iniciado\n"
        f"Pares OTC analizados: hasta {MAX_OTC_PAIRS}\n"
        f"Pares disponibles 1m: {len(PAIRS)}\n"
        "Temporalidad: M1\n"
        "Estrategia: REVERSIÓN POR AGOTAMIENTO DE IMPULSO\n"
        f"Expiración: {EXPIRATION} minuto\n"
        f"Importe: {AMOUNT:.0f} USD\n"
        "Indicadores: ninguno\n"
        "S/R: no\n"
        "Rechazo: no\n"
        "Actualización de pares: cada 5 minutos"
    )

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()
                PAIRS = discover_otc_1m_pairs(iq)
                last_pair_refresh = time.monotonic()
                last_minute = None

            now_mono = time.monotonic()

            # Actualiza la lista de pares exactamente cada 5 minutos.
            if now_mono - last_pair_refresh >= PAIR_REFRESH_SECONDS:
                refreshed = discover_otc_1m_pairs(iq)

                if refreshed:
                    PAIRS = refreshed
                    tg(
                        f"🔄 Pares OTC actualizados\n"
                        f"Disponibles con expiración 1m: {len(PAIRS)}\n"
                        f"Analizando: {', '.join(PAIRS)}"
                    )
                else:
                    logging.warning(
                        "Actualizacion sin pares validos; se conserva la lista anterior"
                    )

                last_pair_refresh = now_mono

            if not PAIRS:
                logging.warning(
                    "No hay pares OTC con expiracion 1m disponibles; "
                    "analisis y entradas bloqueados"
                )
                time.sleep(POLL)
                continue

            # Procesa cada vela M1 cerrada una sola vez.
            server_now = int(iq.get_server_timestamp())
            current_minute = server_now - server_now % TF

            if current_minute == last_minute:
                time.sleep(POLL)
                continue

            last_minute = current_minute

            for pair in list(PAIRS):
                try:
                    # Verificacion final: si el par ya no tiene TURBO/1m,
                    # se bloquean tanto el analisis como la entrada.
                    is_open, availability_reason = binary_asset_status(
                        iq,
                        pair,
                    )

                    if not is_open:
                        logging.info(
                            "%s | ANALISIS BLOQUEADO | %s",
                            pair,
                            availability_reason,
                        )
                        continue

                    cs, _ = get_candles(iq, pair)

                    if len(cs) < 4:
                        logging.info(
                            "%s | Esperando historial M1: %d/4",
                            pair,
                            len(cs),
                        )
                        continue

                    stamp = cs[-1]["timestamp"]

                    if stamp == last_candles.get(pair):
                        continue

                    last_candles[pair] = stamp
                    candle_numbers[pair] = candle_numbers.get(pair, 0) + 1

                    # Mantener la API apuntando al ultimo par procesado,
                    # conservando el formato original del endpoint.
                    PAIR = pair
                    received_candles = build_candle_api_history(cs)

                    tg(
                        candle_message(
                            cs[-1],
                            candle_numbers[pair],
                        )
                    )

                    res = analyze_market(cs)

                    logging.info(
                        "%s señal=%s | motivo=%s",
                        pair,
                        res["signal"],
                        res["reason"],
                    )

                    # La entrada queda bloqueada si el par no tiene 1m
                    # disponible justo antes de enviar la orden.
                    if (
                        res["signal"] in ("CALL", "PUT")
                        and ENABLE_TRADES
                    ):
                        is_open, availability_reason = wait_binary_asset(
                            iq,
                            pair,
                        )

                        if not is_open:
                            logging.info(
                                "%s | ENTRADA BLOQUEADA | %s",
                                pair,
                                availability_reason,
                            )
                            tg(
                                f"⛔ Entrada bloqueada\n"
                                f"Par: {pair}\n"
                                f"Motivo: {availability_reason}\n"
                                f"Expiración requerida: {EXPIRATION} minuto"
                            )
                        else:
                            ok, oid = execute_binary_order(
                                iq,
                                pair,
                                res["signal"],
                                AMOUNT,
                                EXPIRATION,
                            )

                            logging.info(
                                "%s señal=%s | orden=%s | ok=%s | respuesta=%s",
                                pair,
                                res["signal"],
                                res["signal"],
                                ok,
                                oid,
                            )

                            tg(
                                f'{"🧪 Orden enviada" if ok else "⚠️ Orden rechazada"}\n'
                                f"Par: {pair}\n"
                                f"Dirección: {res['signal']}\n"
                                f"Importe: {AMOUNT:.0f} USD\n"
                                f"Expiración: {EXPIRATION} minuto\n"
                                f"Motivo: {res['reason']}\n"
                                f"ID/respuesta: {oid}"
                            )

                    tg(
                        f"📈 {pair} M1\n"
                        f"Señal: {res['signal']}\n"
                        f"Motivo: {res['reason']}"
                    )

                except Exception as e:
                    logging.exception(
                        "Error procesando %s",
                        pair,
                    )
                    tg(
                        f"⚠️ Error procesando {pair}: "
                        f"{type(e).__name__}: {e}"
                    )

        except Exception as e:
            logging.exception("Error de ciclo")

            tg(
                f"⚠️ Error: {type(e).__name__}: {e}"
            )

            try:
                if not iq.check_connect():
                    iq = connect_retry()
                    PAIRS = discover_otc_1m_pairs(iq)
                    last_pair_refresh = time.monotonic()
                    last_minute = None
            except Exception:
                iq = connect_retry()
                PAIRS = discover_otc_1m_pairs(iq)
                last_pair_refresh = time.monotonic()
                last_minute = None

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
