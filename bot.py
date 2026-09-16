from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code


# ============================================================
# COMPATIBILIDAD IQOPTIONAPI - SOLO BINARY OTC
# ============================================================

def _binary_only_digital_underlying(self):
    return {"underlying": []}


def _disabled_digital_open(self, *args, **kwargs):
    return None


setattr(IQ_Option, "get_digital_underlying_list_data",
        _binary_only_digital_underlying)

for _name in (
    "_IQ_Option__get_digital_open",
    "__get_digital_open",
    "_get_digital_open",
):
    if hasattr(IQ_Option, _name):
        setattr(IQ_Option, _name, _disabled_digital_open)


from strategy import analyze_market


# ============================================================
# CONFIGURACION
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = int(os.getenv("EXPIRATION", "1"))
AMOUNT = float(os.getenv("AMOUNT", "5000"))
CANDLE_COUNT = max(60, int(os.getenv("CANDLE_COUNT", "80")))
MAX_OTC_PAIRS = max(1, int(os.getenv("MAX_OTC_PAIRS", "50")))

PAIR_REFRESH_SECONDS = 60.0
SNIPER_POLL = 0.06
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))
MIN_HISTORY = 35

# La API publica de strategy.py debe exponer solamente entry_type=force.
REQUIRE_FORCE = True
REQUIRE_N_PLUS_1 = True


# ============================================================
# ESTADO
# ============================================================

BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
PAIRS: list[str] = []
LAST_PAIR_REFRESH = 0.0

LIVE_STATE: Dict[str, Dict[str, Any]] = {}
PENDING_ENTRY: Dict[str, Dict[str, Any]] = {}
LAST_TRADE_TIME: Dict[str, float] = {}
LAST_TRADE_CANDLE: Dict[str, int] = {}

STATE_LOCK = threading.RLock()


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================

def _telegram_post(
    endpoint: str,
    data: Dict[str, Any],
    timeout: float = 3.0,
) -> bool:
    if not TELEGRAM_TOKEN:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}",
            data=data,
            timeout=timeout,
        )
        return response.status_code == 200
    except Exception as exc:
        logger.debug("Telegram %s: %s", endpoint, exc)
        return False


def telegram_send(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    def worker() -> None:
        _telegram_post(
            "sendMessage",
            {"chat_id": TELEGRAM_CHAT_ID, "text": message},
        )

    threading.Thread(target=worker, daemon=True).start()


def telegram_command_loop() -> None:
    global BOT_RUNNING

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    last_update_id: Optional[int] = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    while True:
        try:
            params: Dict[str, Any] = {"timeout": 1}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            response = requests.get(url, params=params, timeout=3)
            data = response.json()

            if not data.get("ok"):
                time.sleep(0.5)
                continue

            for update in data.get("result", []):
                uid = update.get("update_id")
                if uid is not None:
                    last_update_id = int(uid)

                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                text = str(message.get("text", "")).strip().lower()

                if text == "/start":
                    BOT_RUNNING = True
                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "⚡ BINARY OTC | FUERZA\n"
                        "📌 Análisis en N y ejecución en N+1\n"
                        f"⏱ Temporalidad: {TIMEFRAME // 60} minuto(s)\n"
                        f"⏳ Expiración: {EXPIRATION} minuto(s)\n"
                        f"💵 Importe: {AMOUNT:g}"
                    )

                elif text == "/stop":
                    BOT_RUNNING = False
                    with STATE_LOCK:
                        PENDING_ENTRY.clear()
                    telegram_send(
                        "🔴 BOT DETENIDO\n\n"
                        "No se abrirán nuevas operaciones."
                    )

                elif text == "/status":
                    status = "🟢 ACTIVO" if BOT_RUNNING else "🔴 DETENIDO"
                    telegram_send(
                        "📊 ESTADO\n\n"
                        f"Estado: {status}\n"
                        "Mercado: BINARY OTC\n"
                        "Filtro: FUERZA\n"
                        "Entrada: N+1\n"
                        f"Expiración: {EXPIRATION} minuto(s)\n"
                        f"Importe: {AMOUNT:g}\n"
                        f"Pares OTC: {len(PAIRS)}"
                    )

        except Exception as exc:
            logger.debug("Telegram commands: %s", exc)
            time.sleep(1)


# ============================================================
# OTC
# ============================================================

def _is_otc_pair(value: Any) -> bool:
    try:
        name = str(value).strip().upper()
    except Exception:
        return False

    return name.endswith("-OTC") or name.endswith("_OTC") or "OTC" in name


def _load_binary_otc_catalog() -> Tuple[list[str], bool]:
    if IQ is None or not hasattr(IQ, "get_all_init_v2"):
        return [], False

    try:
        data = IQ.get_all_init_v2()
    except Exception as exc:
        logger.warning("Catálogo BINARY no disponible: %s", exc)
        return [], False

    if not isinstance(data, dict):
        return [], False

    binary = data.get("binary")
    if not isinstance(binary, dict):
        result = data.get("result")
        if isinstance(result, dict):
            binary = result.get("binary")

    if not isinstance(binary, dict):
        return [], False

    actives = binary.get("actives", {})
    if not isinstance(actives, dict):
        return [], False

    pairs: list[str] = []

    for active_id, info in actives.items():
        if not isinstance(info, dict):
            continue

        raw_name = info.get("name")
        if not isinstance(raw_name, str):
            continue

        name = raw_name.split(".", 1)[1] if "." in raw_name else raw_name
        name = name.strip()

        if not _is_otc_pair(name):
            continue
        if info.get("enabled", True) is False:
            continue
        if info.get("is_suspended", info.get("suspended", False)) is True:
            continue

        try:
            numeric_id = int(active_id)
        except (TypeError, ValueError):
            continue

        OP_code.ACTIVES[name] = numeric_id
        pairs.append(name)

    return sorted(set(pairs)), True


def discover_binary_otc_pairs() -> list[str]:
    pairs, ok = _load_binary_otc_catalog()
    return sorted(set(p for p in pairs if _is_otc_pair(p))) if ok else []


def refresh_binary_otc_pairs(force: bool = False) -> list[str]:
    global PAIRS, LAST_PAIR_REFRESH

    now = time.time()
    if not force and now - LAST_PAIR_REFRESH < PAIR_REFRESH_SECONDS:
        return list(PAIRS)

    discovered = discover_binary_otc_pairs()
    selected = discovered[:MAX_OTC_PAIRS]
    previous = set(PAIRS)
    current = set(selected)

    with STATE_LOCK:
        PAIRS = list(selected)
        LAST_PAIR_REFRESH = now

        for pair in previous - current:
            PENDING_ENTRY.pop(pair, None)
            LIVE_STATE.pop(pair, None)
            LAST_TRADE_CANDLE.pop(pair, None)

    if current != previous:
        logger.info("Universo OTC actualizado: %s pares", len(selected))
        telegram_send(
            "🔄 UNIVERSO OTC ACTUALIZADO\n\n"
            f"Pares disponibles: {len(selected)}/{MAX_OTC_PAIRS}"
        )

    return list(PAIRS)


# ============================================================
# RELOJ Y CONEXION
# ============================================================

def get_iq_server_timestamp() -> float:
    if IQ is None:
        return time.time()

    try:
        value = float(IQ.get_server_timestamp())
        if value > 0:
            return value
    except Exception:
        pass

    return time.time()


def floor_candle_timestamp(timestamp: float) -> int:
    return int(timestamp // TIMEFRAME) * TIMEFRAME


def connect_iq() -> bool:
    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise ValueError("Faltan IQ_EMAIL/IQ_PASSWORD")

    logger.info("Conectando a IQ Option...")
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

    connected, reason = IQ.connect()
    if not connected:
        raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")

    refresh_binary_otc_pairs(force=True)

    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "⚡ MODO FUERZA\n"
        "📌 N cerrada → N+1\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)"
    )

    logger.info("IQ conectado | server=%.3f", get_iq_server_timestamp())
    return True


def ensure_connection() -> bool:
    global IQ

    try:
        if IQ is None:
            return connect_iq()

        if IQ.check_connect():
            return True

        logger.warning("Conexión perdida. Reconectando...")
        connected, reason = IQ.connect()

        if not connected:
            logger.error("No se pudo reconectar: %s", reason)
            return False

        refresh_binary_otc_pairs(force=True)
        telegram_send("🟢 IQ OPTION RECONECTADO")
        return True

    except Exception as exc:
        logger.error("Error de conexión: %s", exc)
        return False


# ============================================================
# VELAS CERRADAS
# ============================================================

def realtime_dataframe(pair: str) -> pd.DataFrame:
    return pd.DataFrame()


def get_closed_candles(pair: str) -> Optional[pd.DataFrame]:
    if IQ is None:
        return None

    try:
        candles = IQ.get_candles(
            pair,
            TIMEFRAME,
            CANDLE_COUNT,
            get_iq_server_timestamp(),
        )
        if not candles:
            return None

        df = pd.DataFrame(candles).rename(
            columns={"max": "high", "min": "low"}
        )

        required = ["from", "open", "high", "low", "close"]
        if any(column not in df.columns for column in required):
            return None

        for column in required:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df.dropna(subset=required, inplace=True)
        df["from"] = df["from"].astype(int)

        return (
            df.drop_duplicates("from", keep="last")
            .sort_values("from")
            .tail(CANDLE_COUNT)
            .reset_index(drop=True)
        )

    except Exception as exc:
        logger.debug("%s | historial: %s", pair, exc)
        return None


def get_row_by_ts(df: pd.DataFrame, ts: int) -> Optional[pd.Series]:
    if df is None or df.empty or "from" not in df.columns:
        return None

    rows = df[df["from"].astype(int) == int(ts)]
    return None if rows.empty else rows.iloc[-1]


def candle_values(row: pd.Series) -> Dict[str, float]:
    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


# ============================================================
# FILTRO DE ESTRATEGIA
# ============================================================

def is_force_signal(result: Dict[str, Any], signal: Any) -> bool:
    """
    El bot acepta únicamente señales públicas de FUERZA.
    La validación principal debe realizarse en strategy.py.
    """

    if signal not in ("call", "put"):
        return False

    entry_type = str(result.get("entry_type") or "").strip().lower()
    analysis = result.get("analysis") or {}

    if REQUIRE_FORCE and entry_type != "force":
        return False

    if REQUIRE_FORCE and analysis.get("force") is False:
        return False

    expected_direction = "bullish" if signal == "call" else "bearish"
    returned_direction = result.get("direction")

    if returned_direction not in (None, expected_direction):
        return False

    # Si strategy.py expone pullback, se exige que sea válido.
    pullback = analysis.get("pullback")
    if isinstance(pullback, dict):
        if pullback.get("valid") is False:
            return False

        # Ambas confirmaciones son obligatorias cuando el campo existe.
        for key in (
            "previous_candle_confirmed",
            "extreme_confirmed",
        ):
            if key in pullback and pullback.get(key) is not True:
                return False

    return True


# ============================================================
# ANALISIS Y PREPARACION N+1
# ============================================================

def analysis_message(pair: str, ts: int, result: Dict[str, Any]) -> str:
    analysis = result.get("analysis") or {}

    return (
        "🔎 ANÁLISIS DE FUERZA\n\n"
        f"Par: {pair}\n"
        f"Vela N: {ts}\n"
        f"Dirección: {result.get('signal')}\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n"
        f"Fase: {analysis.get('impulse_phase', 'unknown')}\n"
        f"Tipo: {result.get('entry_type')}\n"
        f"Score: {result.get('score', 0)}/100\n"
        f"Calidad: {result.get('entry_quality', analysis.get('entry_quality', 0))}/100\n\n"
        f"{result.get('reason', '')}"
    )


def analyze_closed_candle(pair: str, expected_closed_ts: int) -> bool:
    df = realtime_dataframe(pair)
    closed_row = get_row_by_ts(df, expected_closed_ts)

    if closed_row is None:
        df = get_closed_candles(pair)
        closed_row = get_row_by_ts(df, expected_closed_ts) if df is not None else None

    if closed_row is None or df is None or len(df) < MIN_HISTORY:
        return False

    df = df[df["from"].astype(int) <= expected_closed_ts].copy()
    df = df.sort_values("from").reset_index(drop=True)

    if len(df) < MIN_HISTORY:
        return False

    with STATE_LOCK:
        state = LIVE_STATE.get(pair)
        if state and int(state.get("analyzed_ts", -1)) == expected_closed_ts:
            return True

    result = analyze_market(
        candle_1m=closed_row.to_dict(),
        previous_m1=df.iloc[:-1].copy(),
        pair=pair,
    )

    signal = result.get("signal")
    score = int(result.get("score", 0) or 0)
    analysis = result.get("analysis") or {}

    with STATE_LOCK:
        LIVE_STATE[pair] = {
            "analyzed_ts": int(expected_closed_ts),
            "signal": signal,
            "score": score,
            "entry_type": result.get("entry_type"),
            "reason": result.get("reason", ""),
            "analysis": analysis,
            "created_at": time.time(),
        }

    logger.info(
        "%s | N CERRADA | signal=%s | type=%s | score=%s | %s",
        pair,
        signal,
        result.get("entry_type"),
        score,
        result.get("reason", ""),
    )

    if not is_force_signal(result, signal):
        return True

    values = candle_values(closed_row)
    execution_ts = int(expected_closed_ts + TIMEFRAME)

    pending = {
        "signal": signal,
        "score": score,
        "entry_type": result.get("entry_type"),
        "force": bool(analysis.get("force", True)),
        "continuity_ts": int(expected_closed_ts),
        "execution_ts": execution_ts,
        "open": values["open"],
        "high": values["high"],
        "low": values["low"],
        "close": values["close"],
        "reason": result.get("reason", ""),
        "analysis": analysis,
        "created_at": time.time(),
    }

    with STATE_LOCK:
        existing = PENDING_ENTRY.get(pair)

        if existing is not None:
            existing_ts = int(existing.get("execution_ts", 0))
            if existing_ts >= execution_ts:
                return True

        PENDING_ENTRY[pair] = pending

    side = "CALL 🟢" if signal == "call" else "PUT 🔴"
    telegram_send(
        "🎯 SEÑAL DE FUERZA ARMADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {side}\n"
        f"Tipo: {pending['entry_type']}\n"
        f"Score: {score}/100\n"
        f"Calidad: {analysis.get('entry_quality', result.get('entry_quality', 0))}/100\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n\n"
        f"Cierre N: {values['close']}\n"
        f"N cierre: {expected_closed_ts}\n"
        f"N+1: {execution_ts}\n\n"
        "🚫 N no se opera.\n"
        "⚡ Ejecutar únicamente en N+1.\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
        f"{result.get('reason', '')}"
    )

    return True


# ============================================================
# EJECUCION
# ============================================================

def cooldown_active(pair: str) -> bool:
    return time.time() - LAST_TRADE_TIME.get(pair, 0.0) < TRADE_COOLDOWN


def buy_binary(pair: str, signal: str) -> Tuple[bool, Optional[Any]]:
    if IQ is None or signal not in ("call", "put"):
        return False, None

    try:
        result = IQ.buy(AMOUNT, pair, signal, EXPIRATION)

        if isinstance(result, tuple):
            return bool(result[0]), result[1] if len(result) > 1 else None

        if result not in (None, False, "error", -1):
            return True, result

        return False, result

    except Exception as exc:
        logger.error("%s | buy error: %s", pair, exc)
        return False, None


def execute_sniper(pair: str, pending: Dict[str, Any]) -> bool:
    execution_ts = int(pending["execution_ts"])
    signal = str(pending["signal"])

    current_ts = floor_candle_timestamp(get_iq_server_timestamp())

    if current_ts < execution_ts:
        return False

    if current_ts > execution_ts:
        with STATE_LOCK:
            PENDING_ENTRY.pop(pair, None)

        logger.info("%s | señal N+1 vencida y descartada", pair)
        return False

    if LAST_TRADE_CANDLE.get(pair) == execution_ts:
        return False

    if cooldown_active(pair):
        return False

    if floor_candle_timestamp(get_iq_server_timestamp()) != execution_ts:
        return False

    sent_at = get_iq_server_timestamp()
    ok, order_id = buy_binary(pair, signal)

    if not ok:
        with STATE_LOCK:
            PENDING_ENTRY.pop(pair, None)

        telegram_send(
            "❌ ORDEN RECHAZADA\n\n"
            f"Par: {pair}\n"
            f"Dirección: {signal.upper()}\n"
            f"N+1: {execution_ts}\n"
            "La señal no se trasladará."
        )
        return False

    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = execution_ts

    with STATE_LOCK:
        PENDING_ENTRY.pop(pair, None)

    telegram_send(
        "✅ FUERZA EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        f"Tipo: {pending.get('entry_type')}\n"
        f"N cierre: {pending.get('continuity_ts')}\n"
        f"N+1: {execution_ts}\n"
        f"Reloj IQ: {sent_at:.3f}\n"
        f"ID: {order_id}\n\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)"
    )

    logger.info(
        "%s | EJECUTADO | %s | N=%s | N+1=%s | ID=%s",
        pair,
        signal.upper(),
        pending.get("continuity_ts"),
        execution_ts,
        order_id,
    )
    return True


# ============================================================
# MOTOR
# ============================================================

def process_pair(pair: str) -> None:
    if IQ is None:
        return

    current_ts = floor_candle_timestamp(get_iq_server_timestamp())
    closed_ts = current_ts - TIMEFRAME

    state = LIVE_STATE.get(pair)
    analyzed_ts = int(state.get("analyzed_ts", -1)) if state else -1

    if analyzed_ts != closed_ts:
        analyze_closed_candle(pair, closed_ts)

    pending = PENDING_ENTRY.get(pair)
    if pending is None:
        return

    if int(pending.get("execution_ts", -1)) != current_ts:
        return

    execute_sniper(pair, pending)


def analyze_all_pairs() -> None:
    if not BOT_RUNNING:
        return

    refresh_binary_otc_pairs()

    for pair in list(PAIRS):
        if not BOT_RUNNING:
            return

        try:
            process_pair(pair)
        except Exception:
            logger.exception("Error procesando %s", pair)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    global BOT_RUNNING

    logger.info("========================================")
    logger.info("BOT BINARY OTC | FUERZA | N+1")
    logger.info("TIMEFRAME=%s | EXPIRATION=%s", TIMEFRAME, EXPIRATION)
    logger.info("MAX OTC=%s | AMOUNT=%s", MAX_OTC_PAIRS, AMOUNT)
    logger.info("========================================")

    required = {
        "IQ_EMAIL": IQ_EMAIL,
        "IQ_PASSWORD": IQ_PASSWORD,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [key for key, value in required.items() if not value]
    if missing:
        logger.error("Faltan variables: %s", ", ".join(missing))
        return

    threading.Thread(target=telegram_command_loop, daemon=True).start()

    try:
        connect_iq()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option")
        telegram_send(f"❌ ERROR DE CONEXIÓN\n\n{exc}")
        return

    BOT_RUNNING = False

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "⚡ Solo señales de FUERZA\n"
        "📌 Analiza N cerrada\n"
        "🚫 No opera N\n"
        "⚡ Ejecuta en N+1\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
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

            analyze_all_pairs()
            time.sleep(SNIPER_POLL)

        except KeyboardInterrupt:
            BOT_RUNNING = False
            telegram_send("🔴 BOT DETENIDO MANUALMENTE")
            break

        except Exception as exc:
            logger.exception("Error principal")
            telegram_send(f"⚠️ ERROR EN BOT\n\n{exc}")
            time.sleep(1)


if __name__ == "__main__":
    main()
