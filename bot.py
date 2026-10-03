"""bot.py: al iniciar el servicio, recopila 200 velas cerradas M1 de GBPUSD-OTC
y envía el historial detallado a Telegram. No abre operaciones.
"""
import logging
import os
import time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle

PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC").strip().upper()
TIMEFRAME = 60
TARGET = max(200, int(os.getenv("CANDLE_COUNT_M1", "200")))
RETRY_SECONDS = max(5, int(os.getenv("RETRY_SECONDS", "20")))
IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def telegram_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en las variables de Railway.")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    response = requests.post(
        url, data={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=25
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rechazó el mensaje: {result}")
    return True


def connect():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD.")
    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = iq.connect()
    if not connected:
        raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")
    logging.info("Conectado a IQ Option.")
    return iq


def get_server_time(iq):
    try:
        return int(iq.get_server_timestamp())
    except Exception:
        return int(time.time())


def get_closed_history(iq):
    # Consultamos directamente el activo solicitado. No usamos get_all_open_time(),
    # que en algunas versiones dispara consultas digitales que fallan con None.
    now = get_server_time(iq)
    raw = iq.get_candles(PAIR, TIMEFRAME, TARGET + 15, now) or []
    current_minute = now - now % TIMEFRAME
    closed = []
    for candle in raw:
        try:
            start = int(candle.get("from", candle.get("at", 0)))
            if start > 0 and start + TIMEFRAME <= current_minute:
                closed.append(candle)
        except (TypeError, ValueError):
            continue
    return normalize_candles(closed)[-TARGET:]


def split_messages(items, max_chars=3600):
    """Agrupa velas sin exceder el límite práctico de Telegram."""
    batches, current = [], []
    for item in items:
        candidate = "\n\n".join(current + [item])
        if current and len(candidate) > max_chars:
            batches.append("\n\n".join(current))
            current = [item]
        else:
            current.append(item)
    if current:
        batches.append("\n\n".join(current))
    return batches


def send_full_history(candles):
    report = describe_history(candles)
    header = (
        f"📊 HISTORIAL COMPLETO M1 | {PAIR}\n"
        f"Velas cerradas: {report['count']}/{TARGET}\n"
        f"Verdes: {report['greens']} | Rojas: {report['reds']} | Doji: {report['dojis']}\n"
        f"Máximo del bloque: {report['highest']}\n"
        f"Mínimo del bloque: {report['lowest']}\n"
        f"Cambio neto: {report['net_change']:+.6f}\n"
        "Cada vela incluye OHLC, rango, cuerpo, ambas mechas y posición del cierre.\n"
        "Solo recopilación: operaciones desactivadas."
    )
    telegram_send(header)

    details = [format_candle(i, candle) for i, candle in enumerate(candles, 1)]
    batches = split_messages(details)
    total = len(batches)
    for n, batch in enumerate(batches, 1):
        telegram_send(f"🕯️ {PAIR} | Bloque {n}/{total}\n\n{batch}")
        time.sleep(0.4)
    logging.info("Enviadas %d velas en %d mensajes.", len(candles), total)


def main():
    iq = connect()
    telegram_send(
        f"🟡 Recolector iniciado al arrancar el servicio\n"
        f"Par solicitado: {PAIR}\nTemporalidad: M1\nObjetivo: {TARGET} velas cerradas\n"
        "Esperando datos; no se ejecutan operaciones."
    )

    while True:
        try:
            candles = get_closed_history(iq)
            logging.info("Velas cerradas recibidas para %s: %d/%d", PAIR, len(candles), TARGET)
            if len(candles) >= TARGET:
                send_full_history(candles)
                logging.info("Historial completo enviado a Telegram.")
                return
            logging.warning("Aún no hay suficientes velas. Se reintentará.")
        except Exception as exc:
            logging.exception("Fallo al recopilar/enviar historial.")
            try:
                telegram_send(f"⚠️ {PAIR}: error al obtener/enviar historial: {exc}")
            except Exception:
                logging.exception("También falló el aviso por Telegram.")
        time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
