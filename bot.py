"""
bot.py — GBPUSD-OTC M1: historial inicial y monitoreo de señales/entradas.
Modo seguro: solo PRACTICE. No cambia a REAL.
Requiere IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
"""
import logging
import os
import time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle, analyze_market

PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC", "USDCHF-OTC").strip().upper()
TIMEFRAME = 60
HISTORY_COUNT = 200
AMOUNT = float(os.getenv("AMOUNT", "1000"))
EXPIRATION_MINUTES = 1
POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "2")))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
SEND_HISTORY = os.getenv("SEND_HISTORY", "true").lower() in ("1", "true", "yes")
# El usuario debe habilitarlo explícitamente; aun así, la cuenta queda forzada a PRACTICE.
ENABLE_TRADES = os.getenv("ENABLE_TRADES", "false").lower() in ("1", "true", "yes")
MIN_SECONDS_IN_CANDLE = float(os.getenv("MIN_SECONDS_IN_CANDLE", "1.5"))
MAX_ENTRY_SECOND = float(os.getenv("MAX_ENTRY_SECOND", "12"))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
_session = requests.Session()
_last_tg_sent = 0.0


def telegram_send(message, retries=5):
    """Envía mensajes respetando límites y reintentando si Telegram devuelve 429."""
    global _last_tg_sent
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram no configurado; mensaje omitido.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(retries):
        wait = 1.1 - (time.monotonic() - _last_tg_sent)
        if wait > 0:
            time.sleep(wait)
        try:
            response = _session.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
                timeout=25,
            )
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("parameters", {}).get("retry_after", 3))
                except (ValueError, TypeError):
                    retry_after = 3
                logging.warning("Telegram 429; esperando %.1f s", retry_after + 0.5)
                time.sleep(retry_after + 0.5)
                continue
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram respondió: {data}")
            _last_tg_sent = time.monotonic()
            return True
        except requests.RequestException as exc:
            logging.warning("Fallo Telegram intento %d/%d: %s", attempt + 1, retries, exc)
            time.sleep(min(2 ** attempt, 15))
    logging.error("No se pudo enviar el mensaje a Telegram.")
    return False


def split_messages(lines, max_chars=3200):
    batches, current = [], ""
    for line in lines:
        candidate = current + ("\n\n" if current else "") + line
        if current and len(candidate) > max_chars:
            batches.append(current)
            current = line
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def connect_iq():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD en Railway.")
    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = iq.connect()
    if not ok:
        raise RuntimeError(f"Conexión IQ Option fallida: {reason}")
    # Bloqueo deliberado de operaciones con dinero real.
    iq.change_balance("PRACTICE")
    logging.info("Conectado a IQ Option; cuenta solicitada PRACTICE.")
    return iq


def server_time(iq):
    try:
        return int(iq.get_server_timestamp())
    except Exception:
        return int(time.time())


def get_closed_candles(iq, count=HISTORY_COUNT):
    now = server_time(iq)
    raw = iq.get_candles(PAIR, TIMEFRAME, count + 20, now) or []
    current_minute = now - now % TIMEFRAME
    closed = []
    for item in raw:
        try:
            start = int(item.get("from", item.get("at", 0)))
            if start > 0 and start + TIMEFRAME <= current_minute:
                closed.append(item)
        except (TypeError, ValueError):
            continue
    return normalize_candles(closed)[-count:]


def send_history(candles):
    report = describe_history(candles)
    header = (
        f"📊 HISTORIAL M1 | {PAIR}\n"
        f"Velas cerradas: {report['count']}/{HISTORY_COUNT}\n"
        f"Verdes: {report['greens']} | Rojas: {report['reds']} | Doji: {report['dojis']}\n"
        f"Máximo: {report['highest']} | Mínimo: {report['lowest']}\n"
        f"Cambio neto: {report['net_change']:+.6f}\n"
        "Anatomía completa por vela. Operaciones habilitadas según configuración."
    )
    telegram_send(header)
    details = [format_candle(i, candle) for i, candle in enumerate(candles, 1)]
    chunks = split_messages(details)
    for i, chunk in enumerate(chunks, 1):
        telegram_send(f"🕯️ {PAIR} | Historial {i}/{len(chunks)}\n\n{chunk}")


def _trade_count_prune(trades):
    cutoff = time.time() - 3600
    return [t for t in trades if t >= cutoff]


def place_binary(iq, direction):
    """Abre operación binaria de 1 minuto; solo si ENABLE_TRADES está habilitado."""
    if not ENABLE_TRADES:
        return False, "ENABLE_TRADES=false; solo análisis"
    # Reafirma PRACTICE justo antes de cualquier orden.
    iq.change_balance("PRACTICE")
    ok, order_id = iq.buy(AMOUNT, PAIR, direction.lower(), EXPIRATION_MINUTES)
    return bool(ok), order_id


def main():
    iq = connect_iq()
    telegram_send(
        f"🟢 Bot M1 iniciado\nPar: {PAIR}\nTemporalidad: M1\n"
        f"Cuenta: PRACTICE\nExpiración: {EXPIRATION_MINUTES} minuto\n"
        f"Entradas: {'HABILITADAS en demo' if ENABLE_TRADES else 'DESACTIVADAS (análisis)'}"
    )

    history_sent = False
    last_processed = None
    last_direction = None
    trades = []

    while True:
        try:
            candles = get_closed_candles(iq, HISTORY_COUNT)
            if len(candles) < HISTORY_COUNT:
                logging.info("Esperando velas: %d/%d", len(candles), HISTORY_COUNT)
                time.sleep(POLL_SECONDS)
                continue

            if SEND_HISTORY and not history_sent:
                send_history(candles)
                history_sent = True

            latest = candles[-1]
            candle_ts = latest["timestamp"]
            if candle_ts == last_processed:
                time.sleep(POLL_SECONDS)
                continue
            last_processed = candle_ts

            result = analyze_market(candles)
            signal = result["signal"]
            logging.info(
                "Análisis %s | señal=%s | sesgo=%s | CALL=%s PUT=%s | %s",
                PAIR, signal, result.get("bias"), result.get("call_score"),
                result.get("put_score"), result.get("reason")
            )
            telegram_send(
                f"🔎 Análisis M1 {PAIR}\nSeñal: {signal}\nSesgo: {result.get('bias')}\n"
                f"Puntaje CALL/PUT: {result.get('call_score')}/{result.get('put_score')}\n"
                f"Motivo: {result.get('reason')}"
            )

            if signal not in ("CALL", "PUT"):
                continue
            if signal == last_direction:
                logging.info("Se omite señal repetida consecutiva: %s", signal)
                continue
            trades = _trade_count_prune(trades)
            if len(trades) >= MAX_TRADES_PER_HOUR:
                logging.warning("Límite horario alcanzado.")
                continue

            # Se ejecuta solo dentro de la ventana inicial de la vela siguiente.
            now = server_time(iq)
            second = now % TIMEFRAME
            if second > MAX_ENTRY_SECOND or second < MIN_SECONDS_IN_CANDLE:
                msg = f"Señal {signal} omitida: fuera de ventana de entrada (segundo {second})."
                logging.info(msg)
                telegram_send(msg)
                continue

            ok, order_id = place_binary(iq, signal)
            if ok:
                trades.append(time.time())
                last_direction = signal
                telegram_send(
                    f"🧪 Entrada enviada en PRACTICE\nPar: {PAIR}\nDirección: {signal}\n"
                    f"Expiración: 1 minuto\nID: {order_id}"
                )
                logging.info("Orden aceptada: %s %s id=%s", PAIR, signal, order_id)
            else:
                telegram_send(f"⚠️ Orden no aceptada: {signal} | respuesta: {order_id}")
                logging.warning("Orden rechazada: %s", order_id)

        except Exception as exc:
            logging.exception("Error en ciclo principal.")
            telegram_send(f"⚠️ Error del bot: {type(exc).__name__}: {exc}")
            time.sleep(3)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
