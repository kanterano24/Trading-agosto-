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

from strategy import analyze_market


# ============================================================
# BINARY OTC ONLY
# ============================================================
def _binary_only_digital_underlying(self):
    return {"underlying": []}


def _disabled_digital_open(self, *args, **kwargs):
    return None


setattr(IQ_Option, "get_digital_underlying_list_data", _binary_only_digital_underlying)
for _name in ("_IQ_Option__get_digital_open", "__get_digital_open", "_get_digital_open"):
    if hasattr(IQ_Option, _name):
        setattr(IQ_Option, _name, _disabled_digital_open)


# ============================================================
# CONFIG
# ============================================================
IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

M1 = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "500"))
MAX_PAIRS = int(os.getenv("MAX_OTC_PAIRS", "30"))
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "120"))
PAIR_REFRESH_SECONDS = float(os.getenv("PAIR_REFRESH_SECONDS", "600"))
WORKERS = int(os.getenv("ANALYSIS_WORKERS", "30"))
LOOP_SLEEP = float(os.getenv("LOOP_SLEEP", "0.02"))
MAX_ENTRY_DELAY = float(os.getenv("MAX_ENTRY_DELAY", "1.5"))
STREAM_REFRESH = float(os.getenv("STREAM_REFRESH", "0.10"))

# NO hay alternancia obligatoria CALL/PUT.
# Una nueva señal válida puede ser CALL o PUT independientemente de la anterior.

PAIRS: list[str] = []
LAST_REFRESH = 0.0
IQ: Optional[IQ_Option] = None
BOT_RUNNING = True

STREAM_STARTED: set[str] = set()
STREAM_CACHE: dict[str, pd.DataFrame] = {}
TRADED_CANDLE: dict[str, int] = {}
LAST_STREAM_READ = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================
def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=3,
        )
    except Exception:
        pass


def telegram_loop():
    global BOT_RUNNING

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 1}
            if offset is not None:
                params["offset"] = offset + 1

            data = requests.get(url, params=params, timeout=3).json()

            for update in data.get("result", []):
                offset = update.get("update_id", offset)
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                command = str(message.get("text", "")).strip().lower()

                if command == "/start":
                    BOT_RUNNING = True
                    tg(
                        "🟢 BOT ACTIVADO\n\n"
                        "Estrategia: CHOPPINESS INDEX\n"
                        "CI(14)\n"
                        "CALL = cruce ARRIBA 61.8 + vela ROJA\n"
                        "PUT = cruce ABAJO 38.2 + vela VERDE\n"
                        "Señal en vela cerrada N → entrada en N+1\n"
                        "Expiración: 1 minuto\n"
                        f"OTC: hasta {MAX_PAIRS} pares\n"
                        "Sin alternancia obligatoria."
                    )

                elif command == "/stop":
                    BOT_RUNNING = False
                    tg("🔴 BOT DETENIDO")

                elif command == "/status":
                    tg(
                        "📊 ESTADO\n\n"
                        f"{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\n"
                        f"OTC: {len(PAIRS)}\n"
                        f"Importe: {AMOUNT:g}\n"
                        "Estrategia: CI(14)\n"
                        "CALL: CI ↑ 61.8 + vela roja\n"
                        "PUT: CI ↓ 38.2 + vela verde\n"
                        "Entrada: siguiente M1\n"
                        "Expiración: 1 minuto\n"
                        "Fuente: stream M1 en tiempo real"
                    )

        except Exception:
            time.sleep(1)


# ============================================================
# OTC PAIRS
# ============================================================
def is_otc(name: str) -> bool:
    n = str(name).upper()
    return n.endswith("-OTC") or n.endswith("_OTC") or "OTC" in n


def refresh_pairs(force: bool = False):
    global PAIRS, LAST_REFRESH

    if IQ is None:
        return PAIRS

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

        if not is_otc(name):
            continue
        if info.get("enabled", True) is False:
            continue
        if info.get("is_suspended", info.get("suspended", False)):
            continue

        try:
            OP_code.ACTIVES[name] = int(active_id)
            found.append(name)
        except Exception:
            continue

    if found:
        new_pairs = sorted(set(found))[:MAX_PAIRS]

        removed = set(PAIRS) - set(new_pairs)
        for pair in removed:
            STREAM_STARTED.discard(pair)
            STREAM_CACHE.pop(pair, None)

        PAIRS = new_pairs
        LAST_REFRESH = now
        logger.info("OTC seleccionados: %d/%d", len(PAIRS), len(set(found)))

    return PAIRS


# ============================================================
# IQ SERVER TIME
# ============================================================
def server_ts() -> float:
    try:
        if IQ is not None:
            return float(IQ.get_server_timestamp())
    except Exception:
        pass
    return time.time()


def floor_ts(ts: float) -> int:
    return int(ts // M1) * M1


# ============================================================
# CONNECTION / STREAMS
# ============================================================
def connect():
    global IQ

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise ConnectionError(reason)

    refresh_pairs(True)

    tg(
        "🟢 IQ OPTION CONECTADO\n\n"
        "CHOPPINESS INDEX CI(14)\n"
        "CALL: cruce ↑ 61.8 + vela ROJA\n"
        "PUT: cruce ↓ 38.2 + vela VERDE\n"
        "Entrada: siguiente M1\n"
        "Expiración: 1 minuto\n"
        f"OTC: {len(PAIRS)}"
    )


def ensure_connection() -> bool:
    if IQ is None:
        return False

    try:
        if IQ.check_connect():
            return True
    except Exception:
        pass

    try:
        ok = bool(IQ.connect()[0])
        if ok:
            STREAM_STARTED.clear()
            STREAM_CACHE.clear()
        return ok
    except Exception:
        return False


def ensure_stream(pair: str) -> bool:
    if IQ is None:
        return False

    if pair in STREAM_STARTED:
        return True

    try:
        IQ.start_candles_stream(pair, M1, CANDLE_COUNT_M1)
        STREAM_STARTED.add(pair)
        logger.info("Stream M1 iniciado: %s", pair)
        return True
    except Exception as exc:
        logger.warning("No se pudo iniciar stream %s: %s", pair, exc)
        return False


def read_stream(pair: str):
    if IQ is None or not ensure_stream(pair):
        return STREAM_CACHE.get(pair)

    try:
        raw = IQ.get_realtime_candles(pair, M1)
        if not raw:
            return STREAM_CACHE.get(pair)

        rows = []
        for candle in raw.values():
            if not isinstance(candle, dict):
                continue

            rows.append(
                {
                    "from": candle.get("from"),
                    "open": candle.get("open"),
                    "high": candle.get("max", candle.get("high")),
                    "low": candle.get("min", candle.get("low")),
                    "close": candle.get("close"),
                }
            )

        data = pd.DataFrame(rows)
        required = ["from", "open", "high", "low", "close"]

        if data.empty or any(c not in data.columns for c in required):
            return STREAM_CACHE.get(pair)

        for c in required:
            data[c] = pd.to_numeric(data[c], errors="coerce")

        data = (
            data.dropna(subset=required)
            .drop_duplicates("from")
            .sort_values("from")
            .reset_index(drop=True)
        )

        if not data.empty:
            STREAM_CACHE[pair] = data
            return data

    except Exception:
        pass

    return STREAM_CACHE.get(pair)


def update_stream_cache():
    global LAST_STREAM_READ

    now = time.monotonic()
    if now - LAST_STREAM_READ < STREAM_REFRESH:
        return

    LAST_STREAM_READ = now

    if not PAIRS:
        return

    workers = max(1, min(WORKERS, len(PAIRS)))

    def worker(pair):
        try:
            read_stream(pair)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(worker, PAIRS))


# ============================================================
# CLOSED-CANDLE ANALYSIS
# ============================================================
def get_closed_context(pair: str, entry_candle_ts: int):
    """Devuelve únicamente velas cerradas antes de la M1 de entrada.

    Si la entrada empieza en T, la última vela analizada DEBE ser T-60.
    Esto evita analizar la vela M1 que está abierta.
    """
    data = STREAM_CACHE.get(pair)
    if data is None or data.empty:
        return None

    context = data[data["from"] < entry_candle_ts].copy()
    if context.empty:
        return None

    context = (
        context.drop_duplicates("from")
        .sort_values("from")
        .reset_index(drop=True)
    )

    expected_last = entry_candle_ts - M1
    if int(context.iloc[-1]["from"]) != expected_last:
        # El stream todavía no entregó la vela cerrada inmediatamente anterior.
        return None

    return context


def analyze_pair(pair: str, entry_candle_ts: int):
    context = get_closed_context(pair, entry_candle_ts)
    if context is None or len(context) < 30:
        return None

    try:
        result = analyze_market(context, pair=pair, mode="M1_M1")
    except Exception as exc:
        logger.warning("Error CI %s: %s", pair, exc)
        return None

    signal = result.get("signal")
    if signal not in ("call", "put"):
        return None

    analysis = result.get("analysis", {})

    # Seguridad adicional: la vela que generó la señal debe ser exactamente T-60.
    signal_from = analysis.get("signal_candle_from")
    if signal_from is not None and int(signal_from) != entry_candle_ts - M1:
        return None

    return {
        "pair": pair,
        "signal": signal,
        "reason": result.get("reason", "señal CI"),
        "entry_candle_ts": entry_candle_ts,
        "signal_candle_ts": entry_candle_ts - M1,
        "analysis": analysis,
    }


# ============================================================
# ORDER EXECUTION
# ============================================================
def buy(candidate):
    try:
        result = IQ.buy(
            AMOUNT,
            candidate["pair"],
            candidate["signal"],
            EXPIRATION,
        )

        if isinstance(result, tuple):
            return bool(result[0]), result[1] if len(result) > 1 else None

        return result not in (False, None, -1), result

    except Exception as exc:
        logger.error("buy %s: %s", candidate["pair"], exc)
        return False, None


def execute(candidate):
    pair = candidate["pair"]
    signal = candidate["signal"]
    entry_candle_ts = int(candidate["entry_candle_ts"])

    # Nunca duplicar la misma señal en la misma vela de entrada.
    if TRADED_CANDLE.get(pair) == entry_candle_ts:
        return False

    now = server_ts()
    current_candle_ts = floor_ts(now)

    # La entrada solo es válida en la M1 inmediatamente posterior al cruce.
    if current_candle_ts != entry_candle_ts:
        return False

    delay = now - entry_candle_ts

    # Si perdimos la apertura, NO entrar tarde.
    if delay < 0 or delay > MAX_ENTRY_DELAY:
        return False

    ok, order_id = buy(candidate)
    if not ok:
        return False

    TRADED_CANDLE[pair] = entry_candle_ts

    logger.info(
        "ENTRY EXECUTED | %s | %s | CI | señal=%d | entrada=%d | "
        "retraso=%.2fs | ID=%s | %s",
        pair,
        signal.upper(),
        candidate["signal_candle_ts"],
        entry_candle_ts,
        delay,
        order_id,
        candidate["reason"],
    )

    tg(
        "⚡ ENTRADA EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        "Estrategia: CI(14)\n"
        f"Razón: {candidate['reason']}\n"
        "Señal: vela M1 cerrada\n"
        "Entrada: siguiente M1\n"
        f"Retraso desde apertura: {delay:.2f}s\n"
        "Expiración: 1 minuto\n"
        f"ID: {order_id}"
    )

    return True


# ============================================================
# MAIN PROCESS
# ============================================================
def process():
    refresh_pairs()
    if not PAIRS:
        return

    update_stream_cache()

    # T = apertura de la M1 actual.
    # La señal válida tiene que estar en la vela cerrada T-60.
    entry_candle_ts = floor_ts(server_ts())

    def worker(pair):
        try:
            return analyze_pair(pair, entry_candle_ts)
        except Exception:
            return None

    workers = max(1, min(WORKERS, len(PAIRS)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(worker, PAIRS))

    candidates = [r for r in results if r]
    if not candidates:
        return

    # Mantiene el orden de los 30 pares. No se inventa un ranking.
    pair_order = {pair: idx for idx, pair in enumerate(PAIRS)}
    candidates.sort(key=lambda x: pair_order.get(x["pair"], 999999))

    for candidate in candidates:
        if execute(candidate):
            break


def main():
    global BOT_RUNNING

    if not all((IQ_EMAIL, IQ_PASSWORD)):
        logger.error("Faltan IQ_EMAIL/IQ_PASSWORD")
        return

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=telegram_loop, daemon=True).start()

    try:
        connect()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option")
        tg(f"❌ ERROR DE CONEXIÓN\n\n{exc}")
        return

    # El bot arranca ACTIVO. /stop lo detiene y /start lo vuelve a activar.
    BOT_RUNNING = True

    while True:
        try:
            if not BOT_RUNNING:
                time.sleep(0.25)
                continue

            if not ensure_connection():
                time.sleep(1)
                continue

            process()
            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            BOT_RUNNING = False
            break
        except Exception as exc:
            logger.exception("Error principal: %s", exc)
            time.sleep(1)


if __name__ == "__main__":
    main()
