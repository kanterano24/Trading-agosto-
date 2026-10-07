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
from strategy import normalize_candles, analyze_market


PAIR = "EURUSD-OTC"
TF = 60
COUNT = 200
EXPIRATION = 1
AMOUNT = 333.0

ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in (
    "1", "true", "yes", "si"
)

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
POLL = max(0.5, float(os.getenv("POLL_SECONDS", "1")))

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


def get_candles(iq):
    """Obtiene únicamente velas M1 ya cerradas desde IQ Option."""
    try:
        now = int(iq.get_server_timestamp())
    except Exception:
        now = int(time.time())

    raw = iq.get_candles(
        PAIR,
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
    Comprueba si el activo está disponible para opciones binarias
    antes de intentar iq.buy().

    get_all_open_time() es una consulta pesada, por eso se ejecuta
    solamente cuando la estrategia genera una señal.
    """
    try:
        all_assets = iq.get_all_open_time() or {}

        states = []

        for mode in ("turbo", "binary"):
            mode_data = all_assets.get(mode, {})
            asset = mode_data.get(pair)

            if isinstance(asset, dict):
                states.append(
                    (
                        mode,
                        bool(asset.get("open", False)),
                    )
                )

        if not states:
            return False, "EURUSD no aparece en turbo/binary"

        opened = [mode for mode, is_open in states if is_open]

        if opened:
            return True, f"activo abierto en {', '.join(opened)}"

        details = ", ".join(
            f"{mode}={'ABIERTO' if is_open else 'CERRADO'}"
            for mode, is_open in states
        )
        return False, details

    except Exception as e:
        logging.exception(
            "No se pudo comprobar disponibilidad de %s",
            pair,
        )
        return False, (
            f"no se pudo comprobar disponibilidad: "
            f"{type(e).__name__}: {e}"
        )


def wait_binary_asset(iq, pair, attempts=2, delay=0.35):
    """
    Comprueba disponibilidad inmediatamente antes de la entrada.
    Si el activo está cerrado/no disponible, NO llama iq.buy().
    """
    last_reason = "sin respuesta"

    for attempt in range(1, attempts + 1):
        is_open, reason = binary_asset_status(iq, pair)
        last_reason = reason

        logging.info(
            "Disponibilidad %s | intento=%d/%d | abierta=%s | %s",
            pair,
            attempt,
            attempts,
            is_open,
            reason,
        )

        if is_open:
            return True, reason

        if attempt < attempts:
            time.sleep(delay)

    return False, last_reason


def check_binary_availability(iq, pair):
    """Refresca activos y comprueba Binary/Turbo antes de comprar."""
    try:
        assets = iq.get_all_open_time() or {}
        states = []

        for mode in ("turbo", "binary"):
            info = assets.get(mode, {}).get(pair)
            if isinstance(info, dict):
                states.append((mode, bool(info.get("open", False))))

        if not states:
            return False, f"{pair} no aparece en binary/turbo"

        opened = [mode for mode, is_open in states if is_open]

        if opened:
            return True, f"{pair} abierto en {', '.join(opened)}"

        return False, ", ".join(
            f"{mode}={'ABIERTO' if is_open else 'CERRADO'}"
            for mode, is_open in states
        )

    except Exception as e:
        logging.exception("Error comprobando disponibilidad Binary de %s", pair)
        return False, f"error comprobando activo: {type(e).__name__}: {e}"


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
    global received_candles

    # IMPORTANTE:
    # Este bot NO llama getUpdates.
    # No compite con el receptor de Telegram que ya tienes.
    start_candles_api()

    iq = connect_retry()
    last_candle = None
    candle_number = 0

    tg(
        "🟢 Bot iniciado\n"
        f"Par: {PAIR}\n"
        "Mercado: EURUSD-OTC\n"
        "Temporalidad: M1\n"
        "Estrategia: REVERSIÓN\n"
        f"Expiración: {EXPIRATION} minuto\n"
        f"Importe: {AMOUNT:.0f} USD\n"
        "Indicadores: ninguno\n"
        "S/R: no\n"
        "Rechazo: no"
    )

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()

            cs, now = get_candles(iq)

            if len(cs) < 4:
                logging.info(
                    "Esperando historial M1: %d/4",
                    len(cs),
                )
                time.sleep(POLL)
                continue

            # Mantener todo el historial actual disponible en /candles.
            received_candles = build_candle_api_history(cs)

            stamp = cs[-1]["timestamp"]

            # Solo procesa una vez cada vela cerrada.
            if stamp == last_candle:
                time.sleep(POLL)
                continue

            last_candle = stamp
            candle_number += 1

            # Enviar cada nueva vela cerrada al grupo.
            tg(
                candle_message(
                    cs[-1],
                    candle_number,
                )
            )

            res = analyze_market(cs)

            logging.info(
                "%s señal=%s | motivo=%s",
                PAIR,
                res["signal"],
                res["reason"],
            )

            if (
                res["signal"] in ("CALL", "PUT")
                and ENABLE_TRADES
            ):
                ok, oid = execute_binary_order(
                    iq,
                    PAIR,
                    res["signal"],
                    AMOUNT,
                    EXPIRATION,
                )

                logging.info(
                    "%s señal=%s | orden=%s | ok=%s | "
                    "respuesta=%s",
                    PAIR,
                    res["signal"],
                    res["signal"],
                    ok,
                    oid,
                )

                tg(
                    f'{"🧪 Orden enviada" if ok else "⚠️ Orden rechazada"}\\n'
                    f"Par: {PAIR}\\n"
                    f"Dirección: {res['signal']}\\n"
                    f"Importe: {AMOUNT:.0f} USD\\n"
                    f"Expiración: {EXPIRATION} minuto\\n"
                    f"Motivo: {res['reason']}\\n"
                    f"ID/respuesta: {oid}"
                )

            tg(
                f"📈 {PAIR} M1\n"
                f"Señal: {res['signal']}\n"
                f"Motivo: {res['reason']}"
            )

        except Exception as e:
            logging.exception("Error de ciclo")

            tg(
                f"⚠️ Error: {type(e).__name__}: {e}"
            )

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
