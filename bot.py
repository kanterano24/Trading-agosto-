"""
bot.py — recopila 100 velas M1 cerradas de IQ Option y las envía por Telegram.
No ejecuta operaciones. Usar credenciales mediante variables de entorno.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, MIN_CANDLES, TIMEFRAME_SECONDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("m1_collector")

EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PAIR = os.getenv("ANALYSIS_PAIR", "EURUSD")
CANDLE_COUNT = max(MIN_CANDLES + 2, int(os.getenv("CANDLE_COUNT_M1", "120")))
POLL_SECONDS = max(0.5, float(os.getenv("POLL_SECONDS", "2")))
BATCH_SIZE = max(5, min(25, int(os.getenv("TELEGRAM_BATCH_SIZE", "20"))))


def telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        if not payload.get("ok"):
            log.error("Telegram rechazó el mensaje: %s", payload)
            return False
        return True
    except Exception:
        log.exception("No se pudo enviar mensaje a Telegram")
        return False


def fmt(value: Any) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def format_batch(batch: list[dict], start_index: int, total: int) -> str:
    lines = [f"HISTORIAL M1 | Velas {start_index}-{start_index+len(batch)-1} de {total}",
             f"Par: {PAIR}", "N | Hora UTC | Color | Open | High | Low | Close | Cuerpo% | Mecha sup | Mecha inf"]
    for i, c in enumerate(batch, start_index):
        ts = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(
            f"{i} | {ts} | {c['color']} | {fmt(c['open'])} | {fmt(c['high'])} | "
            f"{fmt(c['low'])} | {fmt(c['close'])} | {c['body_ratio_pct']:.1f}% | "
            f"{fmt(c['upper_wick'])} | {fmt(c['lower_wick'])}"
        )
    return "\n".join(lines)


def get_closed_candles(iq: IQ_Option) -> list[dict]:
    now = int(iq.get_server_timestamp())
    raw = iq.get_candles(PAIR, TIMEFRAME_SECONDS, CANDLE_COUNT, now)
    df = normalize_candles(raw)
    # Excluir la vela que todavía está abierta según el reloj del servidor.
    current_open = (now // TIMEFRAME_SECONDS) * TIMEFRAME_SECONDS
    df = df[df["timestamp"] < current_open]
    return df.tail(MIN_CANDLES).to_dict("records")


def main() -> None:
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD en Railway.")
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Configura TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en Railway.")

    telegram(f"🟡 Recolector M1 iniciado\nPar: {PAIR}\nObjetivo: {MIN_CANDLES} velas cerradas\nModo: solo recopilación; operaciones desactivadas.")
    iq = IQ_Option(EMAIL, PASSWORD)
    connected, reason = iq.connect()
    if not connected:
        raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")
    log.info("Conectado. Recopilando %s", PAIR)

    sent = False
    while not sent:
        try:
            if not iq.check_connect():
                connected, reason = iq.connect()
                if not connected:
                    raise ConnectionError(str(reason))
            candles = get_closed_candles(iq)
            log.info("Velas cerradas disponibles: %d/%d", len(candles), MIN_CANDLES)
            if len(candles) >= MIN_CANDLES:
                report = describe_history(candles)
                data = report["candles"][-MIN_CANDLES:]
                total = len(data)
                ok_all = True
                for offset in range(0, total, BATCH_SIZE):
                    batch = data[offset:offset+BATCH_SIZE]
                    msg = format_batch(batch, offset+1, total)
                    if not telegram(msg):
                        ok_all = False
                        break
                    time.sleep(0.4)  # evita ráfagas al API de Telegram
                if ok_all:
                    sent = telegram(
                        f"✅ HISTORIAL ENVIADO\nPar: {PAIR}\nVelas: {total}\n"
                        "Datos: OHLC + anatomía básica.\n"
                        "Compárteme los mensajes de las velas para estudiar las condiciones."
                    )
                if not sent:
                    log.warning("Envío incompleto; se reintentará.")
            time.sleep(POLL_SECONDS)
        except Exception:
            log.exception("Error durante recopilación")
            time.sleep(5)


if __name__ == "__main__":
    main()
