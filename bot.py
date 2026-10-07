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


tg_update_offset = 0
received_candles = []
received_candle_stamps = set()


def parse_telegram_candles(update):
    """Extrae velas EURUSD de mensajes del grupo con el formato actual."""
    global received_candles

    try:
        message = update.get("message") or update.get("channel_post") or {}
        msg = message.get("text", "") or ""
        if "🕯️ VELA M1 CERRADA" not in msg or "Par: EURUSD" not in msg:
            return []

        blocks = re.split(r"(?=🕯️ VELA M1 CERRADA)", msg)
        parsed = []

        for block in blocks:
            if "🕯️ VELA M1 CERRADA" not in block:
                continue

            def val(pattern):
                m = re.search(pattern, block, re.MULTILINE)
                return float(m.group(1)) if m else None

            m_num = re.search(r"🕯️ VELA M1 CERRADA #(\d+)", block)
            m_time = re.search(r"Hora apertura:\s*(.+)", block)
            m_color = re.search(r"Color:\s*(.+)", block)

            if not (m_num and m_time and m_color):
                continue

            o = val(r"Apertura:\s*([0-9.eE+-]+)")
            cl = val(r"Cierre:\s*([0-9.eE+-]+)")
            hi = val(r"Máximo:\s*([0-9.eE+-]+)")
            lo = val(r"Mínimo:\s*([0-9.eE+-]+)")
            rng = val(r"Rango:\s*([0-9.eE+-]+)")
            body = val(r"Cuerpo:\s*([0-9.eE+-]+)")
            lw = val(r"Mecha inferior:\s*([0-9.eE+-]+)")
            uw = val(r"Mecha superior:\s*([0-9.eE+-]+)")

            if None in (o, cl, hi, lo):
                continue

            try:
                stamp = int(datetime.strptime(
                    m_time.group(1).strip(), "%Y-%m-%d %H:%M:%S"
                ).timestamp())
            except ValueError:
                continue

            color_txt = m_color.group(1).strip().upper()
            color = (
                "green" if color_txt == "VERDE"
                else "red" if color_txt == "ROJA"
                else "doji"
            )

            parsed.append({
                "number": int(m_num.group(1)),
                "timestamp": stamp,
                "time_text": m_time.group(1).strip(),
                "open": o,
                "close": cl,
                "high": hi,
                "low": lo,
                "min": lo,
                "max": hi,
                "range": rng if rng is not None else hi - lo,
                "body": body if body is not None else abs(cl - o),
                "lower_wick": lw if lw is not None else min(o, cl) - lo,
                "upper_wick": uw if uw is not None else hi - max(o, cl),
                "color": color,
            })

        return parsed

    except Exception:
        logging.exception("Error procesando mensaje de Telegram")
        return []


def poll_telegram_candles():
    """Lee mensajes nuevos del grupo mediante getUpdates."""
    global tg_update_offset

    if not TOKEN:
        return []

    try:
        r = sess.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={
                "offset": tg_update_offset,
                "timeout": 1,
                "allowed_updates": '["message","channel_post"]',
            },
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("ok"):
            return []

        new_candles = []

        for update in data.get("result", []):
            tg_update_offset = max(
                tg_update_offset, int(update.get("update_id", 0)) + 1
            )

            for candle in parse_telegram_candles(update):
                stamp = candle["timestamp"]
                if stamp not in received_candle_stamps:
                    received_candle_stamps.add(stamp)
                    received_candles.append(candle)
                    new_candles.append(candle)

        # Mantener solo el historial necesario para la estrategia.
        received_candles[:] = sorted(
            received_candles, key=lambda x: x["timestamp"]
        )[-COUNT:]

        return new_candles

    except Exception:
        logging.exception("Error recibiendo mensajes de Telegram")
        return []


def tg(msg):
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
    def _authorized(self):
        # Si no se configura clave, el endpoint queda accesible.
        # En Railway se recomienda configurar CANDLES_API_KEY.
        if not CANDLES_API_KEY:
            return True

        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {CANDLES_API_KEY}"

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(200, {"ok": True, "service": "EURUSD candles"})
            return

        if self.path.split("?", 1)[0] != "/candles":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        candles = []
        for c in received_candles[-COUNT:]:
            candles.append({
                "number": c.get("number"),
                "timestamp": c.get("timestamp"),
                "time": c.get("time_text"),
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
            })

        self._send_json(200, {
            "ok": True,
            "pair": PAIR,
            "timeframe": "M1",
            "count": len(candles),
            "candles": candles,
        })

    def log_message(self, format, *args):
        logging.info("HTTP %s - %s", self.address_string(), format % args)


def start_candles_api():
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), CandlesAPIHandler)

    import threading
    thread = threading.Thread(
        target=server.serve_forever,
        name="candles-api",
        daemon=True,
    )
    thread.start()

    logging.info(
        "API de velas disponible en puerto %s | /health | /candles",
        API_PORT,
    )
    return server



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

    # Inicializa el offset sin ejecutar operaciones con mensajes antiguos.
    poll_telegram_candles()

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

            # Las velas para la estrategia llegan desde los mensajes
            # de Telegram del grupo.
            new_candles = poll_telegram_candles()

            if not new_candles:
                time.sleep(POLL)
                continue

            for new_candle in new_candles:
                cs = list(received_candles)

                if len(cs) < 4:
                    logging.info(
                        "Esperando velas recibidas de Telegram: %d/4",
                        len(cs)
                    )
                    continue

                stamp = new_candle["timestamp"]

                # Solo procesa una vez cada vela cerrada.
                if stamp == last_candle:
                    continue

                last_candle = stamp
                candle_number = new_candle["number"]

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
