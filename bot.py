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
# IQ OPTION / TELEGRAM
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
# OPERACION
# ---------------------------------------------------------------------------

M1 = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "5000"))

# Solo 3 pares OTC disponibles.
MAX_PAIRS = 6

# Tres workers: uno por cada par disponible.
WORKERS = 3

# La orden solo se permite al inicio de la nueva M1.
# Si el analisis tarda mas que esto, se descarta y NO se entra tarde.
MAX_ENTRY_DELAY = float(os.getenv("MAX_ENTRY_DELAY", "2.0"))

CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "120"))
PAIR_REFRESH_SECONDS = 600.0
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))


# ---------------------------------------------------------------------------
# ESTADO
# ---------------------------------------------------------------------------

PAIRS: list[str] = []
LAST_REFRESH = 0.0
LAST_EVENT = -1
LAST_TRADE_TIME = 0.0
LAST_TRADE_ENTRY = -1
BOT_RUNNING = False
IQ: Optional[IQ_Option] = None


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
                        "OTC analizados: 3\n"
                        "Patrón: último nivel → recorrido → regreso → rechazo → confirmación.\n"
                        "Entrada tardía: BLOQUEADA."
                    )

                elif command == "/stop":
                    BOT_RUNNING = False
                    tg("🔴 BOT DETENIDO")

                elif command == "/status":
                    tg(
                        f"📊 ESTADO\n\n"
                        f"{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\n"
                        f"OTC: {len(PAIRS)}/3\n"
                        f"Importe: {AMOUNT:g}\n"
                        f"Ventana máxima de entrada: {MAX_ENTRY_DELAY:.1f}s"
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
        "M1→M1 | 1 minuto\n"
        f"OTC seleccionados: {len(PAIRS)}/3\n"
        "La entrada se ejecuta solo en la nueva vela."
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
        return bool(IQ.connect()[0])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DATOS M1
# ---------------------------------------------------------------------------

def get_m1(pair: str) -> Optional[pd.DataFrame]:
    try:
        candles = IQ.get_candles(
            pair,
            M1,
            CANDLE_COUNT_M1,
            server_ts(),
        )
        data = pd.DataFrame(candles).rename(columns={"max": "high", "min": "low"})
        required = ["from", "open", "high", "low", "close"]
        if data.empty or any(c not in data.columns for c in required):
            return None

        for c in required:
            data[c] = pd.to_numeric(data[c], errors="coerce")

        return (
            data.dropna(subset=required)
            .drop_duplicates("from")
            .sort_values("from")
            .reset_index(drop=True)
        )
    except Exception as exc:
        logger.warning("Datos M1 %s: %s", pair, exc)
        return None


# ---------------------------------------------------------------------------
# ANALISIS
# ---------------------------------------------------------------------------

def analyze_pair(pair: str, event_ts: int) -> dict:
    m1 = get_m1(pair)
    if m1 is None:
        return {"pair": pair, "signal": None, "reason": "sin datos M1"}

    # Solo velas completamente cerradas antes de la nueva M1.
    closed_start = event_ts - M1
    data = m1[m1["from"] <= closed_start].copy().reset_index(drop=True)

    if data.empty or int(data.iloc[-1]["from"]) != closed_start:
        return {
            "pair": pair,
            "signal": None,
            "reason": "vela M1 cerrada no disponible",
        }

    result = analyze_market(
        df=data,
        pair=pair,
        mode="M1_M1",
        higher_tf_df=None,
    )

    if result.get("signal") not in ("call", "put"):
        return {
            "pair": pair,
            "signal": None,
            "reason": result.get("reason", "sin patrón confirmado"),
        }

    return {
        "pair": pair,
        "signal": result["signal"],
        "reason": result.get("reason", ""),
        "analysis": result.get("analysis", {}),
        "analysis_ts": closed_start,
        "entry_ts": event_ts,
        "expiration": EXPIRATION,
    }


def analyze_event(event_ts: int) -> Optional[dict]:
    if not PAIRS:
        return None

    results = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(PAIRS))) as executor:
        futures = [executor.submit(analyze_pair, pair, event_ts) for pair in PAIRS]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                logger.exception("Error analizando par: %s", exc)

    valid = [r for r in results if r.get("signal") in ("call", "put")]

    logger.info(
        "ANALISIS M1 | evento=%s | tiempo=%.3fs | %s",
        event_ts,
        time.time() - started,
        " | ".join(
            f"{r['pair']}={r['signal'].upper()}" for r in valid
        ) if valid else "sin patrón confirmado",
    )

    for r in results:
        if r.get("signal") not in ("call", "put"):
            logger.info("SIN SEÑAL | %s | %s", r["pair"], r.get("reason", ""))

    # Si dos pares tienen señal al mismo tiempo, no se inventa un ranking.
    # Se conserva el orden de los 3 pares seleccionados por el catálogo.
    if len(valid) > 1:
        valid.sort(key=lambda x: PAIRS.index(x["pair"]))

    return valid[0] if valid else None


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


def execute(candidate: dict, event_ts: int) -> bool:
    global LAST_TRADE_TIME, LAST_TRADE_ENTRY

    now = server_ts()
    delay = now - event_ts

    # Regla clave: nunca entrar en una vela ya avanzada.
    if floor_m1(now) != event_ts:
        logger.info(
            "SEÑAL DESCARTADA | %s | apertura perdida | delay=%.3fs",
            candidate["pair"], delay,
        )
        return False

    if delay < 0:
        return False

    if delay > MAX_ENTRY_DELAY:
        logger.info(
            "SEÑAL DESCARTADA | %s | entrada tardía %.3fs > %.3fs",
            candidate["pair"], delay, MAX_ENTRY_DELAY,
        )
        return False

    if LAST_TRADE_ENTRY == event_ts:
        return False

    if time.time() - LAST_TRADE_TIME < TRADE_COOLDOWN:
        return False

    logger.info(
        "ENTRADA | %s | %s | delay=%.3fs | exp=1m | %s",
        candidate["pair"],
        candidate["signal"].upper(),
        delay,
        candidate["reason"],
    )

    result = buy(candidate)
    ok = bool(result[0]) if isinstance(result, tuple) else result not in (False, None, -1, "error")
    order_id = result[1] if isinstance(result, tuple) and len(result) > 1 else result

    if not ok:
        tg(
            "❌ ORDEN RECHAZADA\n\n"
            f"Par: {candidate['pair']}\n"
            f"Dirección: {candidate['signal'].upper()}\n"
            "Expiración: 1 minuto"
        )
        return False

    LAST_TRADE_ENTRY = event_ts
    LAST_TRADE_TIME = time.time()

    tg(
        "⚡ ENTRADA EJECUTADA\n\n"
        f"Par: {candidate['pair']}\n"
        "Modo: M1→M1\n"
        f"Dirección: {candidate['signal'].upper()}\n"
        f"Razón: {candidate['reason']}\n"
        "Expiración: 1 minuto\n"
        f"Retraso desde apertura: {delay:.2f}s\n"
        f"ID: {order_id}"
    )
    return True


# ---------------------------------------------------------------------------
# CICLO
# ---------------------------------------------------------------------------

def process() -> None:
    global LAST_EVENT

    refresh_pairs()
    if not PAIRS:
        return

    now = server_ts()
    event_ts = floor_m1(now)

    if LAST_EVENT == event_ts:
        return

    # Solo se analiza una vez por apertura M1.
    LAST_EVENT = event_ts

    candidate = analyze_event(event_ts)
    if not candidate:
        return

    # Revisa de nuevo el reloj justo antes de enviar la orden.
    execute(candidate, event_ts)


def main() -> None:
    global BOT_RUNNING

    required = (IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
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
        "M1→M1 | expiración 1m\n"
        "3 pares OTC\n"
        "Patrón exacto: nivel → recorrido → regreso → rechazo → confirmación.\n"
        "Las entradas tardías se descartan.\n\n"
        "Usa /start para activar."
    )

    while True:
        try:
            if not BOT_RUNNING:
                time.sleep(0.10)
                continue

            if not ensure_connection():
                time.sleep(1)
                continue

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
