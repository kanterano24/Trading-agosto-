"""Recolector de 200 velas M1 GBPUSD-OTC. No ejecuta operaciones."""
import logging
import os
import time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle

TIMEFRAME = 60
CANDLE_COUNT = max(200, int(os.getenv("CANDLE_COUNT_M1", "200")))
POLL_SECONDS = max(5, float(os.getenv("POLL_SECONDS", "30")))
BATCH_SIZE = max(1, int(os.getenv("TELEGRAM_BATCH_SIZE", "15")))
PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC").upper()
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "PRACTICE").upper()
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
TG_TOKEN, TG_CHAT = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT:
        logging.error("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": message,
                                "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except requests.RequestException:
        logging.exception("Error de Telegram")
        return False


def connect_iq():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD")
    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    # Evita el fallo de la consulta digital interna de la librería:
    # el recolector no utiliza ese mercado y el método original puede devolver None.
    iq.get_digital_underlying_list_data = lambda: {"underlying": []}

    ok, reason = iq.connect()
    if not ok:
        raise RuntimeError(f"Conexión fallida: {reason}")
    if ACCOUNT_TYPE in ("PRACTICE", "REAL"):
        try:
            iq.change_balance(ACCOUNT_TYPE)
        except Exception:
            logging.exception("No se pudo cambiar la cuenta")
    logging.info("Conectado a IQ Option")
    return iq


def server_time(iq):
    try:
        return int(iq.get_server_timestamp())
    except Exception:
        return int(time.time())


def is_pair_open(iq):
    try:
        data = iq.get_all_open_time() or {}
        info = (data.get("binary") or {}).get(PAIR)
        return isinstance(info, dict) and info.get("open") is True
    except Exception:
        logging.exception("No se pudo consultar apertura de par")
        return False


def get_closed_candles(iq):
    now = server_time(iq)
    raw = iq.get_candles(PAIR, TIMEFRAME, CANDLE_COUNT + 10, now) or []
    minute_start = now - now % TIMEFRAME
    closed = []
    for c in raw:
        try:
            start = int(c.get("from", c.get("at", 0)))
            if start > 0 and start + TIMEFRAME <= minute_start:
                closed.append(c)
        except (TypeError, ValueError):
            pass
    return normalize_candles(closed)[-CANDLE_COUNT:]


def send_history(candles):
    info = describe_history(candles)
    telegram_send(
        f"📊 HISTORIAL M1 | {PAIR}\nVelas: {info['count']}/{CANDLE_COUNT}\n"
        f"Verdes: {info['greens']} | Rojas: {info['reds']} | Doji: {info['dojis']}\n"
        f"Máximo: {info['highest']} | Mínimo: {info['lowest']}\n"
        f"Cambio neto: {info['net_change']:+.6f}\n"
        "Solo recopilación; no se ejecutan operaciones."
    )
    lines = [format_candle(i, c) for i, c in enumerate(candles, 1)]
    for start in range(0, len(lines), BATCH_SIZE):
        batch = lines[start:start + BATCH_SIZE]
        end = start + len(batch)
        msg = (f"HISTORIAL M1 | Velas {start+1}-{end} de {len(lines)}\n"
               f"Par: {PAIR}\nN | Hora UTC | Color | Open | High | Low | Close | "
               f"Cuerpo% | Mecha sup | Mecha inf\n" + "\n".join(batch))
        telegram_send(msg)
        time.sleep(0.5)


def main():
    iq = connect_iq()
    telegram_send(f"🟡 Recolector M1 iniciado\nPar: {PAIR}\nObjetivo: {CANDLE_COUNT} velas cerradas")
    while True:
        try:
            if not is_pair_open(iq):
                logging.info("%s no figura abierto en binary; reintento.", PAIR)
                time.sleep(POLL_SECONDS)
                continue
            candles = get_closed_candles(iq)
            logging.info("Velas cerradas de %s: %d", PAIR, len(candles))
            if len(candles) < CANDLE_COUNT:
                time.sleep(POLL_SECONDS)
                continue
            send_history(candles)
            logging.info("Historial enviado.")
            break
        except Exception as exc:
            logging.exception("Error recopilando velas")
            telegram_send(f"⚠️ Error del recolector: {exc}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
