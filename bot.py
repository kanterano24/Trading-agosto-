"""Bot EURUSD M1: REVERSIÓN sobre EURUSD normal (no OTC)."""

import logging
import os
import time
import re
import requests
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle, analyze_market


PAIR = "EURUSD"
TF = 60
COUNT = 200
EXPIRATION = 1
AMOUNT = 333.0

ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in ("1", "true", "yes", "si")
EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
POLL = max(0.5, float(os.getenv("POLL_SECONDS", "1")))
API_PORT = int(os.getenv("PORT", "8080"))
CANDLES_API_KEY = os.getenv("CANDLES_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

sess = requests.Session()
tg_last = 0.0



def connect():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL/IQ_PASSWORD en Railway")

    iq = IQ_Option(EMAIL, PASSWORD)
    ok, why = iq.connect()

    if not ok:
        raise RuntimeError(f"Conexión fallida: {why}")

    # Se mantiene PRACTICE para no cambiar otro comportamiento no solicitado.
    iq.change_balance("PRACTICE")
    logging.info("Conectado a PRACTICE")
    return iq


def connect_retry():
    delay = 5

    while True:
        try:
            return connect()
        except Exception as e:
            logging.exception("No conectó; reintento en %ss", delay)
            tg(
                f"⚠️ Conexión fallida ({type(e).__name__}); "
                f"reintento en {delay}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, 60)


def get_candles(iq):
    try:
        now = int(iq.get_server_timestamp())
    except Exception:
        now = int(time.time())

    raw = iq.get_candles(PAIR, TF, COUNT + 20, now) or []
    minute = now - now % TF

    closed = []

    for c in raw:
        try:
            start = int(c.get("from", c.get("at", 0)))
            if start > 0 and start + TF <= minute:
                closed.append(c)
        except (TypeError, ValueError):
            pass

    return normalize_candles(closed)[-COUNT:], now


def candle_message(candle, number):
    dt = datetime.fromtimestamp(candle["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")

    color = {
        "green": "VERDE",
        "red": "ROJA",
        "doji": "DOJI",
    }.get(candle["color"], candle["color"].upper())

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

            # Fuente de mercado: velas M1 cerradas de IQ Option.
            # No usa getUpdates, evitando conflicto con el receptor
            # de Telegram que ya tienes funcionando.
            cs, now = get_candles(iq)

            if len(cs) < 4:
                logging.info("Esperando historial M1: %d/4", len(cs))
                time.sleep(POLL)
                continue

            stamp = cs[-1]["timestamp"]

            # Solo procesa una vez cada vela cerrada.
            if stamp == last_candle:
                time.sleep(POLL)
                continue

            last_candle = stamp
            candle_number += 1

            # Publica la vela cerrada al grupo y la deja disponible
            # mediante /candles para la futura conexión con ChatGPT.
            tg(candle_message(cs[-1], candle_number))

            # Actualiza el conjunto de velas expuesto por la API.
            received_candles[:] = [
                dict(c, number=i + 1, time_text=datetime.fromtimestamp(
                    c["timestamp"]
                ).strftime("%Y-%m-%d %H:%M:%S"))
                for i, c in enumerate(cs)
            ]

            res = analyze_market(cs)

            logging.info(
                "%s señal=%s | motivo=%s",
                PAIR,
                res["signal"],
                res["reason"],
            )

            if res["signal"] in ("CALL", "PUT") and ENABLE_TRADES:
                iq.change_balance("PRACTICE")

                ok, oid = iq.buy(
                    AMOUNT,
                    PAIR,
                    res["signal"].lower(),
                    EXPIRATION,
                )

                logging.info(
                    "%s señal=%s | orden=%s | ok=%s | respuesta=%s",
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

            tg(f"⚠️ Error: {type(e).__name__}: {e}")

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
            logging.exception("Fallo principal; reinicio en 10s")
            time.sleep(10)
