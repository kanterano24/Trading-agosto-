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

from strategy import analyze_market, MODE_CONFIG


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
# TIMEFRAMES
# ---------------------------------------------------------------------------

M1 = 60
M15 = 900

EXPIRATION = {"M1_M1": 1}

TIMEFRAME = {"M1": M1, "M15": M15}

MODE_LABEL = {"M1_M1": "M15→M1"}


# ---------------------------------------------------------------------------
# PERFORMANCE / SELECTION
# ---------------------------------------------------------------------------

AMOUNT = float(os.getenv("AMOUNT", "600"))

# Para M15 necesitamos bloques M1 suficientes para contexto y estructura.
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "240"))

# Se analizan exactamente 3 OTC disponibles.
MAX_PAIRS = 30

# 12 es un punto medio para acelerar sin disparar demasiado las peticiones
# simultaneas al websocket.
WORKERS = int(os.getenv("ANALYSIS_WORKERS", "12"))

PAIR_REFRESH_SECONDS = 600.0
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))

# Con la nueva estrategia, 80 significa confluencia fuerte.
MIN_SCORE = int(os.getenv("MIN_SCORE_TO_TRADE", "80"))


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

PAIRS: list[str] = []
LAST_REFRESH = 0.0

LAST_EVENT: dict[str, int] = {}
LAST_TRADE_ENTRY = -1
LAST_TRADE_TIME = 0.0

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
# TELEGRAM CONTROL
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
                        "M15→M1 | expiracion 1m\n"
                        f"OTC analizados: hasta {MAX_PAIRS}\n"
                        "Regla: M15 tendencia + M1 LH/LL o HL/HH + rechazo S/R + confirmación.\n"
                        "Solo una entrada por evento."
                    )

                elif command == "/stop":
                    BOT_RUNNING = False
                    tg("🔴 BOT DETENIDO")

                elif command == "/status":
                    tg(
                        f"📊 ESTADO\n\n"
                        f"{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\n"
                        f"OTC: {len(PAIRS)}\n"
                        f"Importe: {AMOUNT:g}\n"
                        f"Score minimo: {MIN_SCORE}"
                    )

        except Exception:
            time.sleep(1)


# ---------------------------------------------------------------------------
# OTC PAIRS
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
        # Stable: mismas 50 mientras el catalogo no cambie.
        PAIRS = sorted(set(found))[:MAX_PAIRS]
        LAST_REFRESH = now

        logger.info(
            "OTC seleccionados: %d/%d | %s",
            len(PAIRS),
            len(set(found)),
            ", ".join(PAIRS[:10]) + (" ..." if len(PAIRS) > 10 else ""),
        )

    return PAIRS


# ---------------------------------------------------------------------------
# TIME
# ---------------------------------------------------------------------------

def server_ts() -> float:
    try:
        return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:
        return time.time()


def floor_ts(ts: float, timeframe: int) -> int:
    return int(ts // timeframe) * timeframe


# ---------------------------------------------------------------------------
# CONNECTION
# ---------------------------------------------------------------------------

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
        "M15→M1 | expiración 1m\n"
        f"OTC seleccionados: {len(PAIRS)}\n"
        "Entrada: M15 + estructura M1 + rechazo S/R + ruptura + confirmacion"
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
# CANDLES
# ---------------------------------------------------------------------------

def get_m1(pair: str) -> Optional[pd.DataFrame]:
    try:
        candles = IQ.get_candles(
            pair,
            M1,
            CANDLE_COUNT_M1,
            server_ts(),
        )

        data = pd.DataFrame(candles).rename(
            columns={"max": "high", "min": "low"}
        )

        required = ["from", "open", "high", "low", "close"]

        if data.empty or any(c not in data.columns for c in required):
            return None

        for c in required:
            data[c] = pd.to_numeric(data[c], errors="coerce")

        data = (
            data
            .dropna(subset=required)
            .drop_duplicates("from")
            .sort_values("from")
            .reset_index(drop=True)
        )

        return data

    except Exception:
        return None


def aggregate_m1(
    m1: pd.DataFrame,
    timeframe: int,
    last_closed_start: int,
) -> pd.DataFrame:
    """Construye M15 solo con bloques M1 completos ya cerrados."""

    if m1 is None or m1.empty:
        return pd.DataFrame()

    d = m1.copy()
    d["from"] = d["from"].astype(int)

    # Solo bloques cuyo ultimo M1 ya esta cerrado.
    d = d[
        d["from"] <= int(last_closed_start + timeframe - M1)
    ]

    d["block"] = (d["from"] // timeframe) * timeframe

    expected_count = timeframe // M1
    rows = []

    for block, group in d.groupby("block", sort=True):
        group = group.sort_values("from")

        expected = [
            int(block) + i * M1
            for i in range(expected_count)
        ]
        actual = group["from"].astype(int).tolist()

        if actual != expected:
            continue

        rows.append(
            {
                "from": int(block),
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
            }
        )

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values("from")
        .reset_index(drop=True)
    )


def analysis_data(
    m1: pd.DataFrame,
    timeframe: int,
    closed_start: int,
) -> pd.DataFrame:
    if timeframe == M1:
        return (
            m1[m1["from"] <= closed_start]
            .copy()
            .reset_index(drop=True)
        )

    return aggregate_m1(
        m1,
        timeframe,
        closed_start,
    )


def higher_context(
    m1: pd.DataFrame,
    mode: str,
    closed_start: int,
) -> Optional[pd.DataFrame]:
    if mode == "M1_M1":
        return aggregate_m1(
            m1,
            M15,
            floor_ts(closed_start, M15),
        )
    return None


# ---------------------------------------------------------------------------
# STRATEGY ANALYSIS
# ---------------------------------------------------------------------------

def analyze_pair_mode(
    pair: str,
    mode: str,
    event_ts: int,
    m1: pd.DataFrame,
):
    cfg = MODE_CONFIG[mode]

    tf = TIMEFRAME[cfg["analysis_tf"]]
    closed_start = int(event_ts - tf)

    data = analysis_data(
        m1,
        tf,
        closed_start,
    )

    if data.empty or int(data.iloc[-1]["from"]) != closed_start:
        return {
            "pair": pair,
            "signal": None,
            "score": 0,
            "reason": "datos incompletos para la vela cerrada",
        }

    higher = higher_context(
        m1,
        mode,
        closed_start,
    )

    result = analyze_market(
        df=data,
        pair=pair,
        mode=mode,
        higher_tf_df=higher,
    )

    signal = result.get("signal")
    score = int(result.get("score", 0))
    reason = result.get("reason", "")

    # Diagnostico incluso cuando se bloquea.
    if signal not in ("call", "put") or score < MIN_SCORE:
        return {
            "pair": pair,
            "signal": signal,
            "score": score,
            "reason": reason or "sin señal",
        }

    # La vela que genera la entrada debe tener el mismo color que la orden.
    last = data.iloc[-1]

    if float(last["close"]) > float(last["open"]):
        candle_signal = "call"
    elif float(last["close"]) < float(last["open"]):
        candle_signal = "put"
    else:
        return {
            "pair": pair,
            "signal": None,
            "score": 0,
            "reason": "vela final neutra",
        }

    if signal != candle_signal:
        return {
            "pair": pair,
            "signal": None,
            "score": 0,
            "reason": "señal no coincide con vela cerrada",
        }

    return {
        "pair": pair,
        "mode": mode,
        "signal": signal,
        "score": score,
        "analysis_ts": closed_start,
        "entry_tf": TIMEFRAME[cfg["entry_tf"]],
        "entry_ts": int(event_ts),
        "expiration": int(cfg["expiration"]),
        "reason": reason,
        "analysis": result.get("analysis", {}),
    }


def analyze_event(
    mode: str,
    event_ts: int,
):
    candidates = []
    diagnostics = []

    def worker(pair: str):
        m1 = get_m1(pair)

        if m1 is None:
            return {
                "pair": pair,
                "signal": None,
                "score": 0,
                "reason": "sin datos M1",
            }

        try:
            return analyze_pair_mode(
                pair,
                mode,
                event_ts,
                m1,
            )
        except Exception as exc:
            logger.exception(
                "Error analizando %s %s",
                pair,
                mode,
            )
            return {
                "pair": pair,
                "signal": None,
                "score": 0,
                "reason": f"error: {type(exc).__name__}",
            }

    max_workers = max(
        1,
        min(WORKERS, len(PAIRS)),
    )

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:
        futures = {
            executor.submit(worker, pair): pair
            for pair in PAIRS
        }

        for future in as_completed(futures):
            try:
                result = future.result()

                if not result:
                    continue

                if result.get("signal") in ("call", "put") and int(
                    result.get("score", 0)
                ) >= MIN_SCORE:
                    candidates.append(result)
                else:
                    diagnostics.append(result)

            except Exception:
                pass

    # Siempre dejamos diagnostico en Railway.
    top_diag = sorted(
        diagnostics,
        key=lambda x: int(x.get("score", 0)),
        reverse=True,
    )[:8]

    if top_diag:
        logger.info(
            "FILTROS %s | %s",
            mode,
            " | ".join(
                f"{x.get('pair')} {x.get('reason', '')[:90]}"
                for x in top_diag
            ),
        )

    if not candidates:
        logger.info(
            "SIN CANDIDATO %s | evento=%s | pares=%d",
            mode,
            event_ts,
            len(PAIRS),
        )
        return None

    logger.info(
        "CANDIDATOS %s | total=%d | %s",
        mode,
        len(candidates),
        " | ".join(
            f"{c['pair']} {c['signal'].upper()} "
            f"score={c['score']} {c['reason']}"
            for c in sorted(
                candidates,
                key=lambda x: x["score"],
                reverse=True,
            )[:8]
        ),
    )

    # Una sola operacion por evento: la mayor confluencia.
    return max(
        candidates,
        key=lambda c: (
            int(c["score"]),
            1 if c["signal"] == "call" else 0,
        ),
    )


# ---------------------------------------------------------------------------
# ORDER
# ---------------------------------------------------------------------------

def buy(candidate):
    try:
        # No hay inversion.
        # CALL = alcista, PUT = bajista.
        return IQ.buy(
            AMOUNT,
            candidate["pair"],
            candidate["signal"],
            int(candidate["expiration"]),
        )

    except Exception as exc:
        logger.error("buy: %s", exc)
        return False, None


def execute(candidate) -> bool:
    global LAST_TRADE_ENTRY, LAST_TRADE_TIME

    now = server_ts()
    entry_tf = int(candidate["entry_tf"])
    current_entry = floor_ts(now, entry_tf)

    # Si el analisis termina antes de la apertura, esperamos la apertura
    # sin bloquear el resto del proceso.
    if current_entry < candidate["entry_ts"]:
        return False

    # Para una expiracion de 1 minuto no se entra tarde.
    # Si el analisis perdio la apertura, la senal queda invalidada.
    if current_entry > candidate["entry_ts"]:
        logger.info(
            "SEÑAL DESCARTADA POR RETRASO | %s | evento=%s | actual=%s",
            candidate["pair"], candidate["entry_ts"], current_entry,
        )
        return False

    if current_entry == LAST_TRADE_ENTRY:
        return False

    if time.time() - LAST_TRADE_TIME < TRADE_COOLDOWN:
        return False

    delay = max(
        0.0,
        now - candidate["entry_ts"],
    )

    logger.info(
        "EJECUTANDO | %s | %s | %s | score=%s | exp=%sm | "
        "delay=%.2fs | %s",
        candidate["pair"],
        MODE_LABEL[candidate["mode"]],
        candidate["signal"].upper(),
        candidate["score"],
        candidate["expiration"],
        delay,
        candidate["reason"],
    )

    ok_result = buy(candidate)

    ok = (
        bool(ok_result[0])
        if isinstance(ok_result, tuple)
        else ok_result not in (False, None, "error", -1)
    )

    order_id = (
        ok_result[1]
        if isinstance(ok_result, tuple) and len(ok_result) > 1
        else ok_result
    )

    if not ok:
        tg(
            f"❌ ORDEN RECHAZADA\n\n"
            f"Par: {candidate['pair']}\n"
            f"Modo: {MODE_LABEL[candidate['mode']]}\n"
            f"Dirección: {candidate['signal'].upper()}\n"
            f"Score: {candidate['score']}/100\n"
            f"Expiración: {candidate['expiration']} min"
        )
        return False

    LAST_TRADE_ENTRY = current_entry
    LAST_TRADE_TIME = time.time()

    tg(
        f"⚡ ENTRADA EJECUTADA\n\n"
        f"Par: {candidate['pair']}\n"
        f"Modo: {MODE_LABEL[candidate['mode']]}\n"
        f"Análisis: {candidate['analysis'].get('analysis_timeframe', MODE_LABEL[candidate['mode']])}\n"
        f"Dirección: {candidate['signal'].upper()}\n"
        f"Score: {candidate['score']}/100\n"
        f"Razón: {candidate['reason']}\n"
        f"Expiración: {candidate['expiration']} min\n"
        f"Retraso: {delay:.2f}s\n"
        f"ID: {order_id}"
    )

    return True


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def process() -> None:
    refresh_pairs()

    if not PAIRS:
        return

    now = server_ts()
    current_m1 = floor_ts(now, M1)

    events = [("M1_M1", current_m1)]

    for mode, event_ts in events:
        event_key = f"{mode}:{event_ts}"

        if LAST_EVENT.get(mode) == event_ts:
            continue

        LAST_EVENT[mode] = event_ts

        started = time.time()

        candidate = analyze_event(
            mode,
            event_ts,
        )

        elapsed = time.time() - started

        if not candidate:
            continue

        execute(candidate)


def main() -> None:
    global BOT_RUNNING

    required = (
        IQ_EMAIL,
        IQ_PASSWORD,
        TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID,
    )

    if not all(required):
        logger.error(
            "Faltan IQ_EMAIL/IQ_PASSWORD/"
            "TELEGRAM_TOKEN/TELEGRAM_CHAT_ID"
        )
        return

    threading.Thread(
        target=telegram_loop,
        daemon=True,
    ).start()

    try:
        connect()
    except Exception as exc:
        logger.exception(
            "No se pudo iniciar IQ Option"
        )
        tg(
            f"❌ ERROR DE CONEXIÓN\n\n{exc}"
        )
        return

    tg(
        "🤖 BOT LISTO\n\n"
        "M15→M1 | analisis M1 | expiracion 1m\n"
        f"Hasta {MAX_PAIRS} OTC.\n"
        "Entrada: M15 + estructura M1 + rechazo S/R + confirmación.\n"
        "Usa /start para activar."
    )

    while True:
        try:
            if not BOT_RUNNING:
                time.sleep(0.25)
                continue

            if not ensure_connection():
                time.sleep(1)
                continue

            process()
            time.sleep(0.05)

        except KeyboardInterrupt:
            BOT_RUNNING = False
            break

        except Exception as exc:
            logger.exception(
                "Error principal: %s",
                exc,
            )
            time.sleep(1)


if __name__ == "__main__":
    main()
