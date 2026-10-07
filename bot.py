"""Bot EURUSD M1: REVERSIÓN sobre EURUSD normal (no OTC)."""

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


PAIR = "EURUSD"
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

# Historial que queda expuesto por /candles.
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


class CandlesAPIHandler(BaseHTTPRequestHandler):
    """API de solo lectura para que un cliente autorizado consulte las velas."""

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
            self._send_json(
                200,
                {
                    "openapi": "3.0.1",
                    "info": {
                        "title": "EURUSD Candles API",
                        "version": "1.0.0",
                        "description": "API de solo lectura para consultar velas M1 cerradas de EURUSD.",
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
                                "summary": "Comprobar estado de la API",
                                "responses": {
                                    "200": {
                                        "description": "API disponible"
                                    }
                                }
                            }
                        },
                        "/candles": {
                            "get": {
                                "operationId": "getCandles",
                                "summary": "Obtener velas M1 cerradas de EURUSD",
                                "responses": {
                                    "200": {
                                        "description": "Velas M1 cerradas",
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {
                                                        "ok": {"type": "boolean"},
                                                        "pair": {"type": "string"},
                                                        "timeframe": {"type": "string"},
                                                        "count": {"type": "integer"},
                                                        "candles": {
                                                            "type": "array",
                                                            "items": {
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
                                                                    "upper_wick": {"type": "number"}
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    "401": {
                                        "description": "No autorizado"
                                    }
                                }
                            }
                        }
                    }
                },
            )
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
    ok, why = iq.connect()

    if not ok:
        raise RuntimeError(
            f"Conexión fallida: {why}"
        )

    # Se mantiene PRACTICE como en la configuración anterior.
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
        "Mercado: EURUSD normal (NO OTC)\n"
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
                iq.change_balance("PRACTICE")

                ok, oid = iq.buy(
                    AMOUNT,
                    PAIR,
                    res["signal"].lower(),
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
                    f'{"🧪 Orden enviada" if ok else "⚠️ Orden rechazada"}\n'
                    f"Par: {PAIR}\n"
                    f"Dirección: {res['signal']}\n"
                    f"Importe: {AMOUNT:.0f} USD\n"
                    f"Expiración: {EXPIRATION} minuto\n"
                    f"Motivo: {res['reason']}\n"
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
