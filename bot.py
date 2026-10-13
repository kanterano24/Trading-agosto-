"""bot.py — Analizador de velas cerradas M1 de un solo par IQ Option.
Envía a Telegram cada nueva vela cerrada, sus medidas y sus clasificaciones.
No usa indicadores, S/R, señales de entrada ni abre operaciones.
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, classify_candles, describe_history, format_candle

PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC").strip().upper()
TIMEFRAME = 60
HISTORY_COUNT = max(100, int(os.getenv("CANDLE_COUNT_M1", "500")))
POLL_SECONDS = max(0.5, float(os.getenv("POLL_SECONDS", "1")))
RETRY_SECONDS = max(5, int(os.getenv("RETRY_SECONDS", "15")))
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SEND_INITIAL_HISTORY = os.getenv("SEND_INITIAL_HISTORY", "true").lower() in {"1", "true", "yes", "on"}
SUMMARY_EVERY = max(1, int(os.getenv("SUMMARY_EVERY", "10")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("candle_analyzer")


def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram no configurado; mensaje omitido.")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Telegram permite hasta 4096 caracteres; dejamos margen.
    chunks = []
    while len(message) > 3900:
        cut = message.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(message[:cut])
        message = message[cut:].lstrip()
    chunks.append(message)
    for chunk in chunks:
        response = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram rechazó el mensaje: {result}")
        time.sleep(1.1)
    return True


def connect():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD en las variables de Railway.")
    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = iq.connect()
    if not connected:
        raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")
    log.info("Conectado a IQ Option; par único: %s", PAIR)
    return iq


def server_time(iq):
    try:
        return int(iq.get_server_timestamp())
    except Exception:
        return int(time.time())


def get_closed_candles(iq):
    now = server_time(iq)
    raw = iq.get_candles(PAIR, TIMEFRAME, HISTORY_COUNT + 10, now) or []
    current_minute = now - now % TIMEFRAME
    closed = []
    for candle in raw:
        try:
            start = int(float(candle.get("from", candle.get("at", 0))))
            # Una vela se considera cerrada solo cuando acabó antes del minuto actual.
            if start > 0 and start + TIMEFRAME <= current_minute:
                closed.append(candle)
        except (TypeError, ValueError, AttributeError):
            continue
    return normalize_candles(closed)[-HISTORY_COUNT:]


def candle_message(candle, index, total):
    item = classify_candles([candle])[0]
    # Para clasificaciones contextuales (continuidad, rupturas, inside/outside)
    # se clasifica la historia completa en main y se pasa el objeto ya etiquetado.
    return (
        f"🕯 VELA M1 CERRADA | {PAIR}\n"
        f"Historial: vela {index} | {total} velas recibidas\n"
        f"{format_candle(index, item)}"
    )


def initial_history_message(candles):
    info = describe_history(candles)
    labels = sorted(info["label_counts"].items(), key=lambda x: (-x[1], x[0]))
    counts = "\n".join(f"• {name}: {count}" for name, count in labels[:18]) or "• Sin clasificaciones"
    return (
        f"📚 HISTORIAL M1 INICIAL | {PAIR}\n"
        f"Velas cerradas analizadas: {info['count']}\n"
        f"Verdes: {info['greens']} | Rojas: {info['reds']} | Doji: {info['dojis']}\n"
        f"Variación neta: {info['net_change']:.8f}\n"
        f"Máximo del historial: {info['highest']}\nMínimo del historial: {info['lowest']}\n\n"
        f"CLASIFICACIONES EN EL HISTORIAL:\n{counts}\n\n"
        "Solo precio OHLC; sin indicadores y sin operaciones."
    )


def main():
    iq = connect()
    telegram_send(
        f"🟢 ANALIZADOR M1 CONECTADO\n\nPar único: {PAIR}\n"
        f"Historial objetivo: {HISTORY_COUNT} velas cerradas\n"
        "Clasifica fuerza, impulso/momentum, reversión, continuidad, indecisión, "
        "doji, descanso, rechazo, ruptura, inside bar, outside bar, expansión "
        "y compresión de rango.\n\n"
        "Sin indicadores técnicos. Sin operaciones."
    )

    last_reported = 0
    last_summary_count = 0
    while True:
        try:
            try:
                if not iq.check_connect():
                    log.warning("Conexión perdida; reconectando...")
                    connected, reason = iq.connect()
                    if not connected:
                        raise ConnectionError(reason)
            except AttributeError:
                pass

            candles = get_closed_candles(iq)
            if not candles:
                time.sleep(POLL_SECONDS)
                continue

            classified = classify_candles(candles)
            if last_reported == 0:
                if SEND_INITIAL_HISTORY:
                    telegram_send(initial_history_message(candles))
                    # Envía el historial completo inicial por bloques.
                    for start in range(0, len(classified), 5):
                        batch = classified[start:start + 5]
                        text = f"📜 HISTORIAL DETALLADO | {PAIR} | velas {start + 1}-{start + len(batch)}\n\n"
                        text += "\n\n".join(format_candle(start + j + 1, c) for j, c in enumerate(batch))
                        telegram_send(text)
                last_reported = candles[-1]["timestamp"]
                last_summary_count = len(candles)
                time.sleep(POLL_SECONDS)
                continue

            new_indices = [i for i, c in enumerate(candles) if c["timestamp"] > last_reported]
            for i in new_indices:
                candle = classified[i]
                message = (
                    f"🕯 VELA M1 CERRADA | {PAIR}\n"
                    f"Hora: {datetime.fromtimestamp(candle['timestamp'], tz=timezone.utc):%Y-%m-%d %H:%M:%S UTC}\n"
                    f"Color: {candle['color']}\n"
                    f"O={candle['open']:.8f} H={candle['high']:.8f} "
                    f"L={candle['low']:.8f} C={candle['close']:.8f}\n"
                    f"Rango={candle['range']:.8f} | Cuerpo={candle['body']:.8f} "
                    f"({candle['body_pct']:.1f}%)\n"
                    f"Mecha superior={candle['upper_wick']:.8f} "
                    f"({candle['upper_wick_pct']:.1f}%)\n"
                    f"Mecha inferior={candle['lower_wick']:.8f} "
                    f"({candle['lower_wick_pct']:.1f}%)\n"
                    f"Clasificación: {', '.join(candle['labels'])}\n"
                    f"Velas cerradas disponibles: {len(candles)}"
                )
                telegram_send(message)
                last_reported = candle["timestamp"]
                log.info("%s cerrada: %s", PAIR, ", ".join(candle["labels"]))

                # Informe periódico de resumen; no afecta a la clasificación.
                if len(candles) >= last_summary_count + SUMMARY_EVERY:
                    telegram_send(initial_history_message(candles))
                    last_summary_count = len(candles)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Analizador detenido.")
            break
        except Exception:
            log.exception("Error en ciclo de análisis")
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
