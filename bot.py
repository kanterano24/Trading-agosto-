"""bot.py — Bot IQ Option: análisis M1 y ejecución binaria en cuenta DEMO.
Solo usa velas cerradas; sin indicadores técnicos, S/R ni rechazo como filtro.
Variables requeridas: IQ_EMAIL, IQ_PASSWORD. Telegram es opcional.
"""

import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone

import requests
from iqoptionapi.stable_api import IQ_Option

# Asegura que Python busque strategy.py junto a este archivo, incluso si
# Railway cambia el directorio de trabajo al iniciar el proceso.
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

try:
    from strategy import normalize_candles, classify_candles, analyze_market, describe_history
except ModuleNotFoundError as exc:
    if exc.name != "strategy":
        raise
    # Respaldo incorporado: el bot no se cae si Railway recibe bot.py pero olvidan
    # subir strategy.py. Si existe strategy.py, siempre se usa ese módulo.
    log_fallback_message = (
        "No se encontró strategy.py junto a bot.py; se activa la clasificación de respaldo integrada."
    )
    print(log_fallback_message, flush=True)
    from statistics import median
    TIMEFRAME_SECONDS = 60
    ACTIONABLE_LABELS = {
        "FUERZA", "IMPULSO", "CONTINUIDAD", "RUPTURA ALCISTA", "RUPTURA BAJISTA",
        "REVERSIÓN (ENVOLVENTE)", "POSIBLE REVERSIÓN",
    }


    def normalize_candles(raw):
        """Normaliza velas de IQ Option, elimina inválidas/duplicadas y ordena por tiempo."""
        out = {}
        for item in raw or []:
            try:
                ts = int(float(item.get("from", item.get("timestamp", item.get("at", 0)))))
                o = float(item["open"])
                h = float(item.get("max", item.get("high")))
                lo = float(item.get("min", item.get("low")))
                c = float(item["close"])
                if ts <= 0 or not all(map(lambda x: x == x and abs(x) != float("inf"), (o, h, lo, c))):
                    continue
                if h < max(o, lo, c) or lo > min(o, h, c):
                    continue
                out[ts] = {"timestamp": ts, "open": o, "high": h, "low": lo, "close": c}
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                continue
        return [out[k] for k in sorted(out)]


    def candle_anatomy(candle):
        o, h, lo, c = (float(candle[k]) for k in ("open", "high", "low", "close"))
        span = max(0.0, h - lo)
        body = abs(c - o)
        upper = max(0.0, h - max(o, c))
        lower = max(0.0, min(o, c) - lo)
        color = "VERDE" if c > o else "ROJA" if c < o else "DOJI"
        return {
            **candle, "color": color, "range": span, "body": body,
            "body_pct": body / span * 100 if span else 0.0,
            "upper_wick": upper, "lower_wick": lower,
            "upper_wick_pct": upper / span * 100 if span else 0.0,
            "lower_wick_pct": lower / span * 100 if span else 0.0,
            "close_position_pct": (c - lo) / span * 100 if span else 50.0,
        }


    def classify_candles(raw):
        """Clasifica cada vela; las categorías descriptivas pueden coexistir."""
        candles = normalize_candles(raw)
        result = []
        ranges = [c["high"] - c["low"] for c in candles]
        for i, raw_c in enumerate(candles):
            x = candle_anatomy(raw_c)
            prev = candle_anatomy(candles[i - 1]) if i else None
            labels = []
            body_pct, close_pos, rng = x["body_pct"], x["close_position_pct"], x["range"]
            avg_range = median(ranges[max(0, i - 10):i]) if i else 0.0

            if rng == 0 or body_pct <= 10:
                labels.append("DOJI")
            elif body_pct <= 25:
                labels.append("INDECISIÓN")
            elif body_pct <= 35 and max(x["upper_wick_pct"], x["lower_wick_pct"]) >= 45:
                labels.append("DESCANSO")

            if body_pct >= 60 and (close_pos >= 80 or close_pos <= 20):
                labels.append("FUERZA")
            if body_pct >= 55 and (
                (close_pos >= 70 and x["color"] == "VERDE") or
                (close_pos <= 30 and x["color"] == "ROJA")
            ):
                labels.append("IMPULSO")

            if prev:
                prev_body_top, prev_body_bottom = max(prev["open"], prev["close"]), min(prev["open"], prev["close"])
                cur_body_top, cur_body_bottom = max(x["open"], x["close"]), min(x["open"], x["close"])
                if x["high"] <= prev["high"] and x["low"] >= prev["low"]:
                    labels.append("INSIDE BAR")
                if x["high"] > prev["high"] and x["low"] < prev["low"]:
                    labels.append("OUTSIDE BAR")
                if x["color"] == prev["color"] and x["color"] != "DOJI" and body_pct >= 30:
                    labels.append("CONTINUIDAD")

                bullish_engulf = (
                    prev["color"] == "ROJA" and x["color"] == "VERDE"
                    and cur_body_top >= prev_body_top and cur_body_bottom <= prev_body_bottom
                )
                bearish_engulf = (
                    prev["color"] == "VERDE" and x["color"] == "ROJA"
                    and cur_body_top >= prev_body_top and cur_body_bottom <= prev_body_bottom
                )
                if bullish_engulf or bearish_engulf:
                    labels.append("REVERSIÓN (ENVOLVENTE)")
                elif prev["color"] != x["color"] and x["color"] != "DOJI" and body_pct >= 40:
                    labels.append("POSIBLE REVERSIÓN")

                if x["close"] > prev["high"]:
                    labels.append("RUPTURA ALCISTA")
                elif x["close"] < prev["low"]:
                    labels.append("RUPTURA BAJISTA")

                # El rechazo se informa como descripción, nunca como filtro de entrada.
                if x["upper_wick"] >= max(x["body"] * 1.5, rng * 0.35) and close_pos < 65:
                    labels.append("RECHAZO SUPERIOR")
                if x["lower_wick"] >= max(x["body"] * 1.5, rng * 0.35) and close_pos > 35:
                    labels.append("RECHAZO INFERIOR")

                if avg_range > 0 and rng >= avg_range * 1.5 and body_pct >= 45:
                    labels.append("EXPANSIÓN DE RANGO")
                if avg_range > 0 and rng <= avg_range * 0.55:
                    labels.append("RANGO COMPRIMIDO")

            if not labels:
                labels.append("VELA NORMAL")
            x["labels"] = list(dict.fromkeys(labels))
            x["index"] = i + 1
            result.append(x)
        return result


    def analyze_market(raw):
        """Evalúa las últimas velas cerradas y devuelve CALL, PUT o NO SIGNAL.

        Para abrir una operación la última vela debe ser direccional y tener al menos
        una etiqueta accionable. La dirección se determina por el color/cierre de la
        vela; no se usa rechazo como filtro independiente.
        """
        items = classify_candles(raw)
        if not items:
            return {"signal": "NO SIGNAL", "reason": "No hay velas cerradas válidas", "candles": []}

        latest = items[-1]
        labels = set(latest["labels"])
        actionable = labels.intersection(ACTIONABLE_LABELS)
        if not actionable:
            return {
                "signal": "NO SIGNAL",
                "reason": "La última vela no tiene una clasificación direccional de entrada.",
                "latest": latest, "candles": items,
            }
        if latest["color"] == "VERDE":
            signal = "call"
        elif latest["color"] == "ROJA":
            signal = "put"
        else:
            signal = "NO SIGNAL"

        if signal == "NO SIGNAL":
            reason = "La última vela es doji/indecisa; no se opera."
        else:
            reason = "Clasificación direccional: " + ", ".join(sorted(actionable))
        return {
            "signal": signal, "reason": reason, "latest": latest,
            "sequence": [x["color"] for x in items], "candles": items,
        }


    def describe_history(raw):
        items = classify_candles(raw)
        greens = sum(x["color"] == "VERDE" for x in items)
        reds = sum(x["color"] == "ROJA" for x in items)
        dojis = sum(x["color"] == "DOJI" for x in items)
        label_counts = {}
        for candle in items:
            for label in candle["labels"]:
                label_counts[label] = label_counts.get(label, 0) + 1
        return {
            "candles": items, "count": len(items), "greens": greens, "reds": reds,
            "dojis": dojis,
            "sequence": " ".join("V" if x["color"] == "VERDE" else "R" if x["color"] == "ROJA" else "D" for x in items),
            "highest": max((x["high"] for x in items), default=None),
            "lowest": min((x["low"] for x in items), default=None),
            "net_change": items[-1]["close"] - items[0]["open"] if items else 0.0,
            "label_counts": label_counts,
        }


    def format_candle(index, candle):
        x = candle if "labels" in candle and "body_pct" in candle else candle_anatomy(candle)
        dt = datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc)
        labels = ", ".join(x.get("labels", ["SIN CLASIFICAR"]))
        return (
            f'{index:04d} | {dt:%Y-%m-%d %H:%M:%S} UTC | {x["color"]}\n'
            f'O={x["open"]:.8f} H={x["high"]:.8f} L={x["low"]:.8f} C={x["close"]:.8f}\n'
            f'Rango={x["range"]:.8f} | Cuerpo={x["body"]:.8f} ({x["body_pct"]:.1f}%)\n'
            f'Mecha sup={x["upper_wick"]:.8f} ({x["upper_wick_pct"]:.1f}%) | '
            f'Mecha inf={x["lower_wick"]:.8f} ({x["lower_wick_pct"]:.1f}%)\n'
            f'Tipos: {labels}'
        )

PAIR = os.getenv("ANALYSIS_PAIR", "USDZAR-OTC").strip().upper()
TIMEFRAME = 60
HISTORY_COUNT = max(30, int(os.getenv("CANDLE_COUNT_M1", "300")))
POLL_SECONDS = max(0.5, float(os.getenv("POLL_SECONDS", "1")))
RETRY_SECONDS = max(5, int(os.getenv("RETRY_SECONDS", "15")))
AMOUNT = float(os.getenv("AMOUNT", "5"))
EXPIRATION_MINUTES = 1
ACCOUNT_MODE = "PRACTICE"
IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SEND_INITIAL_HISTORY = os.getenv("SEND_INITIAL_HISTORY", "true").lower() in {"1", "true", "yes", "on"}
SUMMARY_EVERY = max(1, int(os.getenv("SUMMARY_EVERY", "10")))
TRADE_COOLDOWN_SECONDS = max(0, int(os.getenv("TRADE_COOLDOWN_SECONDS", "0")))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("iqoption_m1_bot")


def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.info("Telegram no configurado; mensaje omitido.")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
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
            url, data={"chat_id": TG_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram rechazó el mensaje: {result}")
        time.sleep(1.1)
    return True


def _disable_digital_open_worker(self, *args, **kwargs):
    """Desactiva el hilo digital que algunas versiones de iqoptionapi inician solas.

    Este bot opera únicamente binarias. La función original puede fallar cuando
    el servidor devuelve None para la lista de activos digitales.
    """
    log.info("Sondeo de activos digitales desactivado; se usarán solo binarias.")


# Aplicar antes de construir IQ_Option: algunas versiones arrancan el hilo
# _get_digital_open durante __init__, por lo que parchear después es demasiado tarde.
if hasattr(IQ_Option, "_get_digital_open"):
    IQ_Option._get_digital_open = _disable_digital_open_worker
# En algunas versiones el método usa doble guion bajo y Python lo transforma
# en _IQ_Option__get_digital_open.
if hasattr(IQ_Option, "_IQ_Option__get_digital_open"):
    IQ_Option._IQ_Option__get_digital_open = _disable_digital_open_worker


def connect():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD en las variables de Railway.")
    iq = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = iq.connect()
    if not connected:
        raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")
    # Forzar cuenta de práctica, nunca real.
    if hasattr(iq, "change_balance"):
        iq.change_balance(ACCOUNT_MODE)
    elif hasattr(iq, "set_balance_mode"):
        iq.set_balance_mode(ACCOUNT_MODE)
    else:
        raise RuntimeError("La versión de iqoptionapi no expone un método para seleccionar la cuenta demo.")
    log.info("Conectado a IQ Option en DEMO. Par=%s, importe=%s, expiración=%s min", PAIR, AMOUNT, EXPIRATION_MINUTES)
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
            if start > 0 and start + TIMEFRAME <= current_minute:
                closed.append(candle)
        except (TypeError, ValueError, AttributeError):
            continue
    return normalize_candles(closed)[-HISTORY_COUNT:]


def initial_history_message(candles):
    info = describe_history(candles)
    labels = sorted(info["label_counts"].items(), key=lambda x: (-x[1], x[0]))
    counts = "\n".join(f"• {name}: {count}" for name, count in labels[:18]) or "• Sin clasificaciones"
    return (
        f"📚 HISTORIAL M1 INICIAL | {PAIR}\nVelas cerradas: {info['count']}\n"
        f"Verdes: {info['greens']} | Rojas: {info['reds']} | Doji: {info['dojis']}\n"
        f"Variación neta: {info['net_change']:.8f}\nMáximo: {info['highest']}\nMínimo: {info['lowest']}\n\n"
        f"CLASIFICACIONES:\n{counts}\n\nCuenta DEMO | Importe {AMOUNT} | Expiración 1 minuto."
    )


def is_binary_open(iq):
    """Si la API permite consultar horarios, evita enviar orden a un activo cerrado."""
    try:
        schedule = iq.get_all_open_time()
        binary = schedule.get("binary", {})
        if PAIR in binary:
            return bool(binary[PAIR].get("open", False))
        # Algunas versiones de la API no publican OTC en el diccionario.
        return True
    except Exception:
        return True


def place_trade(iq, direction, candle_ts, reason):
    """Envía una orden binaria y valida la respuesta de la API."""
    if not is_binary_open(iq):
        msg = f"⏸ OPERACIÓN OMITIDA | {PAIR}\nEl activo binario figura cerrado."
        telegram_send(msg)
        log.warning(msg)
        return False, None

    action = "call" if direction == "call" else "put"
    try:
        success, order_id = iq.buy(AMOUNT, PAIR, action, EXPIRATION_MINUTES)
    except Exception as exc:
        log.exception("Error al enviar orden")
        telegram_send(f"❌ ERROR DE ORDEN | {PAIR}\nDirección: {action.upper()}\nDetalle: {exc}")
        return False, None

    if not success:
        telegram_send(
            f"❌ ORDEN RECHAZADA | {PAIR}\nDirección: {action.upper()}\n"
            f"Importe: {AMOUNT}\nExpiración: {EXPIRATION_MINUTES} minuto\n"
            f"Respuesta API: {order_id}"
        )
        log.error("Orden rechazada por IQ Option: %s", order_id)
        return False, None

    opened_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        f"🟢 OPERACIÓN DEMO ACEPTADA | {PAIR}\n"
        f"Dirección: {action.upper()}\nImporte: {AMOUNT}\n"
        f"Expiración: {EXPIRATION_MINUTES} minuto\nID: {order_id}\n"
        f"Vela señal (UTC): {datetime.fromtimestamp(candle_ts, tz=timezone.utc):%Y-%m-%d %H:%M:%S}\n"
        f"Enviada: {opened_at}\nClasificación: {reason}"
    )
    telegram_send(msg)
    log.info("Orden aceptada: %s %s id=%s", PAIR, action.upper(), order_id)
    return True, order_id


def check_result(iq, order_id):
    """Consulta el resultado si esta versión de iqoptionapi expone check_win_v4."""
    if order_id is None or not hasattr(iq, "check_win_v4"):
        return
    try:
        result = iq.check_win_v4(order_id)
        if result is None:
            return
        value = float(result)
        if value > 0:
            status = f"GANADA (+{value:.2f})"
        elif value < 0:
            status = f"PERDIDA ({value:.2f})"
        else:
            status = "EMPATE (0.00)"
        telegram_send(f"📊 RESULTADO DEMO | {PAIR}\nID: {order_id}\n{status}")
    except Exception:
        log.exception("No se pudo consultar el resultado de la operación %s", order_id)


def main():
    iq = connect()
    telegram_send(
        f"🟢 BOT IQ OPTION INICIADO\n\nPar: {PAIR}\nTimeframe: M1\n"
        f"Cuenta: DEMO/PRACTICE\nImporte: {AMOUNT}\nExpiración: 1 minuto\n"
        "Señal: clasificación de velas cerradas (fuerza, impulso, continuidad, "
        "reversión o ruptura direccional).\nSin indicadores técnicos, sin S/R y sin rechazo como filtro."
    )

    last_seen_candle = None
    last_traded_candle = None
    last_trade_wallclock = 0.0
    last_summary_count = 0
    last_reported_count = 0

    while True:
        try:
            try:
                if not iq.check_connect():
                    log.warning("Conexión perdida; reconectando...")
                    connected, reason = iq.connect()
                    if not connected:
                        raise ConnectionError(reason)
                    if hasattr(iq, "change_balance"):
                        iq.change_balance(ACCOUNT_MODE)
            except AttributeError:
                pass

            candles = get_closed_candles(iq)
            if not candles:
                time.sleep(POLL_SECONDS)
                continue

            classified = classify_candles(candles)
            if last_seen_candle is None:
                if SEND_INITIAL_HISTORY:
                    telegram_send(initial_history_message(candles))
                    # Reporte detallado limitado a las últimas 20 velas para no saturar Telegram.
                    for start in range(max(0, len(classified) - 20), len(classified), 5):
                        batch = classified[start:start + 5]
                        lines = []
                        for j, c in enumerate(batch, start + 1):
                            dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
                            lines.append(
                                f'{j:04d} | {dt:%H:%M:%S} UTC | {c["color"]} | '
                                f'O={c["open"]:.6f} H={c["high"]:.6f} L={c["low"]:.6f} C={c["close"]:.6f}\n'
                                f'Rango={c["range"]:.6f} cuerpo={c["body_pct"]:.1f}% '
                                f'mechas sup/inf={c["upper_wick_pct"]:.1f}%/{c["lower_wick_pct"]:.1f}%\n'
                                f'Tipos: {", ".join(c["labels"])}'
                            )
                        telegram_send(f"📜 HISTORIAL M1 | {PAIR}\n\n" + "\n\n".join(lines))
                last_seen_candle = candles[-1]["timestamp"]
                last_reported_count = len(candles)
                last_summary_count = len(candles)
                time.sleep(POLL_SECONDS)
                continue

            # Procesa únicamente velas nuevas y cerradas. Si hubo desconexión, procesa
            # solo la vela cerrada más reciente para no ejecutar señales atrasadas.
            new_indices = [i for i, c in enumerate(candles) if c["timestamp"] > last_seen_candle]
            if len(new_indices) > 1:
                new_indices = [new_indices[-1]]

            for i in new_indices:
                candle = candles[i]
                analysis = analyze_market(candles[:i + 1])
                labels = ", ".join(analysis.get("latest", {}).get("labels", [])) or "Sin clasificación"
                dt = datetime.fromtimestamp(candle["timestamp"], tz=timezone.utc)
                telegram_send(
                    f"🕯 VELA M1 CERRADA | {PAIR}\nHora UTC: {dt:%Y-%m-%d %H:%M:%S}\n"
                    f"Color: {analysis.get('latest', {}).get('color', 'N/D')}\n"
                    f"Tipos: {labels}\nSeñal: {analysis['signal'].upper()}\n"
                    f"Motivo: {analysis['reason']}"
                )

                if analysis["signal"] in ("call", "put"):
                    signal_ts = candle["timestamp"]
                    elapsed = time.time() - last_trade_wallclock
                    if signal_ts == last_traded_candle:
                        log.info("Señal duplicada omitida para la vela %s", signal_ts)
                    elif TRADE_COOLDOWN_SECONDS and elapsed < TRADE_COOLDOWN_SECONDS:
                        log.info("Señal omitida por cooldown de %s segundos", TRADE_COOLDOWN_SECONDS)
                    else:
                        ok, order_id = place_trade(iq, analysis["signal"], signal_ts, analysis["reason"])
                        if ok:
                            last_traded_candle = signal_ts
                            last_trade_wallclock = time.time()
                            threading.Thread(target=check_result, args=(iq, order_id), daemon=True).start()

                last_seen_candle = candle["timestamp"]

                if len(candles) >= last_summary_count + SUMMARY_EVERY:
                    telegram_send(initial_history_message(candles))
                    last_summary_count = len(candles)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot detenido por usuario.")
            break
        except Exception:
            log.exception("Error en ciclo principal")
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
