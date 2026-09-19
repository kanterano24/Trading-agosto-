from __future__ import annotations

import json
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
EXPIRATION = 1  # Fijo: solo operaciones con expiración de 1 minuto
AMOUNT = float(os.getenv("AMOUNT", "800"))

# Cuenta de IQ Option: PRACTICE o REAL
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "PRACTICE").strip().upper()

# Límite total de entradas por ejecución del bot
MAX_TOTAL_TRADES = 100
TOTAL_TRADES = 0
CANDLE_COUNT = max(60, int(os.getenv("CANDLE_COUNT", "80")))
MAX_OTC_PAIRS = max(1, int(os.getenv("MAX_OTC_PAIRS", "2")))

# Modo de estudio: por defecto concentra el bot en FARTCOINUSD-OTC.
STUDY_PAIR = os.getenv("STUDY_PAIR", "FARTCOINUSD-OTC").strip().upper()
STUDY_ONLY_PAIR = os.getenv("STUDY_ONLY_PAIR", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
STUDY_LOG_DIR = os.getenv("STUDY_LOG_DIR", "trade_study")
STUDY_LOG_FILE = os.path.join(STUDY_LOG_DIR, "trades.jsonl")

PAIR_REFRESH_SECONDS = float(os.getenv("PAIR_REFRESH_SECONDS", "900"))  # 15 minutos
SNIPER_POLL = 0.06
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))
MIN_HISTORY = 35
MIN_ROOM_TO_OPPOSITE_ATR = float(os.getenv("MIN_ROOM_TO_OPPOSITE_ATR", "0.90"))

# Inicia el análisis automáticamente después de conectar a IQ Option.
# Puede desactivarse con AUTO_START=false y usar /start desde Telegram.
AUTO_START = os.getenv("AUTO_START", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

# La API publica de strategy.py debe exponer solamente entry_type=force.
REQUIRE_FORCE = True
MIN_ACCEPTED_SCORE = int(os.getenv("MIN_ACCEPTED_SCORE", "90"))
REQUIRE_N_PLUS_1 = False


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
STUDY_LOG_LOCK = threading.RLock()


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
    global BOT_RUNNING, TOTAL_TRADES

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
                description = str(data.get("description", ""))
                if response.status_code == 409 or "terminated by other getUpdates request" in description.lower():
                    logger.warning("Telegram 409: otra instancia está usando getUpdates; pausa de 30 s")
                    time.sleep(30)
                else:
                    time.sleep(1)
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
                    with STATE_LOCK:
                        TOTAL_TRADES = 0
                        PENDING_ENTRY.clear()
                    BOT_RUNNING = True
                    telegram_send(
                        "🟢 BOT ACTIVADO\\n\\n"
                        "⚡ BINARY OTC | FUERZA\\n"
                        f"Cuenta: {ACCOUNT_TYPE}\\n"
                        f"Entradas: 0/{MAX_TOTAL_TRADES}\\n"
                        "📌 Análisis de estructura y ejecución al comenzar N\\n"
                        f"⏱ Temporalidad: {TIMEFRAME // 60} minuto(s)\\n"
                        f"⏳ Expiración: {EXPIRATION} minuto(s)\\n"
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
                        "Entrada: inicio de N a favor de la estructura\n"
                        f"Expiración: {EXPIRATION} minuto(s)\n"
                        f"Importe: {AMOUNT:g}\\n"
                        f"Cuenta: {ACCOUNT_TYPE}\\n"
                        f"Entradas: {TOTAL_TRADES}/{MAX_TOTAL_TRADES}\\n"
                        f"Pares OTC: {len(PAIRS)}\n"
                        f"Estudio: {STUDY_PAIR if STUDY_ONLY_PAIR else 'todos los pares'}"
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


def _supports_one_minute_expiration(info: Dict[str, Any]) -> bool:
    """
    Acepta pares cuya metadata declara expiración de 1 minuto.
    Si IQ Option no publica ese dato en el catálogo, no descarta el par;
    la orden se fuerza igualmente a EXPIRATION=1.
    """
    if not isinstance(info, dict):
        return False

    values = []
    keys = (
        "expiration", "expirations", "expiration_time",
        "expiration_times", "expiration_period", "expiration_periods",
        "durations", "duration", "available_expirations",
    )

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).lower()
                if any(token in key_text for token in keys):
                    values.append(item)
                collect(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(value)
        elif isinstance(value, str):
            values.append(value)

    collect(info)
    if not values:
        return True

    found_one = False
    found_expiration_data = False
    for value in values:
        text = str(value).lower()
        numbers = []
        import re
        for match in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)?", text):
            try:
                number = float(match[0])
            except (TypeError, ValueError):
                continue
            unit = match[1]
            minutes = number / 60.0 if unit in {"s"} else number
            numbers.append(minutes)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numbers.append(float(value))
        if numbers:
            found_expiration_data = True
            if any(abs(number - 1.0) < 1e-9 for number in numbers):
                found_one = True

    return found_one if found_expiration_data else True


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

        if not _supports_one_minute_expiration(info):
            logger.debug("Par descartado: no declara expiración de 1 minuto: %s", name)
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
    if STUDY_ONLY_PAIR:
        selected = [p for p in discovered if p.upper() == STUDY_PAIR]
    else:
        # Prioriza el par de estudio y completa el universo con un segundo par.
        # Si STUDY_PAIR no está disponible, toma los dos primeros pares OTC.
        prioritized = [p for p in discovered if p.upper() == STUDY_PAIR]
        remaining = [p for p in discovered if p.upper() != STUDY_PAIR]
        selected = (prioritized + remaining)[:MAX_OTC_PAIRS]
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

    if ACCOUNT_TYPE not in {"PRACTICE", "REAL"}:
        raise ValueError("ACCOUNT_TYPE debe ser PRACTICE o REAL")

    IQ.change_balance(ACCOUNT_TYPE)
    logger.info("Cuenta seleccionada: %s", ACCOUNT_TYPE)

    refresh_binary_otc_pairs(force=True)

    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "📊 BB + ATR Trailing Stops + RSI\n"
        "⚡ Análisis de estructura; ejecución al comenzar N\n"
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


def study_candles(df: pd.DataFrame, until_ts: int, count: int = 10) -> list[Dict[str, Any]]:
    """Devuelve las últimas velas cerradas previas a la entrada, con OHLC."""
    if df is None or df.empty:
        return []

    sample = df[df["from"].astype(int) <= int(until_ts)].tail(count)
    result: list[Dict[str, Any]] = []
    for _, row in sample.iterrows():
        values = candle_values(row)
        values["timestamp"] = int(row["from"])
        result.append(values)
    return result


def _study_write(event: Dict[str, Any]) -> None:
    """Guarda eventos de entrada y resultado en JSONL para su análisis posterior."""
    try:
        os.makedirs(STUDY_LOG_DIR, exist_ok=True)
        with STUDY_LOG_LOCK:
            with open(STUDY_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("No se pudo guardar estudio: %s", exc)


def _normalize_trade_result(value: Any) -> tuple[str, Optional[float]]:
    try:
        profit = float(value)
    except (TypeError, ValueError):
        return "unknown", None

    if profit > 0:
        return "win", profit
    if profit < 0:
        return "loss", profit
    return "draw", profit


def track_trade_result(pair: str, pending: Dict[str, Any], order_id: Any) -> None:
    """Consulta el resultado de una operación binaria y lo agrega al registro."""
    if IQ is None or order_id in (None, "", False):
        _study_write({
            "event": "result",
            "pair": pair,
            "order_id": order_id,
            "outcome": "unknown",
            "reason": "missing_order_id_or_iq",
        })
        return

    checker = getattr(IQ, "check_win_v4", None) or getattr(IQ, "check_win_v3", None)
    if checker is None:
        _study_write({
            "event": "result",
            "pair": pair,
            "order_id": order_id,
            "outcome": "unknown",
            "reason": "iqoptionapi_result_method_unavailable",
        })
        return

    deadline = time.time() + max(180, EXPIRATION * 60 + 60)
    while time.time() < deadline:
        try:
            raw_result = checker(order_id)
            if (
                raw_result is not None
                and raw_result is not False
                and raw_result != ""
                and raw_result != "pending"
            ):
                outcome, profit = _normalize_trade_result(raw_result)
                event = {
                    "event": "result",
                    "pair": pair,
                    "order_id": order_id,
                    "signal": pending.get("signal"),
                    "entry_timestamp": pending.get("execution_ts"),
                    "outcome": outcome,
                    "profit": profit,
                    "resolved_at": int(time.time()),
                }
                _study_write(event)
                telegram_send(
                    "📚 RESULTADO REGISTRADO\n\n"
                    f"Par: {pair}\n"
                    f"Dirección: {str(pending.get('signal', '')).upper()}\n"
                    f"Resultado: {outcome.upper()}\n"
                    f"Ganancia/Pérdida: {profit if profit is not None else 'N/D'}\n"
                    f"ID: {order_id}"
                )
                return
        except Exception as exc:
            logger.debug("%s | error consultando resultado %s: %s", pair, order_id, exc)
        time.sleep(2)

    _study_write({
        "event": "result",
        "pair": pair,
        "order_id": order_id,
        "outcome": "unknown",
        "reason": "result_timeout",
        "resolved_at": int(time.time()),
    })


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

    try:
        if int(result.get("score", 0) or 0) < MIN_ACCEPTED_SCORE:
            return False
    except (TypeError, ValueError):
        return False

    entry_type = str(result.get("entry_type") or "").strip().lower()
    analysis = result.get("analysis") or {}

    if REQUIRE_FORCE and entry_type != "force":
        return False

    if not REQUIRE_FORCE and entry_type not in {"force", "bb_atr_rsi"}:
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


def revalidate_pending_location(
    pair: str,
    pending: Dict[str, Any],
) -> bool:
    """
    Revalida el espacio disponible justo antes de ejecutar N.

    La señal se prepara con N-1, pero el precio puede desplazarse
    durante el cambio de vela. Si el recorrido restante hasta el
    ultimo extremo estructural ya no cumple el minimo, se descarta.
    """
    analysis = pending.get("analysis") or {}
    atr = float(analysis.get("atr") or 0.0)
    if atr <= 0.0 or IQ is None:
        return False

    # strategy.py vv2 expone los pivotes; si un pivote no existe,
    # usamos la resistencia/soporte calculados como respaldo.
    last_high = analysis.get("last_swing_high")
    last_low = analysis.get("last_swing_low")
    if last_high is None:
        last_high = analysis.get("resistance")
    if last_low is None:
        last_low = analysis.get("support")

    def level_value(value: Any) -> Optional[float]:
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            try:
                return float(value[1])
            except (TypeError, ValueError):
                return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    signal = str(pending.get("signal") or "").lower()
    opposite = (
        level_value(last_high)
        if signal == "call"
        else level_value(last_low)
    )
    if opposite is None:
        return False

    current = get_closed_candles(pair)
    if current is None or current.empty:
        return False

    current_price = float(current.iloc[-1]["close"])
    if signal == "call":
        room = opposite - current_price
    elif signal == "put":
        room = current_price - opposite
    else:
        return False

    room_atr = room / atr
    valid = room_atr >= MIN_ROOM_TO_OPPOSITE_ATR

    logger.info(
        "%s | revalidacion inicio de N | signal=%s | room=%.2f ATR | valido=%s",
        pair,
        signal,
        room_atr,
        valid,
    )
    return valid


# ============================================================
# ANALISIS DE ESTRUCTURA Y PREPARACION DE ENTRADA EN N
# ============================================================


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return "N/D"


def _candle_diagnostic(label: str, candle: Any) -> str:
    if not isinstance(candle, dict):
        return f"{label}: N/D"
    return (
        f"{label}: "
        f"O={_fmt_price(candle.get('open'))} | "
        f"H={_fmt_price(candle.get('high'))} | "
        f"L={_fmt_price(candle.get('low'))} | "
        f"C={_fmt_price(candle.get('close'))} | "
        f"Rango={_fmt_price(candle.get('range'))} | "
        f"Cuerpo={_fmt_price(candle.get('body'))} | "
        f"MechaSup={_fmt_price(candle.get('upper'))} | "
        f"MechaInf={_fmt_price(candle.get('lower'))} | "
        f"PosCierre={_fmt_price(candle.get('close_position'))}"
    )


def _diagnostic_message(
    pair: str,
    signal: str,
    result: Dict[str, Any],
    rejection_label: str = "RETROCESO N-2",
    confirmation_label: str = "CONTINUACION N-1",
) -> str:
    analysis = result.get("analysis") or {}
    rejection = analysis.get("rejection_candle") or {}
    confirmation = analysis.get("confirmation_candle") or {}
    direction = str(signal or "").upper()

    return (
        "📊 DIAGNÓSTICO TÉCNICO\n\n"
        f"Par: {pair}\n"
        f"Dirección: {direction}\n"
        f"Score: {result.get('score', 0)}/100\n"
        f"Calidad: {result.get('entry_quality', 0)}/100\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n"
        f"ATR: {_fmt_price(analysis.get('atr'))}\n"
        f"Soporte: {_fmt_price(analysis.get('support'))}\n"
        f"Resistencia: {_fmt_price(analysis.get('resistance'))}\n"
        f"Tolerancia: {_fmt_price(analysis.get('tolerance'))}\n"
        f"EMA rápida: {_fmt_price(analysis.get('fast_ema'))}\n"
        f"EMA lenta: {_fmt_price(analysis.get('slow_ema'))}\n"
        f"Contexto alcista: {analysis.get('bullish_context')}\n"
        f"Contexto bajista: {analysis.get('bearish_context')}\n\n"
        f"{_candle_diagnostic(rejection_label, rejection)}\n"
        f"{_candle_diagnostic(confirmation_label, confirmation)}\n\n"
        f"Timestamp {rejection_label}: {analysis.get('rejection_timestamp', 'N/D')}\n"
        f"Timestamp {confirmation_label}: {analysis.get('confirmation_timestamp', 'N/D')}\n\n"
        f"Motivos: {result.get('reason', '')}"
    )


def analysis_message(pair: str, ts: int, result: Dict[str, Any]) -> str:
    analysis = result.get("analysis") or {}

    return (
        "🔎 ANÁLISIS DE FUERZA\n\n"
        f"Par: {pair}\n"
        f"Vela N-1: {ts}\n"
        f"Dirección: {result.get('signal')}\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n"
        f"Fase: {analysis.get('impulse_phase', 'unknown')}\n"
        f"Tipo: {result.get('entry_type')}\n"
        f"Score: {result.get('score', 0)}/100\n"
        f"Calidad: {result.get('entry_quality', analysis.get('entry_quality', 0))}/100\n\n"
        f"{result.get('reason', '')}"
    )


def analyze_closed_candle(pair: str, expected_closed_ts: int) -> bool:
    """Analiza la última vela cerrada y ejecuta inmediatamente la señal válida."""
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
    study_rows = study_candles(df, expected_closed_ts, 10)

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
        "%s | VELA CERRADA | signal=%s | type=%s | score=%s | %s",
        pair, signal, result.get("entry_type"), score, result.get("reason", ""),
    )

    if not is_force_signal(result, signal):
        return True
    if trade_limit_reached() or cooldown_active(pair):
        return True
    if LAST_TRADE_CANDLE.get(pair) == int(expected_closed_ts):
        return True

    values = candle_values(closed_row)
    ok, order_id = buy_binary(pair, signal)
    if not ok:
        telegram_send(
            "❌ ORDEN RECHAZADA\n\n"
            f"Par: {pair}\n"
            f"Dirección: {str(signal).upper()}\n"
            f"Vela detectada: {expected_closed_ts}\n"
            "La orden fue rechazada por IQ Option."
        )
        return False

    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = int(expected_closed_ts)
    _study_write({
        "event": "entry",
        "pair": pair,
        "signal": signal,
        "order_id": order_id,
        "score": score,
        "entry_type": result.get("entry_type"),
        "analysis": analysis,
        "reason": result.get("reason", ""),
        "signal_timestamp": int(expected_closed_ts),
        "execution_timestamp": int(get_iq_server_timestamp()),
        "entry_price": values["close"],
        "candles": study_rows,
    })

    side = "CALL 🟢" if signal == "call" else "PUT 🔴"
    telegram_send(
        "⚡ ENTRADA EJECUTADA INMEDIATAMENTE\n\n"
        f"Par: {pair}\n"
        f"Dirección: {side}\n"
        f"Tipo: {result.get('entry_type')}\n"
        f"Score: {score}/100\n"
        f"Calidad: {analysis.get('entry_quality', result.get('entry_quality', 0))}/100\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n"
        f"Vela detectada: {expected_closed_ts}\n"
        f"ID: {order_id}\n\n"
        "✅ La operación se ejecutó al detectar la señal.\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
        f"{result.get('reason', '')}\n\n"
        + _diagnostic_message(pair, signal, result)
    )
    return True


# ============================================================
# ANALISIS Y EJECUCION EN LA MISMA VELA
# ============================================================

def analyze_live_candle(pair: str, current_ts: int) -> bool:
    """Analiza la vela activa y ejecuta una sola vez por vela de señal."""
    if trade_limit_reached():
        return False
    df = get_closed_candles(pair)
    if df is None or df.empty or "from" not in df.columns:
        return False

    df = df.sort_values("from").drop_duplicates("from", keep="last").reset_index(drop=True)
    current_rows = df[df["from"].astype(int) == int(current_ts)]
    if current_rows.empty:
        return False

    current_row = current_rows.iloc[-1]
    history = df[df["from"].astype(int) < int(current_ts)].copy()
    if len(history) < MIN_HISTORY - 1:
        return False

    result = analyze_market(
        candle_1m=current_row.to_dict(),
        previous_m1=history,
        pair=pair,
    )
    signal = result.get("signal")
    if not is_force_signal(result, signal):
        return False

    if LAST_TRADE_CANDLE.get(pair) == int(current_ts):
        return False
    if cooldown_active(pair):
        return False

    analysis = result.get("analysis") or {}
    ok, order_id = buy_binary(pair, signal)
    if not ok:
        logger.warning("%s | orden rechazada | señal=%s", pair, signal)
        return False

    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = int(current_ts)

    telegram_send(
        "✅ ENTRADA EJECUTADA EN VELA DE SEÑAL\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        f"Tipo: {result.get('entry_type')}\n"
        f"Score: {result.get('score', 0)}/100\n"
        f"RSI: {analysis.get('rsi')}\n"
        f"ATR posición: {analysis.get('atr_position')}\n"
        f"Vela: {current_ts}\n"
        f"ID: {order_id}\n\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
        f"{result.get('reason', '')}"
    )
    logger.info(
        "%s | EJECUTADO MISMA VELA | %s | ts=%s | ID=%s",
        pair, signal.upper(), current_ts, order_id,
    )
    return True


# ============================================================
# EJECUCION
# ============================================================

def cooldown_active(pair: str) -> bool:
    return time.time() - LAST_TRADE_TIME.get(pair, 0.0) < TRADE_COOLDOWN


def trade_limit_reached() -> bool:
    with STATE_LOCK:
        return TOTAL_TRADES >= MAX_TOTAL_TRADES


def buy_binary(pair: str, signal: str) -> Tuple[bool, Optional[Any]]:
    global TOTAL_TRADES, BOT_RUNNING

    if IQ is None or signal not in ("call", "put"):
        return False, None

    # El bloqueo cubre la comprobación y el envío para evitar
    # que dos hilos consuman el mismo cupo simultáneamente.
    with STATE_LOCK:
        if TOTAL_TRADES >= MAX_TOTAL_TRADES:
            return False, None

        try:
            result = IQ.buy(AMOUNT, pair, signal, EXPIRATION)

            if isinstance(result, tuple):
                ok = bool(result[0])
                order_id = result[1] if len(result) > 1 else None
            else:
                ok = result not in (None, False, "error", -1)
                order_id = result

            if not ok:
                return False, order_id

            TOTAL_TRADES += 1
            current_trades = TOTAL_TRADES

            if TOTAL_TRADES >= MAX_TOTAL_TRADES:
                BOT_RUNNING = False
                PENDING_ENTRY.clear()

        except Exception as exc:
            logger.error("%s | buy error: %s", pair, exc)
            return False, None

    logger.info(
        "%s | ENTRADA %s/%s | cuenta=%s",
        pair,
        current_trades,
        MAX_TOTAL_TRADES,
        ACCOUNT_TYPE,
    )

    if current_trades >= MAX_TOTAL_TRADES:
        telegram_send(
            "🛑 LIMITE DE ENTRADAS ALCANZADO\\n\\n"
            f"Entradas ejecutadas: {current_trades}/{MAX_TOTAL_TRADES}\\n"
            f"Cuenta: {ACCOUNT_TYPE}\\n"
            "El bot se detuvo automáticamente.\\n"
            "No se abrirán nuevas operaciones."
        )

    return True, order_id


def execute_sniper(pair: str, pending: Dict[str, Any]) -> bool:
    execution_ts = int(pending["execution_ts"])
    signal = str(pending["signal"])

    current_ts = floor_candle_timestamp(get_iq_server_timestamp())

    if current_ts < execution_ts:
        return False

    if current_ts > execution_ts:
        with STATE_LOCK:
            PENDING_ENTRY.pop(pair, None)

        logger.info("%s | señal para N vencida y descartada", pair)
        return False

    if LAST_TRADE_CANDLE.get(pair) == execution_ts:
        return False

    if cooldown_active(pair):
        return False

    if floor_candle_timestamp(get_iq_server_timestamp()) != execution_ts:
        return False

    if not revalidate_pending_location(pair, pending):
        with STATE_LOCK:
            PENDING_ENTRY.pop(pair, None)

        analysis = pending.get("analysis") or {}
        current_df = get_closed_candles(pair)
        current_close = None
        if current_df is not None and not current_df.empty:
            current_close = float(current_df.iloc[-1]["close"])

        last_high = analysis.get("last_swing_high")
        last_low = analysis.get("last_swing_low")
        opposite_level = last_high if signal == "call" else last_low
        if isinstance(opposite_level, (tuple, list)) and len(opposite_level) >= 2:
            opposite_level = opposite_level[1]

        atr_value = float(analysis.get("atr") or 0.0)
        room_value = None
        room_atr_value = None
        if current_close is not None and opposite_level is not None:
            try:
                room_value = (
                    float(opposite_level) - current_close
                    if signal == "call"
                    else current_close - float(opposite_level)
                )
                if atr_value > 0:
                    room_atr_value = room_value / atr_value
            except (TypeError, ValueError):
                pass

        telegram_send(
            "🚫 ENTRADA DESCARTADA\n\n"
            f"Par: {pair}\n"
            f"Dirección: {signal.upper()}\n"
            "El espacio disponible no cumplió el mínimo antes de ejecutar.\n\n"
            f"Precio actual: {_fmt_price(current_close)}\n"
            f"Nivel opuesto: {_fmt_price(opposite_level)}\n"
            f"ATR: {_fmt_price(atr_value)}\n"
            f"Espacio: {_fmt_price(room_value)}\n"
            f"Espacio en ATR: {_fmt_price(room_atr_value)}\n"
            f"Mínimo requerido: {MIN_ROOM_TO_OPPOSITE_ATR:.2f} ATR\n"
            f"N: {pending.get('continuity_ts')}\n"
            f"Entrada programada: {pending.get('execution_ts')}\n\n"
            "La entrada no se ejecutará."
        )
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
            f"Entrada en N: {execution_ts}\n"
            "La señal no se trasladará a otra vela."
        )
        return False

    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = execution_ts

    with STATE_LOCK:
        PENDING_ENTRY.pop(pair, None)

    pending_analysis = pending.get("analysis") or {}
    _study_write({
        "event": "entry",
        "pair": pair,
        "order_id": order_id,
        "signal": signal,
        "entry_timestamp": execution_ts,
        "analysis_timestamp": pending.get("continuity_ts"),
        "score": pending.get("score"),
        "entry_type": pending.get("entry_type"),
        "structure": pending_analysis.get("structure"),
        "reason": pending.get("reason", ""),
        "candles": pending.get("study_candles", []),
        "created_at": pending.get("created_at"),
        "recorded_at": int(time.time()),
    })
    threading.Thread(
        target=track_trade_result,
        args=(pair, dict(pending), order_id),
        daemon=True,
    ).start()
    telegram_send(
        "✅ FUERZA EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        f"Tipo: {pending.get('entry_type')}\n"
        f"Vela analizada: {pending.get('continuity_ts')}\n"
        f"Entrada programada: {execution_ts}\n"
        f"Reloj IQ: {sent_at:.3f}\n"
        f"ID: {order_id}\n"
        f"Precio de cierre N: {_fmt_price(pending.get('close'))}\n\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
        f"Estructura: {pending_analysis.get('structure', 'unknown')}\n"
        f"ATR: {_fmt_price(pending_analysis.get('atr'))}\n"
        f"Soporte: {_fmt_price(pending_analysis.get('support'))}\n"
        f"Resistencia: {_fmt_price(pending_analysis.get('resistance'))}\n"
        f"Motivos: {pending.get('reason', '')}"
    )

    logger.info(
        "%s | EJECUTADO | %s | análisis N-1=%s | entrada N=%s | ID=%s",
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

    # La señal se ejecuta inmediatamente al ser detectada en la vela cerrada.
    # No se utiliza una entrada pendiente para la siguiente vela.
    closed_ts = int(current_ts - TIMEFRAME)
    analyze_closed_candle(pair, closed_ts)


def analyze_all_pairs() -> None:
    if not BOT_RUNNING or trade_limit_reached():
        return

    refresh_binary_otc_pairs()

    for pair in list(PAIRS):
        if not BOT_RUNNING or trade_limit_reached():
            return

        try:
            process_pair(pair)
        except Exception:
            logger.exception("Error procesando %s", pair)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    global BOT_RUNNING, TOTAL_TRADES

    logger.info("========================================")
    logger.info("BOT BINARY OTC | BB + EMA + ATR + RSI | N-1 -> N")
    logger.info("TIMEFRAME=%s | EXPIRATION=%s (SOLO 1 MINUTO)", TIMEFRAME, EXPIRATION)
    logger.info("MAX OTC=%s | REFRESH=%ss | AMOUNT=%s | ACCOUNT=%s | MAX_TRADES=%s",
                MAX_OTC_PAIRS, int(PAIR_REFRESH_SECONDS), AMOUNT, ACCOUNT_TYPE, MAX_TOTAL_TRADES)
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

    # En Railway el proceso debe comenzar a trabajar sin depender de
    # un comando manual de Telegram. /stop sigue permitiendo detenerlo.
    with STATE_LOCK:
        TOTAL_TRADES = 0
        PENDING_ENTRY.clear()

    BOT_RUNNING = AUTO_START

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "📊 Filtro de estructura + pullback + continuidad\n"
        "⏱ Solo pares con expiración de 1 minuto\n"
        "⚡ Ejecución al detectar la señal en vela cerrada\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n"
        f"🔢 Pares analizados: {MAX_OTC_PAIRS}\n"
        f"🔄 Actualización de pares: cada {int(PAIR_REFRESH_SECONDS // 60)} minutos\n"
        f"🚀 Inicio automático: {'SI' if AUTO_START else 'NO'}\n\n"
        + (
            "🟢 Análisis automático activado."
            if AUTO_START
            else "Usa /start para activar."
        )
    )

    logger.info(
        "Motor de análisis: %s",
        "ACTIVO AUTOMÁTICAMENTE" if AUTO_START else "ESPERANDO /start",
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
