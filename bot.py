import os
import time
import logging
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option

# ============================================================
# CONFIGURACION
# Requiere: IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Opcionales: CANDLE_COUNT_M1=200, POLL_SECONDS=60,
#             TELEGRAM_BATCH_SIZE=20, ACCOUNT_TYPE=PRACTICE
# ============================================================

TIMEFRAME = 60
CANDLE_COUNT = max(200, int(os.getenv("CANDLE_COUNT_M1", "200")))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "60"))
BATCH_SIZE = max(1, int(os.getenv("TELEGRAM_BATCH_SIZE", "20")))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "PRACTICE").upper()
PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC").upper()

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def telegram_send(text):
    """Envía un mensaje a Telegram. Divide mensajes largos por lotes."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram no está configurado.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            logging.error("Telegram respondió con error: %s", result)
            return False
        return True
    except requests.RequestException as exc:
        logging.error("Error enviando a Telegram: %s", exc)
        return False


def connect_iq():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD.")

    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = iq.connect()
    if not ok:
        raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")

    # Este programa solo recopila/analiza velas; no abre operaciones.
    if ACCOUNT_TYPE in ("PRACTICE", "REAL"):
        try:
            iq.change_balance(ACCOUNT_TYPE)
        except Exception as exc:
            logging.warning("No se pudo cambiar el tipo de cuenta: %s", exc)

    logging.info("Conectado a IQ Option.")
    return iq


def discover_active_otc_pairs(iq):
    """
    Devuelve pares OTC que aparecen como abiertos en el mercado binary.
    El resultado depende de lo que la API exponga en ese momento.
    """
    try:
        open_times = iq.get_all_open_time()
        binary = open_times.get("binary", {})
        candidates = []

        for pair, info in binary.items():
            if not pair.upper().endswith("-OTC"):
                continue
            if isinstance(info, dict) and info.get("open") is True:
                candidates.append(pair)

        return sorted(set(candidates))
    except Exception as exc:
        logging.error("No se pudieron consultar los pares OTC activos: %s", exc)
        return []


def get_server_time(iq):
    try:
        return int(iq.get_server_timestamp())
    except Exception:
        return int(time.time())


def get_closed_candles(iq, pair, count=CANDLE_COUNT):
    """
    Solicita velas M1 y excluye la vela que aún está en formación.
    Devuelve las últimas `count` velas cerradas, en orden cronológico.
    """
    now = get_server_time(iq)
    raw = iq.get_candles(pair, TIMEFRAME, count + 5, now)
    if not raw:
        return []

    # Las velas de IQ Option suelen incluir 'from' como inicio Unix.
    closed = []
    current_minute = now - (now % TIMEFRAME)

    for candle in raw:
        start = int(candle.get("from", candle.get("at", 0)))
        if start <= 0 or start + TIMEFRAME > current_minute:
            continue

        try:
            o = float(candle["open"])
            h = float(candle["max"])
            l = float(candle["min"])
            c = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            continue

        if h < max(o, c, l) or l > min(o, c, h):
            continue

        closed.append({
            "timestamp": start,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
        })

    # Eliminar duplicados por timestamp y ordenar.
    by_time = {c["timestamp"]: c for c in closed}
    return [by_time[t] for t in sorted(by_time)][-count:]


def candle_metrics(c):
    o, h, l, close = c["open"], c["high"], c["low"], c["close"]
    total = max(h - l, 0.0)
    body = abs(close - o)
    upper = max(0.0, h - max(o, close))
    lower = max(0.0, min(o, close) - l)

    if close > o:
        color = "VERDE"
    elif close < o:
        color = "ROJA"
    else:
        color = "DOJI"

    body_pct = (body / total * 100.0) if total else 0.0
    close_position = ((close - l) / total * 100.0) if total else 50.0

    return {
        "color": color,
        "body_pct": body_pct,
        "upper": upper,
        "lower": lower,
        "range": total,
        "close_position": close_position,
    }


def format_candle(index, c):
    m = candle_metrics(c)
    dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
    return (
        f'{index:03d} | {dt:%Y-%m-%d %H:%M:%S} UTC | {m["color"]} | '
        f'O {c["open"]:.6f} H {c["high"]:.6f} '
        f'L {c["low"]:.6f} C {c["close"]:.6f} | '
        f'Cuerpo {m["body_pct"]:.1f}% | '
        f'MS {m["upper"]:.6f} MI {m["lower"]:.6f}'
    )


def analyze_history(candles):
    """Resumen descriptivo básico; no genera señales ni ejecuta trades."""
    if not candles:
        return "No hay velas para analizar."

    first = candles[0]["open"]
    last = candles[-1]["close"]
    net = last - first
    greens = sum(1 for c in candles if c["close"] > c["open"])
    reds = sum(1 for c in candles if c["close"] < c["open"])
    dojis = len(candles) - greens - reds
    highest = max(c["high"] for c in candles)
    lowest = min(c["low"] for c in candles)

    if net > 0:
        direction = "alcista en el balance del período"
    elif net < 0:
        direction = "bajista en el balance del período"
    else:
        direction = "sin cambio neto"

    return (
        "RESUMEN DESCRIPTIVO M1\n"
        f"Velas recibidas: {len(candles)}\n"
        f"Balance: {direction} ({net:+.6f})\n"
        f"Máximo del tramo: {highest:.6f}\n"
        f"Mínimo del tramo: {lowest:.6f}\n"
        f"Verdes: {greens} | Rojas: {reds} | Doji: {dojis}\n"
        "Nota: este resumen describe el historial; no es una señal de entrada."
    )


def send_history(pair, candles):
    if len(candles) < CANDLE_COUNT:
        telegram_send(
            f"⚠️ {pair}: solo se recibieron {len(candles)} velas cerradas "
            f"de las {CANDLE_COUNT} solicitadas. Se enviarán las disponibles."
        )

    header = (
        f"HISTORIAL M1 | {pair}\n"
        f"Velas cerradas: {len(candles)}\n"
        "N | Hora UTC | Color | OHLC | Cuerpo | Mechas\n"
    )
    lines = [
        format_candle(i, c)
        for i, c in enumerate(candles, start=1)
    ]

    telegram_send(header + "\n" + analyze_history(candles))

    for start in range(0, len(lines), BATCH_SIZE):
        part = lines[start:start + BATCH_SIZE]
        msg = (
            f"{pair} | M1 | Velas {start + 1}-{start + len(part)} "
            f"de {len(lines)}\n" + "\n".join(part)
        )
        telegram_send(msg)
        time.sleep(0.4)


def main():
    iq = connect_iq()
    telegram_send(
        f"🤖 Recopilador M1 iniciado. Par fijo: {PAIR}. "
        f"Recopilará hasta {CANDLE_COUNT} velas cerradas y enviará el historial. "
        "No realiza operaciones."
    )

    while True:
        pairs = discover_active_otc_pairs(iq)
        if PAIR not in pairs:
            logging.warning("%s no aparece abierto en binary OTC en este momento.", PAIR)
            telegram_send(
                f"El par solicitado {PAIR} no aparece abierto ahora. "
                "No se sustituirá por EURUSD ni por otro par."
            )
            time.sleep(POLL_SECONDS)
            continue

        pair = PAIR
        logging.info("Par OTC solicitado y activo: %s", pair)

        try:
            candles = get_closed_candles(iq, pair, CANDLE_COUNT)
            if not candles:
                logging.warning("Sin velas cerradas para %s.", pair)
                time.sleep(POLL_SECONDS)
                continue

            send_history(pair, candles)
            logging.info("Historial enviado: %s (%d velas)", pair, len(candles))
            break  # Envío único para que puedas revisar los datos.
        except Exception as exc:
            logging.exception("Error procesando %s: %s", pair, exc)
            telegram_send(f"Error procesando {pair}: {exc}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
