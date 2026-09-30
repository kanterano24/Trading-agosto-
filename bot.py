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


# Bloqueo de operaciones digitales: el bot trabaja solamente BINARY OTC.
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
MAX_PAIRS = int(os.getenv("MAX_OTC_PAIRS", "30"))
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "120"))
PAIR_REFRESH_SECONDS = float(os.getenv("PAIR_REFRESH_SECONDS", "600"))
WORKERS = int(os.getenv("ANALYSIS_WORKERS", "30"))
LOOP_SLEEP = float(os.getenv("LOOP_SLEEP", "0.03"))

# Uma mesma vela M1 só pode gerar UMA entrada por par.
# Além disso, não se repete a mesma direção consecutivamente no mesmo par:
# CALL -> PUT -> CALL -> PUT...
ENFORCE_ALTERNATION = os.getenv("ENFORCE_ALTERNATION", "1").strip().lower() not in ("0", "false", "no", "off")

PAIRS = []
LAST_REFRESH = 0.0
IQ: Optional[IQ_Option] = None
BOT_RUNNING = False
STREAM_STARTED = set()
STREAM_CACHE = {}
TRADED_CANDLE = {}
LAST_DIRECTION = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def tg(msg):
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
                        "M1 | VELA DE FUERZA + RUPTURA\n"
                        "CALL = fuerza verde + ruptura máximo anterior\n"
                        "PUT = fuerza roja + ruptura mínimo anterior\n"
                        "Entrada dentro de la misma vela M1.\n"
                        f"OTC: hasta {MAX_PAIRS}\n"
                        f"Alternancia: {'SI' if ENFORCE_ALTERNATION else 'NO'}"
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
                        "Estrategia: vela de fuerza M1 + acción del precio\n"
                        "Indicadores: ninguno\n"
                        f"Alternancia CALL/PUT: {'ACTIVA' if ENFORCE_ALTERNATION else 'DESACTIVADA'}"
                    )

        except Exception:
            time.sleep(1)


def is_otc(name):
    n = str(name).upper()
    return n.endswith("-OTC") or n.endswith("_OTC") or "OTC" in n


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
        logger.warning("Catalogo OTC: %s", exc)
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
        PAIRS = sorted(set(found))[:MAX_PAIRS]
        LAST_REFRESH = now
        logger.info("OTC seleccionados: %d/%d", len(PAIRS), len(set(found)))

    return PAIRS


def server_ts():
    try:
        return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:
        return time.time()


def floor_ts(ts):
    return int(ts // M1) * M1


def connect():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise ConnectionError(reason)

    refresh_pairs(True)
    tg(
        "🟢 IQ OPTION CONECTADO\n\n"
        "M1 | VELA DE FUERZA + RUPTURA\n"
        "Sin indicadores.\n"
        f"OTC: {len(PAIRS)}\n"
        "Entrada dentro de la misma vela M1."
    )


def ensure_connection():
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
        return ok
    except Exception:
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
        logger.warning("stream %s: %s", pair, exc)
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

        return data if not data.empty else STREAM_CACHE.get(pair)

    except Exception:
        return STREAM_CACHE.get(pair)


def analyze_live_pair(pair):
    data = read_stream(pair)
    if data is None or len(data) < 8:
        return None

    candle_ts = floor_ts(server_ts())
    context = data[data["from"] <= candle_ts].copy()
    if context.empty:
        return None

    # Debemos estar analizando exactamente la vela M1 que está abierta ahora.
    if int(context.iloc[-1]["from"]) != candle_ts:
        return None

    result = analyze_market(context, pair=pair, mode="M1_M1")
    signal = result.get("signal")

    if signal not in ("call", "put"):
        return None
    if not result.get("force_candle"):
        return None
    if not result.get("price_action_confirmed"):
        return None

    # No permitir CALL->CALL ni PUT->PUT en el mismo par si la alternancia está activa.
    if ENFORCE_ALTERNATION and LAST_DIRECTION.get(pair) == signal:
        return None

    return {
        "pair": pair,
        "signal": signal,
        "reason": result["reason"],
        "candle_ts": candle_ts,
        "analysis": result.get("analysis", {}),
    }


def buy(candidate):
    try:
        result = IQ.buy(AMOUNT, candidate["pair"], candidate["signal"], EXPIRATION)
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

    # Una sola entrada dentro de una misma vela M1 por par.
    if TRADED_CANDLE.get(pair) == candle_ts:
        return False

    # La entrada debe seguir ocurriendo dentro de la misma vela que generó la señal.
    now = server_ts()
    if floor_ts(now) != candle_ts:
        return False

    # Segunda protección contra CALL->CALL / PUT->PUT.
    if ENFORCE_ALTERNATION and LAST_DIRECTION.get(pair) == signal:
        return False

    ok, order_id = buy(candidate)
    if not ok:
        return False

    TRADED_CANDLE[pair] = candle_ts
    LAST_DIRECTION[pair] = signal
    inside = max(0.0, now - candle_ts)

    logger.info(
        "ENTRADA | %s | %s | segundo=%.2f | ID=%s | %s",
        pair,
        signal.upper(),
        inside,
        order_id,
        candidate["reason"],
    )

    tg(
        "⚡ ENTRADA EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        "M1 | VELA DE FUERZA + RUPTURA\n"
        f"Entrada dentro de la vela: {inside:.2f}s\n"
        "Expiración: 1 minuto\n"
        "Indicadores: ninguno\n"
        f"Razón: {candidate['reason']}\n"
        f"ID: {order_id}"
    )
    return True


def process():
    refresh_pairs()
    if not PAIRS:
        return

    for pair in PAIRS:
        read_stream(pair)

    def worker(pair):
        try:
            return analyze_live_pair(pair)
        except Exception:
            return None

    workers = max(1, min(WORKERS, len(PAIRS)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(worker, PAIRS))

    candidates = [r for r in results if r]
    if not candidates:
        return

    # Mantiene el orden original de los 30 pares; no inventa ranking.
    candidates.sort(key=lambda x: PAIRS.index(x["pair"]))

    for candidate in candidates:
        if execute(candidate):
            break


def main():
    global BOT_RUNNING

    if not all((IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)):
        logger.error("Faltan IQ_EMAIL/IQ_PASSWORD/TELEGRAM_TOKEN/TELEGRAM_CHAT_ID")
        return

    threading.Thread(target=telegram_loop, daemon=True).start()

    try:
        connect()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option")
        tg(f"❌ ERROR DE CONEXIÓN\n\n{exc}")
        return

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
