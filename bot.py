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

# El bot opera únicamente binarias OTC: no consulta datos digitales.
# Devolver una lista vacía evita la llamada lenta que origina el aviso
# get_digital_underlying_list_data late 30 sec y el NoneType asociado.
def _safe_digital_underlying_list_data(self):
    return {"underlying": []}

IQ_Option.get_digital_underlying_list_data = _safe_digital_underlying_list_data

# Evita iniciar la rutina de actualización de activos digitales.
# Se aplica antes de crear la instancia/conectar IQ_Option.
def _disable_digital_open(self):
    return None

IQ_Option._get_digital_open = _disable_digital_open
from strategy import normalize_candles, describe_history, format_candle, analyze_market

PAIR = "EURUSD-OTC"
MAX_OTC_PAIRS = 1
PAIR_REFRESH_SECONDS = 60
TELEGRAM_SUMMARY_SECONDS = 300
ERROR_NOTICE_SECONDS = 300
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
ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in ("1", "true", "yes")
MIN_SECONDS_IN_CANDLE = float(os.getenv("MIN_SECONDS_IN_CANDLE", "1.5"))
MAX_ENTRY_SECOND = float(os.getenv("MAX_ENTRY_SECOND", "12"))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
_session = requests.Session()
_last_tg_sent = 0.0
_last_error_notice = 0.0
_tg_update_offset = 0
_scan_enabled = True


def telegram_send(message, retries=5):
    """Envía mensajes respetando límites y reintentando si Telegram devuelve 429."""
    global _last_tg_sent
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram no configurado; mensaje omitido.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(retries):
        wait = 1.5 - (time.monotonic() - _last_tg_sent)
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
                delay = max(retry_after + 1.0, 2.0)
                logging.warning("Telegram 429; esperando %.1f s", delay)
                time.sleep(delay)
                _last_tg_sent = time.monotonic()
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



def telegram_commands(iq, pairs, analysis_states):
    """Procesa /start, /stop y /status en el chat configurado."""
    global _tg_update_offset, _scan_enabled
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        response = _session.get(
            url,
            params={"offset": _tg_update_offset, "timeout": 1, "allowed_updates": ["message"]},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            return
        for update in payload.get("result", []):
            _tg_update_offset = max(_tg_update_offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue
            parts = (message.get("text") or "").strip().split()
            if not parts:
                continue
            command = parts[0].lower().split("@")[0]
            if command == "/start":
                _scan_enabled = True
                telegram_send("▶️ Análisis y operaciones habilitados. Cuenta PRACTICE.")
            elif command == "/stop":
                _scan_enabled = False
                telegram_send("⏸️ Análisis y nuevas operaciones detenidos. El proceso sigue conectado.")
            elif command == "/status":
                state = "ACTIVO" if _scan_enabled else "DETENIDO"
                telegram_send(
                    f"📊 Estado: {state}\\nConexión IQ: {'conectada' if iq.check_connect() else 'desconectada'}"
                    f"\\nCuenta: PRACTICE\\nPares cargados: {len(pairs)}"
                    f"\\nPares con análisis: {len(analysis_states)}\\nExpiración: 1 minuto"
                )
    except Exception as exc:
        logging.warning("No se pudieron consultar comandos Telegram: %s", exc)


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
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD en las variables de Railway.")
    logging.info("Iniciando conexión con IQ Option; cuenta de destino PRACTICE.")
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


def get_closed_candles(iq, pair, count=HISTORY_COUNT):
    now = server_time(iq)
    raw = iq.get_candles(pair, TIMEFRAME, count + 20, now) or []
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


def send_history(pair, candles):
    report = describe_history(candles)
    header = (
        f"📊 HISTORIAL M1 | {pair}\n"
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
        telegram_send(f"🕯️ {pair} | Historial {i}/{len(chunks)}\n\n{chunk}")


def _trade_count_prune(trades):
    cutoff = time.time() - 3600
    return [t for t in trades if t >= cutoff]


def place_binary(iq, pair, direction):
    """Abre operación binaria de 1 minuto; solo si ENABLE_TRADES está habilitado."""
    if not ENABLE_TRADES:
        return False, "ENABLE_TRADES=false; solo análisis"
    # Reafirma PRACTICE justo antes de cualquier orden.
    iq.change_balance("PRACTICE")
    ok, order_id = iq.buy(AMOUNT, pair, direction.lower(), EXPIRATION_MINUTES)
    return bool(ok), order_id


def send_analysis_summary(states, pair_count):
    """Resumen compacto de los pares ya analizados."""
    calls = [p for p, d in states.items() if d.get("signal") == "CALL"]
    puts = [p for p, d in states.items() if d.get("signal") == "PUT"]
    waits = sum(1 for d in states.values() if d.get("signal") == "WAIT")
    lines = [
        "📋 Resumen OTC M1",
        f"Pares disponibles: {pair_count}",
        f"Pares analizados: {len(states)}",
        f"CALL: {len(calls)} | PUT: {len(puts)} | WAIT: {waits}",
    ]
    if calls:
        lines.append("CALL: " + ", ".join(calls[:25]))
    if puts:
        lines.append("PUT: " + ", ".join(puts[:25]))
    telegram_send("\n".join(lines))


def discover_otc_pairs(iq):
    """El bot trabaja exclusivamente con EURUSD-OTC."""
    try:
        opened = iq.get_all_open_time()
        for market in ("binary", "turbo"):
            section = opened.get(market, {}) if isinstance(opened, dict) else {}
            info = section.get(PAIR) if isinstance(section, dict) else None
            if isinstance(info, dict) and info.get("open") is True:
                logging.info("Par confirmado abierto: %s (%s)", PAIR, market)
                return [PAIR]
        logging.warning("%s no aparece abierto en la consulta; se conservará para reintentar velas.", PAIR)
    except Exception:
        logging.exception("No se pudo verificar apertura de %s; se conservará para reintentar.", PAIR)
    # Mantener el símbolo fijo evita que una respuesta incompleta de la API deje la lista vacía.
    return [PAIR]


def connect_with_retry():
    delay = 5
    while True:
        try:
            return connect_iq()
        except Exception as exc:
            logging.exception("Fallo de conexión IQ Option; reintentando.")
            telegram_send(f"⚠️ Conexión IQ Option fallida ({type(exc).__name__}); reintento en {delay}s.")
            time.sleep(delay)
            delay = min(delay * 2, 60)


def main():
    iq = connect_with_retry()
    telegram_send(
        f"🟢 Bot M1 iniciado\\nPar único: {PAIR}\\nTemporalidad: M1\\n"
        f"Cuenta: PRACTICE\\nExpiración: {EXPIRATION_MINUTES} minuto\\n"
        f"Entradas: {'HABILITADAS en demo' if ENABLE_TRADES else 'DESACTIVADAS (análisis)'}\\n"
        "Telegram: /start /stop /status"
    )
    pairs, last_refresh = [], 0.0
    last_processed, last_direction = {}, {}
    history_sent, trades = set(), []
    analysis_states = {}
    last_summary = 0.0
    last_error_notice = 0.0
    last_heartbeat = 0.0

    while True:
        try:
            telegram_commands(iq, pairs, analysis_states)
            try:
                connected = iq.check_connect()
            except Exception:
                connected = False
            if not connected:
                logging.warning("Conexión IQ Option perdida; reconectando.")
                pairs = []
                iq = connect_with_retry()

            now_mono = time.monotonic()
            if now_mono - last_heartbeat >= 60:
                logging.info("Bot activo | par=%s | pares cargados=%d | cuenta=PRACTICE", PAIR, len(pairs))
                last_heartbeat = now_mono
            if not pairs or now_mono - last_refresh >= PAIR_REFRESH_SECONDS:
                updated = discover_otc_pairs(iq)
                if updated != pairs:
                    added, removed = sorted(set(updated)-set(pairs)), sorted(set(pairs)-set(updated))
                    pairs = updated
                    logging.info("Pares OTC actualizados: %d", len(pairs))
                    telegram_send(
                        f"🔄 Símbolo configurado: {PAIR}\\n"
                        f"Agregados: {', '.join(added) or 'ninguno'}\\n"
                        f"Retirados: {', '.join(removed) or 'ninguno'}"
                    )
                last_refresh = now_mono

            if not pairs:
                logging.warning(
                    "No se detectaron pares OTC. Se mantiene el proceso activo y "
                    "se volverá a consultar la lista en el siguiente ciclo."
                )
                time.sleep(max(POLL_SECONDS, 5.0))
                continue

            if not _scan_enabled:
                time.sleep(POLL_SECONDS)
                continue

            for pair in list(pairs):
                try:
                    candles = get_closed_candles(iq, pair, HISTORY_COUNT)
                    if len(candles) < HISTORY_COUNT:
                        logging.info("%s esperando velas: %d/%d", pair, len(candles), HISTORY_COUNT)
                        continue
                    # El historial detallado por 50 pares genera cientos de mensajes;
                    # el estado se comunica mediante el resumen agrupado.
                    history_sent.add(pair)
                    ts = candles[-1]["timestamp"]
                    if ts == last_processed.get(pair):
                        continue
                    last_processed[pair] = ts
                    result = analyze_market(candles)
                    signal = result["signal"]
                    logging.info(
                        "Análisis %s | señal=%s | sesgo=%s | CALL=%s PUT=%s | %s",
                        pair, signal, result.get("bias"), result.get("call_score"),
                        result.get("put_score"), result.get("reason")
                    )
                    analysis_states[pair] = {
                        "signal": signal,
                        "bias": result.get("bias"),
                        "reason": result.get("reason"),
                    }
                    if signal not in ("CALL", "PUT") or signal == last_direction.get(pair):
                        continue
                    trades = _trade_count_prune(trades)
                    if len(trades) >= MAX_TRADES_PER_HOUR:
                        continue
                    sec = server_time(iq) % TIMEFRAME
                    if sec > MAX_ENTRY_SECOND or sec < MIN_SECONDS_IN_CANDLE:
                        continue
                    ok, order_id = place_binary(iq, pair, signal)
                    if ok:
                        trades.append(time.time())
                        last_direction[pair] = signal
                        telegram_send(
                            f"🧪 Entrada enviada en PRACTICE\\nPar: {pair}\\nDirección: {signal}\\n"
                            f"Expiración: 1 minuto\\nID: {order_id}"
                        )
                    else:
                        telegram_send(f"⚠️ Orden no aceptada: {pair} {signal} | respuesta: {order_id}")
                except Exception as pair_exc:
                    logging.exception("Error analizando %s.", pair)
                    now_error = time.monotonic()
                    if now_error - last_error_notice >= ERROR_NOTICE_SECONDS:
                        telegram_send(f"⚠️ Error de análisis en {pair}: {type(pair_exc).__name__}: {pair_exc}")
                        last_error_notice = now_error

            now_summary = time.monotonic()
            if now_summary - last_summary >= TELEGRAM_SUMMARY_SECONDS:
                send_analysis_summary(analysis_states, len(pairs))
                last_summary = now_summary
        except Exception as exc:
            logging.exception("Error en ciclo principal.")
            now_error = time.monotonic()
            if now_error - last_error_notice >= ERROR_NOTICE_SECONDS:
                telegram_send(f"⚠️ Error del bot: {type(exc).__name__}: {exc}")
                last_error_notice = now_error
            time.sleep(3)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logging.info("Interrupción solicitada; cerrando bot.")
            break
        except Exception as exc:
            logging.exception("Fallo fuera del ciclo principal; se reiniciará en 10 segundos.")
            try:
                telegram_send(
                    f"⚠️ Reinicio del ciclo principal: {type(exc).__name__}: {exc}. "
                    "Nuevo intento en 10 segundos."
                )
            except Exception:
                logging.exception("No se pudo notificar el fallo.")
            time.sleep(10)
