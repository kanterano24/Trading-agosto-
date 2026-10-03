from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

# Importación unidireccional: strategy.py NO debe importar bot.py ni importarse a sí mismo.
from strategy import analyze_market


# Binary only: deshabilitar helpers de mercado digital.
def _binary_only_digital_underlying(self):
    return {"underlying": []}


def _disabled_digital_open(self, *args, **kwargs):
    return None


setattr(IQ_Option, "get_digital_underlying_list_data", _binary_only_digital_underlying)
for _name in ("_IQ_Option__get_digital_open", "__get_digital_open", "_get_digital_open"):
    if hasattr(IQ_Option, _name):
        setattr(IQ_Option, _name, _disabled_digital_open)


IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

M1 = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "1300"))
MAX_PAIRS = max(1, int(os.getenv("MAX_OTC_PAIRS", "3")))
CANDLE_COUNT_M1 = max(10, int(os.getenv("CANDLE_COUNT_M1", "120")))
PAIR_REFRESH_SECONDS = max(60.0, float(os.getenv("PAIR_REFRESH_SECONDS", "600")))
WORKERS = max(1, int(os.getenv("ANALYSIS_WORKERS", "5")))
LOOP_SLEEP = max(0.05, float(os.getenv("LOOP_SLEEP", "0.20")))
ENFORCE_ALTERNATION = os.getenv("ENFORCE_ALTERNATION", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

PAIRS = []
LAST_REFRESH = 0.0
IQ: Optional[IQ_Option] = None
BOT_RUNNING = os.getenv("AUTO_START", "1").strip().lower() not in ("0", "false", "no", "off")
ACCOUNT_MODE = "PRACTICE"  # Fijado: nunca usar REAL
MAX_ENTRY_DELAY = max(1.0, float(os.getenv("MAX_ENTRY_DELAY_SECONDS", "5")))
LAST_SIGNAL_CANDLE = {}
STREAM_STARTED = set()
STREAM_CACHE = {}
TRADED_CANDLE = {}
LAST_DIRECTION = {}
STATE_LOCK = threading.RLock()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

TG_SESSION = requests.Session()


def tg(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        response = TG_SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": str(msg)},
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
        return bool(payload.get("ok"))
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Telegram sendMessage: %s", exc)
        return False


def telegram_loop():
    global BOT_RUNNING
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram desactivado: faltan variables de entorno.")
        return

    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    retry_delay = 2

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset + 1

            response = TG_SESSION.get(url, params=params, timeout=(5, 35))
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError("Respuesta no válida de Telegram")

            for update in data.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = update_id

                message = update.get("message") or {}
                chat = str((message.get("chat") or {}).get("id", ""))
                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                command = str(message.get("text", "")).strip().lower()
                if command == "/start":
                    with STATE_LOCK:
                        BOT_RUNNING = True
                    tg(
                        "🟢 BOT ACTIVADO\n\n"
                        "Cuenta: PRACTICE (demo)\n"
                        "Estrategia CI M1: cruce 61.8/38.2 + color de vela\n"
                        "CALL: cruce arriba 61.8 con vela roja\n"
                        "PUT: cruce abajo 38.2 con vela verde\n"
                        "Expiración: 1 minuto."
                    )
                elif command == "/stop":
                    with STATE_LOCK:
                        BOT_RUNNING = False
                    tg("🔴 BOT DETENIDO")
                elif command == "/status":
                    with STATE_LOCK:
                        running = BOT_RUNNING
                        pair_count = len(PAIRS)
                    tg(
                        f"📊 ESTADO: {'🟢 ACTIVO' if running else '🔴 DETENIDO'}\n"
                        f"OTC: {pair_count}\n"
                        f"Importe demo: {AMOUNT:g}\n"
                        "Cuenta: PRACTICE\n"
                        "Expiración: 1 minuto\n"
                        f"Alternancia: {'ACTIVA' if ENFORCE_ALTERNATION else 'DESACTIVADA'}"
                    )

            retry_delay = 2

        except (requests.RequestException, ValueError, RuntimeError) as exc:
            logger.warning("Telegram polling: %s", exc)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
        except Exception:
            logger.exception("Error inesperado en Telegram polling")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


def is_otc(name):
    value = str(name).upper()
    return value.endswith("-OTC") or value.endswith("_OTC") or "OTC" in value


def refresh_pairs(force=False):
    global PAIRS, LAST_REFRESH
    if IQ is None:
        return []
    now = time.time()
    if not force and now - LAST_REFRESH < PAIR_REFRESH_SECONDS:
        return PAIRS

    try:
        data = IQ.get_all_init_v2()
        binary = data.get("binary", {}) if isinstance(data, dict) else {}
        actives = binary.get("actives", {}) if isinstance(binary, dict) else {}
    except Exception as exc:
        logger.warning("Catálogo OTC: %s", exc)
        return PAIRS

    found = []
    for active_id, info in actives.items():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if not isinstance(name, str):
            continue
        name = name.split(".", 1)[-1].strip()
        suspended = info.get("is_suspended", info.get("suspended", False))
        if not is_otc(name) or info.get("enabled", True) is False or suspended:
            continue
        try:
            OP_code.ACTIVES[name] = int(active_id)
            found.append(name)
        except (TypeError, ValueError):
            continue

    if found:
        selected = sorted(set(found))[:MAX_PAIRS]
        with STATE_LOCK:
            PAIRS = selected
            LAST_REFRESH = now
        logger.info("OTC seleccionados: %d/%d", len(selected), len(set(found)))
    else:
        logger.warning("No se encontraron pares Binary OTC disponibles.")
    return PAIRS


def server_ts():
    try:
        return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:
        return time.time()


def floor_ts(ts):
    return int(ts // M1) * M1


def select_practice_account():
    """Fuerza la cuenta demo y falla de forma segura si no puede seleccionarla."""
    if IQ is None:
        return False
    try:
        changed = IQ.change_balance("PRACTICE")
        time.sleep(0.4)
        getter = getattr(IQ, "get_balance_mode", None)
        mode = getter() if callable(getter) else None
        if mode is not None and str(mode).strip().upper() != "PRACTICE":
            logger.error("Modo de cuenta inesperado: %r", mode)
            return False
        if changed is False:
            return False
        logger.info("Cuenta seleccionada: PRACTICE (demo)")
        return True
    except Exception as exc:
        logger.error("No se pudo seleccionar PRACTICE: %s", exc)
        return False


def connect():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise ConnectionError(reason)
    if not select_practice_account():
        raise RuntimeError("No se confirmó PRACTICE; se bloquea el inicio por seguridad.")
    refresh_pairs(True)
    tg(f"🟢 IQ OPTION CONECTADO\nCuenta: PRACTICE (demo)\nCI M1 | OTC: {len(PAIRS)}\nExpiración: 1 minuto.\nBot: {'ACTIVO' if BOT_RUNNING else 'PAUSADO'}")


def ensure_connection():
    if IQ is None:
        return False
    try:
        if IQ.check_connect():
            return True
    except Exception:
        pass

    try:
        result = IQ.connect()
        ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
        if ok:
            if not select_practice_account():
                logger.error("Reconexión sin PRACTICE confirmado; operaciones bloqueadas.")
                return False
            with STATE_LOCK:
                STREAM_STARTED.clear()
                STREAM_CACHE.clear()
            logger.info("Conexión IQ Option restablecida en PRACTICE.")
            return True
    except Exception as exc:
        logger.warning("Reconexión IQ Option: %s", exc)
    return False


def ensure_stream(pair):
    if IQ is None:
        return False
    if pair in STREAM_STARTED:
        return True
    try:
        IQ.start_candles_stream(pair, M1, CANDLE_COUNT_M1)
        STREAM_STARTED.add(pair)
        return True
    except Exception as exc:
        logger.warning("Stream %s: %s", pair, exc)
        return False


def read_stream(pair):
    if IQ is None or not ensure_stream(pair):
        return None
    try:
        raw = IQ.get_realtime_candles(pair, M1)
        if not raw:
            return STREAM_CACHE.get(pair)

        rows = []
        for candle in raw.values():
            if isinstance(candle, dict):
                rows.append({
                    "from": candle.get("from"),
                    "open": candle.get("open"),
                    "high": candle.get("max", candle.get("high")),
                    "low": candle.get("min", candle.get("low")),
                    "close": candle.get("close"),
                })

        data = pd.DataFrame(rows)
        required = ["from", "open", "high", "low", "close"]
        if data.empty or any(column not in data.columns for column in required):
            return STREAM_CACHE.get(pair)

        for column in required:
            data[column] = pd.to_numeric(data[column], errors="coerce")
        data = (
            data.dropna(subset=required)
            .drop_duplicates("from")
            .sort_values("from")
            .reset_index(drop=True)
        )
        if not data.empty:
            STREAM_CACHE[pair] = data
            return data
        return STREAM_CACHE.get(pair)
    except Exception as exc:
        logger.debug("Lectura stream %s: %s", pair, exc)
        return STREAM_CACHE.get(pair)


def analyze_live_pair(pair):
    data = read_stream(pair)
    if data is None or data.empty:
        logger.info("%s | esperando datos del stream", pair)
        return None

    now = server_ts()
    current_open_ts = floor_ts(now)
    # La estrategia recibe exclusivamente velas que ya completaron sus 60 segundos.
    closed = data[data["from"] + M1 <= now].copy().reset_index(drop=True)
    if len(closed) < 30:
        logger.info("%s | velas cerradas insuficientes: %d/30", pair, len(closed))
        return None

    result = analyze_market(closed, pair=pair, mode="M1_M1")
    signal = result.get("signal")
    details = result.get("analysis", {})
    source_ts = result.get("candle_timestamp") or details.get("signal_candle_from")
    if signal not in ("call", "put"):
        last_closed_ts = int(closed.iloc[-1]["from"])
        with STATE_LOCK:
            first_log = LAST_SIGNAL_CANDLE.get((pair, "no_signal")) != last_closed_ts
            if first_log:
                LAST_SIGNAL_CANDLE[(pair, "no_signal")] = last_closed_ts
        if first_log:
            logger.info("%s | sin señal | %s", pair, result.get("reason", "sin señal"))
        return None

    source_ts = int(source_ts if source_ts is not None else closed.iloc[-1]["from"])
    # Una señal de la vela N se ejecuta solo al abrir N+1, nunca más tarde.
    if source_ts + M1 != current_open_ts:
        return None
    delay = now - current_open_ts
    if delay > MAX_ENTRY_DELAY:
        logger.info("%s | señal %s omitida por tardía: %.2fs", pair, signal.upper(), delay)
        return None

    with STATE_LOCK:
        if LAST_SIGNAL_CANDLE.get(pair) == source_ts:
            return None
        if ENFORCE_ALTERNATION and LAST_DIRECTION.get(pair) == signal:
            logger.info("%s | señal bloqueada por alternancia: %s", pair, signal.upper())
            return None
        LAST_SIGNAL_CANDLE[pair] = source_ts

    return {"pair": pair, "signal": signal, "reason": result.get("reason", "Cruce CI confirmado"),
            "candle_ts": current_open_ts, "source_ts": source_ts, "analysis": details}


def buy(candidate):
    try:
        if IQ is None or not IQ.check_connect() or not select_practice_account():
            logger.error("Orden bloqueada: no se confirmó conexión/cuenta PRACTICE.")
            return False, None
        result = IQ.buy(
            AMOUNT,
            candidate["pair"],
            candidate["signal"],
            EXPIRATION,
        )
        if isinstance(result, tuple):
            return bool(result[0]), result[1] if len(result) > 1 else result[0]
        return result not in (False, None, -1), result
    except Exception as exc:
        logger.error("buy %s: %s", candidate["pair"], exc)
        return False, None


def execute(candidate):
    pair = candidate["pair"]
    signal = candidate["signal"]
    candle_ts = int(candidate["candle_ts"])

    with STATE_LOCK:
        if TRADED_CANDLE.get(pair) == candle_ts:
            return False
        now = server_ts()
        if floor_ts(now) != candle_ts or now - candle_ts > MAX_ENTRY_DELAY:
            return False
        if ENFORCE_ALTERNATION and LAST_DIRECTION.get(pair) == signal:
            return False
        # Reservar la vela evita órdenes duplicadas si el ciclo vuelve a entrar.
        TRADED_CANDLE[pair] = candle_ts

    ok, order_id = buy(candidate)
    if not ok:
        with STATE_LOCK:
            if TRADED_CANDLE.get(pair) == candle_ts:
                TRADED_CANDLE.pop(pair, None)
        return False

    with STATE_LOCK:
        LAST_DIRECTION[pair] = signal

    inside = max(0.0, server_ts() - candle_ts)
    logger.info(
        "ENTRADA | %s | %s | segundo=%.2f | ID=%s | %s",
        pair, signal.upper(), inside, order_id, candidate["reason"]
    )
    tg(
        f"⚡ ENTRADA DEMO EJECUTADA\nCuenta: PRACTICE\nPar: {pair}\n"
        f"Dirección: {signal.upper()}\nM1\n"
        f"Entrada dentro de vela: {inside:.2f}s\n"
        f"Expiración: 1 minuto\nRazón: {candidate['reason']}\nID: {order_id}"
    )
    return True


def process():
    refresh_pairs()
    pairs_snapshot = list(PAIRS)
    if not pairs_snapshot:
        return

    # Calentar/actualizar los streams antes de analizar.
    for pair in pairs_snapshot:
        read_stream(pair)

    def worker(pair):
        try:
            return analyze_live_pair(pair)
        except Exception as exc:
            logger.debug("Análisis %s: %s", pair, exc)
            return None

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(pairs_snapshot))) as executor:
        results = list(executor.map(worker, pairs_snapshot))

    candidates = [item for item in results if item]
    order = {pair: index for index, pair in enumerate(pairs_snapshot)}
    candidates.sort(key=lambda item: order.get(item["pair"], 9999))

    for candidate in candidates:
        if execute(candidate):
            break


def main():
    global BOT_RUNNING
    required = (IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    if not all(required):
        logger.error(
            "Faltan variables: IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN o TELEGRAM_CHAT_ID"
        )
        return
    if AMOUNT <= 0:
        logger.error("AMOUNT debe ser mayor que cero.")
        return

    threading.Thread(
        target=telegram_loop,
        name="telegram-polling",
        daemon=True,
    ).start()

    try:
        connect()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option")
        tg(f"❌ ERROR DE CONEXIÓN\n{exc}")
        return

    while True:
        try:
            with STATE_LOCK:
                running = BOT_RUNNING
            if not running:
                time.sleep(0.25)
                continue
            if not ensure_connection():
                time.sleep(2)
                continue
            process()
            time.sleep(LOOP_SLEEP)
        except KeyboardInterrupt:
            with STATE_LOCK:
                BOT_RUNNING = False
            break
        except Exception as exc:
            logger.exception("Error principal: %s", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
