from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from strategy import analyze_market

# ---------------------------------------------------------------------------
# COMPATIBILIDAD IQ OPTION
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------
M1 = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "500"))
MAX_PAIRS = 50
WORKERS = 30
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "180"))
PAIR_REFRESH_SECONDS = 600.0
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))
STREAM_REFRESH = float(os.getenv("STREAM_REFRESH", "0.10"))

# ---------------------------------------------------------------------------
# ESTADO
# ---------------------------------------------------------------------------
PAIRS: list[str] = []
LAST_REFRESH = 0.0
LAST_EVENT = -1
TRADED_FORCE_CANDLES: set[tuple[str, int]] = set()
LAST_TRADE_TIME = 0.0
LAST_TRADE_ENTRY = -1
LAST_TRADE_DIRECTION: dict[str, str] = {}
BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
STREAM_STARTED: set[str] = set()
STREAM_CACHE: dict[str, pd.DataFrame] = {}
STREAM_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def tg(msg: str) -> None:
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


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def telegram_loop() -> None:
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
                        "M1→M1 | expiración 1 minuto\n"
                        "OTC analizados: 30\n"
                        "Tendencia: velas M1\n"
                        "CALL: tendencia alcista + CI cruza arriba 61.8\n"
                        "PUT: tendencia bajista + CI cruza abajo 38.2\n"
                        "Alternancia por par: CALL ↔ PUT\n"
                        "Entrada en cualquier momento dentro de la vela de fuerza M1."
                    )

                elif command == "/stop":
                    BOT_RUNNING = False
                    tg("🔴 BOT DETENIDO")

                elif command == "/status":
                    tg(
                        f"📊 ESTADO\n\n"
                        f"{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\n"
                        f"OTC: {len(PAIRS)}/30\n"
                        f"Importe: {AMOUNT:g}\n"
                        "Analisis: M1\n"
                        "CI: 14 | 61.8 / 38.2\n"
                        "CALL = tendencia alcista + cruce arriba\n"
                        "PUT = tendencia bajista + cruce abajo\n"
                        "Entrada: durante toda la vela de fuerza M1"
                    )

        except Exception:
            time.sleep(1)


# ---------------------------------------------------------------------------
# OTC
# ---------------------------------------------------------------------------
def is_otc(name: str) -> bool:
    n = str(name).upper()
    return n.endswith("-OTC") or n.endswith("_OTC") or "OTC" in n


def refresh_pairs(force: bool = False) -> list[str]:
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
        logger.info(
            "OTC seleccionados: %d/%d | %s",
            len(PAIRS),
            len(set(found)),
            ", ".join(PAIRS),
        )
        start_streams(PAIRS)

    return PAIRS


# ---------------------------------------------------------------------------
# TIEMPO / CONEXION
# ---------------------------------------------------------------------------
def server_ts() -> float:
    try:
        return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:
        return time.time()


def floor_m1(ts: float) -> int:
    return int(ts // M1) * M1


def connect() -> None:
    global IQ

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise ConnectionError(reason)

    refresh_pairs(True)
    logger.info("IQ conectado | server=%.3f", server_ts())

    tg(
        "🟢 IQ OPTION CONECTADO\n\n"
        "M1→M1 | expiración 1 minuto\n"
        f"OTC seleccionados: {len(PAIRS)}/30\n"
        "Analisis exclusivo M1\n"
        "Tendencia M1 + cruce CI 14"
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
        ok = IQ.connect()[0]
        if ok:
            with STREAM_LOCK:
                STREAM_STARTED.clear()
                STREAM_CACHE.clear()
            start_streams(PAIRS)
        return bool(ok)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# STREAM M1 EN TIEMPO REAL
# ---------------------------------------------------------------------------
def start_streams(pairs: list[str]) -> None:
    if IQ is None:
        return

    for pair in pairs:
        if pair in STREAM_STARTED:
            continue
        try:
            IQ.start_candles_stream(pair, M1, CANDLE_COUNT_M1)
            STREAM_STARTED.add(pair)
            logger.info("STREAM M1 iniciado | %s", pair)
        except Exception as exc:
            logger.warning("No se pudo iniciar stream %s: %s", pair, exc)


def _realtime_to_df(pair: str) -> Optional[pd.DataFrame]:
    if IQ is None:
        return None

    try:
        raw = IQ.get_realtime_candles(pair, M1)
    except Exception as exc:
        logger.debug("Realtime %s: %s", pair, exc)
        return None

    if not isinstance(raw, dict) or not raw:
        return None

    rows = []
    for ts, candle in raw.items():
        if not isinstance(candle, dict):
            continue
        row = dict(candle)
        row["from"] = ts
        rows.append(row)

    if not rows:
        return None

    data = pd.DataFrame(rows).rename(columns={"max": "high", "min": "low"})
    required = ["from", "open", "high", "low", "close"]
    if any(c not in data.columns for c in required):
        return None

    for c in required:
        data[c] = pd.to_numeric(data[c], errors="coerce")

    data = (
        data.dropna(subset=required)
        .drop_duplicates("from")
        .sort_values("from")
        .reset_index(drop=True)
    )
    return data if not data.empty else None


def update_stream_cache() -> None:
    if not PAIRS:
        return

    for pair in PAIRS:
        data = _realtime_to_df(pair)
        if data is not None:
            with STREAM_LOCK:
                STREAM_CACHE[pair] = data


def get_current_m1_from_stream(pair: str) -> Optional[pd.DataFrame]:
    with STREAM_LOCK:
        data = STREAM_CACHE.get(pair)
        if data is None or data.empty:
            return None
        return data.copy().sort_values("from").reset_index(drop=True)


# ---------------------------------------------------------------------------
# ANALISIS M1
# ---------------------------------------------------------------------------
def analyze_pair(pair: str, force_ts: int) -> dict:
    data = get_current_m1_from_stream(pair)
    if data is None or data.empty:
        return {"pair": pair, "signal": None, "reason": "stream M1 no disponible"}

    current_ts = int(data.iloc[-1]["from"])
    if current_ts != force_ts:
        return {"pair": pair, "signal": None, "reason": "vela M1 actual no coincide"}

    result = analyze_market(df=data, pair=pair, mode="M1_M1")
    signal = result.get("signal")
    if signal not in ("call", "put"):
        return {"pair": pair, "signal": None, "reason": result.get("reason", "sin señal")}

    return {
        "pair": pair, "signal": signal, "reason": result.get("reason", ""),
        "score": result.get("score", 0), "analysis": result.get("analysis", {}),
        "analysis_ts": current_ts, "force_ts": force_ts, "expiration": EXPIRATION,
    }


def analyze_event(force_ts: int) -> Optional[dict]:
    if not PAIRS:
        return None
    results = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(PAIRS))) as executor:
        futures = [executor.submit(analyze_pair, pair, force_ts) for pair in PAIRS]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                logger.exception("Error analizando par: %s", exc)

    valid = [r for r in results if r.get("signal") in ("call", "put")]
    filtered = []
    for r in valid:
        last_direction = LAST_TRADE_DIRECTION.get(r["pair"])
        if last_direction == r["signal"]:
            logger.info("SEÑAL BLOQUEADA | %s | %s repetido", r["pair"], r["signal"].upper())
            continue
        if (r["pair"], force_ts) in TRADED_FORCE_CANDLES:
            continue
        filtered.append(r)

    logger.info("ANALISIS INTRABAR M1 | fuerza=%s | tiempo=%.3fs | %s", force_ts, time.time()-started,
                " | ".join(f"{r['pair']}={r['signal'].upper()}" for r in filtered) if filtered else "sin señal")
    if len(filtered) > 1:
        filtered.sort(key=lambda x: PAIRS.index(x["pair"]))
    return filtered[0] if filtered else None


# ---------------------------------------------------------------------------
# ORDEN
# ---------------------------------------------------------------------------
def buy(candidate: dict):
    try:
        return IQ.buy(
            AMOUNT,
            candidate["pair"],
            candidate["signal"],
            int(candidate["expiration"]),
        )
    except Exception as exc:
        logger.error("buy: %s", exc)
        return False, None


def execute(candidate: dict, force_ts: int) -> bool:
    global LAST_TRADE_TIME, LAST_TRADE_ENTRY, LAST_TRADE_DIRECTION

    now = server_ts()
    current_force = floor_m1(now)
    if current_force != force_ts:
        return False

    pair = candidate["pair"]
    signal = candidate["signal"]
    previous = LAST_TRADE_DIRECTION.get(pair)
    if previous == signal:
        logger.info("SEÑAL DESCARTADA | %s | %s repetido", pair, signal.upper())
        return False
    if (pair, force_ts) in TRADED_FORCE_CANDLES:
        return False
    if time.time() - LAST_TRADE_TIME < TRADE_COOLDOWN:
        return False

    logger.info("ENTRADA DENTRO DE VELA FUERZA | %s | %s | fuerza=%s | exp=1m | %s", pair, signal.upper(), force_ts, candidate["reason"])
    result = buy(candidate)
    ok = bool(result[0]) if isinstance(result, tuple) else result not in (False, None, -1, "error")
    order_id = result[1] if isinstance(result, tuple) and len(result) > 1 else result
    if not ok:
        tg("❌ ORDEN RECHAZADA\n\n" f"Par: {pair}\n" f"Dirección: {signal.upper()}\n" f"Razón: {candidate['reason']}\n" "Expiración: 1 minuto")
        return False

    TRADED_FORCE_CANDLES.add((pair, force_ts))
    LAST_TRADE_ENTRY = force_ts
    LAST_TRADE_TIME = time.time()
    LAST_TRADE_DIRECTION[pair] = signal
    tg("⚡ ENTRADA EJECUTADA\n\n" f"Par: {pair}\n" "Modo: M1 intrabar\n" f"Dirección: {signal.upper()}\n" f"Razón: {candidate['reason']}\n" "Vela de fuerza: misma M1\n" "Expiración: 1 minuto\n" f"ID: {order_id}")
    return True


# ---------------------------------------------------------------------------
# CICLO
# ---------------------------------------------------------------------------
def process() -> None:
    refresh_pairs()
    if not PAIRS:
        return
    now = server_ts()
    force_ts = floor_m1(now)
    update_stream_cache()
    candidate = analyze_event(force_ts)
    if candidate:
        execute(candidate, force_ts)

    # Limpieza para no crecer indefinidamente. Conservamos las ultimas 5 M1.
    if len(TRADED_FORCE_CANDLES) > 500:
        cutoff = force_ts - 5 * M1
        TRADED_FORCE_CANDLES.difference_update({x for x in TRADED_FORCE_CANDLES if x[1] < cutoff})


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    global BOT_RUNNING

    required = (
        IQ_EMAIL,
        IQ_PASSWORD,
        TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID,
    )

    if not all(required):
        logger.error("Faltan IQ_EMAIL/IQ_PASSWORD/TELEGRAM_TOKEN/TELEGRAM_CHAT_ID")
        return

    threading.Thread(target=telegram_loop, daemon=True).start()

    try:
        connect()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option")
        tg(f"❌ ERROR DE CONEXIÓN\n\n{exc}")
        return

    tg(
        "🤖 BOT LISTO\n\n"
        "M1→M1 | expiración 1 minuto\n"
        "30 pares OTC\n"
        "Analisis exclusivo de velas M1\n"
        "CALL: M1 alcista + CI cruza arriba 61.8 + vela verde de rechazo\n"
        "PUT: M1 bajista + CI cruza abajo 38.2 + vela roja de rechazo\n"
        "Alternancia por par: CALL ↔ PUT\n"
        "Entrada tardía: bloqueada.\n\n"
        "Usa /start para activar."
    )

    last_stream_update = 0.0

    while True:
        try:
            if not BOT_RUNNING:
                time.sleep(0.10)
                continue

            if not ensure_connection():
                time.sleep(1)
                continue

            now = time.time()
            if now - last_stream_update >= STREAM_REFRESH:
                start_streams(PAIRS)
                update_stream_cache()
                last_stream_update = now

            process()
            time.sleep(0.02)

        except KeyboardInterrupt:
            BOT_RUNNING = False
            break

        except Exception as exc:
            logger.exception("Error principal: %s", exc)
            time.sleep(1)


if __name__ == "__main__":
    main()
