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

from strategy import analyze_market

# ============================================================
# CONFIGURACION
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "30"))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "PRACTICE").strip().upper()

MAX_TOTAL_TRADES = 100
TOTAL_TRADES = 0
CANDLE_COUNT = max(60, int(os.getenv("CANDLE_COUNT", "80")))
MAX_PAIRS = 50

STUDY_LOG_DIR = os.getenv("STUDY_LOG_DIR", "trade_study")
STUDY_LOG_FILE = os.path.join(STUDY_LOG_DIR, "trades.jsonl")

PAIR_REFRESH_SECONDS = float(os.getenv("PAIR_REFRESH_SECONDS", "900"))
SNIPER_POLL = 0.06
TRADE_COOLDOWN = float(os.getenv("TRADE_COOLDOWN", "60"))
MIN_HISTORY = 35
MIN_ROOM_TO_OPPOSITE_ATR = float(os.getenv("MIN_ROOM_TO_OPPOSITE_ATR", "1.00"))
MIN_STOCH_SEPARATION = float(os.getenv("MIN_STOCH_SEPARATION", "6.0"))
REQUIRE_STRUCTURE_BREAK = os.getenv("REQUIRE_STRUCTURE_BREAK", "true").strip().lower() in {"1", "true", "yes", "on"}
REQUIRE_NEW_REJECTION = os.getenv("REQUIRE_NEW_REJECTION", "true").strip().lower() in {"1", "true", "yes", "on"}
BOT_VERSION = "V3"

AUTO_START = os.getenv("AUTO_START", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

REQUIRE_FORCE = True
MIN_ACCEPTED_SCORE = int(os.getenv("MIN_ACCEPTED_SCORE", "90"))
REQUIRE_N_PLUS_1 = False

# ============================================================
# ESTADO
# ============================================================

BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
PAIRS: list[str] = []
PAIR_MARKET: Dict[str, str] = {}
LAST_PAIR_REFRESH = 0.0

LIVE_STATE: Dict[str, Dict[str, Any]] = {}
PENDING_ENTRY: Dict[str, Dict[str, Any]] = {}
LAST_TRADE_TIME: Dict[str, float] = {}
LAST_TRADE_CANDLE: Dict[str, int] = {}
LAST_REJECTION_TRADED: Dict[str, int] = {}

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

def _telegram_post(endpoint: str, data: Dict[str, Any], timeout: float = 3.0) -> bool:
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
        _telegram_post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message})
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
                        "🟢 BOT ACTIVADO\n\n"
                        "⚡ MULTIMERCADO | REVERSIÓN\n"
                        f"Cuenta: {ACCOUNT_TYPE}\n"
                        f"Entradas: 0/{MAX_TOTAL_TRADES}\n"
                        "📌 V3: rechazo N-2 + confirmación cerrada N-1 + entrada N + entrada N\n"
                        f"⏱ Temporalidad: {TIMEFRAME // 60} minuto(s)\n"
                        f"⏳ Expiración: {EXPIRATION} minuto(s)\n"
                        f"💵 Importe: {AMOUNT:g}"
                    )

                elif text == "/stop":
                    BOT_RUNNING = False
                    with STATE_LOCK:
                        PENDING_ENTRY.clear()
                    telegram_send("🔴 BOT DETENIDO\n\nNo se abrirán nuevas operaciones.")

                elif text == "/status":
                    status = "🟢 ACTIVO" if BOT_RUNNING else "🔴 DETENIDO"
                    telegram_send(
                        "📊 ESTADO\n\n"
                        f"Estado: {status}\n"
                        "Mercados: Binary/Turbo y Digital\n"
                        "Filtro: FUERZA V3\n"
                        "Entrada: rechazo N-2 + confirmación N-1\n"
                        f"Expiración: {EXPIRATION} minuto(s)\n"
                        f"Importe: {AMOUNT:g}\n"
                        f"Cuenta: {ACCOUNT_TYPE}\n"
                        f"Entradas: {TOTAL_TRADES}/{MAX_TOTAL_TRADES}\n"
                        f"Activos: {len(PAIRS)}\n"
                        "Filtro: primer vencimiento disponible de 1 minuto"
                    )

        except Exception as exc:
            logger.debug("Telegram commands: %s", exc)
            time.sleep(1)

# ============================================================
# UNIVERSO DE ACTIVOS
# ============================================================

def _supports_one_minute_expiration(info: Dict[str, Any]) -> bool:
    if not isinstance(info, dict):
        return False

    keys = {
        "expiration", "expirations", "expiration_period", "expiration_periods",
        "available_expirations", "available_durations", "duration", "durations",
        "duration_minutes", "duration_seconds", "expiration_minutes",
        "expiration_seconds", "binary_expirations", "binary_durations",
        "min_duration", "max_duration", "min_expiration", "max_expiration",
    }
    candidates: list[tuple[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in keys or "expiration" in normalized or "duration" in normalized:
                    candidates.append((normalized, value))
                walk(value)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(info)
    if not candidates:
        return False

    def one_minute(key: str, value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, dict):
            return any(one_minute(str(k).lower().replace("-", "_"), v) for k, v in value.items())
        if isinstance(value, (list, tuple, set)):
            return any(one_minute(key, item) for item in value)

        text = str(value).strip().lower()
        import re
        matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|s|m)?\b", text)
        if not matches and isinstance(value, (int, float)):
            matches = [(str(value), "")]

        for number_text, unit in matches:
            try:
                number = float(number_text)
            except (TypeError, ValueError):
                continue
            unit = (unit or "").lower()
            if unit in {"s", "sec", "secs", "second", "seconds"} or "second" in key:
                minutes = number / 60.0
            elif unit in {"m", "min", "mins", "minute", "minutes"} or "minute" in key:
                minutes = number
            elif "duration" in key or "expiration" in key:
                minutes = number if number == 1 else (number / 60.0 if number == 60 else -1)
            else:
                continue
            if abs(minutes - 1.0) < 1e-9:
                return True
        return False

    return any(one_minute(key, value) for key, value in candidates)


def _asset_name(info: Dict[str, Any]) -> Optional[str]:
    raw_name = info.get("name") or info.get("active_name") or info.get("symbol")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    name = raw_name.split(".", 1)[1] if "." in raw_name else raw_name
    return name.strip()


def _asset_is_open(info: Dict[str, Any]) -> bool:
    if not isinstance(info, dict):
        return False
    for key in ("enabled", "open", "is_open", "active", "tradable"):
        if key in info and info[key] is False:
            return False
    for key in ("is_suspended", "suspended", "closed"):
        if info.get(key) is True:
            return False
    return True


def _catalog_assets() -> Tuple[list[str], Dict[str, str], bool]:
    if IQ is None or not hasattr(IQ, "get_all_init_v2"):
        return [], {}, False
    try:
        data = IQ.get_all_init_v2()
    except Exception as exc:
        logger.warning("Catálogo de activos no disponible: %s", exc)
        return [], {}, False
    if not isinstance(data, dict):
        return [], {}, False

    root = data.get("result") if isinstance(data.get("result"), dict) else data
    names: list[str] = []
    markets: Dict[str, str] = {}

    for market_key, market_type in (("binary", "binary"), ("turbo", "binary"), ("digital", "digital")):
        market = root.get(market_key)
        if not isinstance(market, dict):
            continue
        actives = market.get("actives", market.get("assets", {}))
        if not isinstance(actives, dict):
            continue

        for active_id, info in actives.items():
            if not isinstance(info, dict) or not _asset_is_open(info):
                continue
            name = _asset_name(info)
            if not name or not _supports_one_minute_expiration(info):
                continue

            try:
                numeric_id = int(active_id)
            except (TypeError, ValueError):
                numeric_id = None

            if numeric_id is not None:
                try:
                    OP_code.ACTIVES[name] = numeric_id
                except Exception:
                    pass

            if name not in markets or (markets[name] != "digital" and market_type == "digital"):
                markets[name] = market_type
            if name not in names:
                names.append(name)

    return sorted(names), markets, True


def discover_available_pairs() -> list[str]:
    names, _, ok = _catalog_assets()
    return names if ok else []


def refresh_available_pairs(force: bool = False) -> list[str]:
    global PAIRS, PAIR_MARKET, LAST_PAIR_REFRESH
    now = time.time()
    if not force and now - LAST_PAIR_REFRESH < PAIR_REFRESH_SECONDS:
        return list(PAIRS)

    discovered, markets, ok = _catalog_assets_with_markets()
    if not ok:
        logger.warning("No se pudo actualizar el universo de activos")
        return list(PAIRS)

    selected = discovered if MAX_PAIRS <= 0 else discovered[:MAX_PAIRS]
    previous = set(PAIRS)
    current = set(selected)

    with STATE_LOCK:
        PAIRS = list(selected)
        PAIR_MARKET = {name: markets[name] for name in selected if name in markets}
        LAST_PAIR_REFRESH = now
        for pair in previous - current:
            PENDING_ENTRY.pop(pair, None)
            LIVE_STATE.pop(pair, None)
            LAST_TRADE_CANDLE.pop(pair, None)
            LAST_REJECTION_TRADED.pop(pair, None)

    if current != previous:
        by_market = {"binary": 0, "digital": 0}
        for pair in selected:
            by_market[PAIR_MARKET.get(pair, "binary")] = by_market.get(PAIR_MARKET.get(pair, "binary"), 0) + 1

        message = (
            "🔄 UNIVERSO DE ACTIVOS ACTUALIZADO\n\n"
            f"Activos válidos: {len(selected)}\n"
            f"Binary/Turbo: {by_market.get('binary', 0)}\n"
            f"Digital: {by_market.get('digital', 0)}\n"
            "Filtro: primer reloj de expiración disponible = 1 minuto\n"
            "Mercados: TODOS (OTC, REAL y DIGITAL) según disponibilidad"
        )
        logger.info(message.replace("\n", " | "))
        telegram_send(message)

    return list(PAIRS)


def _catalog_assets_with_markets() -> Tuple[list[str], Dict[str, str], bool]:
    return _catalog_assets()


def refresh_binary_otc_pairs(force: bool = False) -> list[str]:
    return refresh_available_pairs(force=force)

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

    refresh_available_pairs(force=True)

    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "📊 V3: rechazo N-2 + confirmación cerrada N-1 + entrada N\n"
        "⚡ V3: selección de la mejor señal entre los pares\n"
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

        refresh_available_pairs(force=True)
        telegram_send("🟢 IQ OPTION RECONECTADO")
        return True

    except Exception as exc:
        logger.error("Error de conexión: %s", exc)
        return False

# ============================================================
# VELAS
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

        df = pd.DataFrame(candles).rename(columns={"max": "high", "min": "low"})
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
    if IQ is None or order_id in (None, "", False):
        _study_write({"event": "result", "pair": pair, "order_id": order_id, "outcome": "unknown", "reason": "missing_order_id_or_iq"})
        return

    checker = getattr(IQ, "check_win_v4", None) or getattr(IQ, "check_win_v3", None)
    if checker is None:
        _study_write({"event": "result", "pair": pair, "order_id": order_id, "outcome": "unknown", "reason": "iqoptionapi_result_method_unavailable"})
        return

    deadline = time.time() + max(180, EXPIRATION * 60 + 60)
    while time.time() < deadline:
        try:
            raw_result = checker(order_id)
            if raw_result is not None and raw_result is not False and raw_result != "" and raw_result != "pending":
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

    _study_write({"event": "result", "pair": pair, "order_id": order_id, "outcome": "unknown", "reason": "result_timeout", "resolved_at": int(time.time())})

# ============================================================
# FILTRO DE ESTRATEGIA
# ============================================================

def is_force_signal(result: Dict[str, Any], signal: Any) -> bool:
    """Filtro final V3. Solo acepta señales completas y cerradas."""
    if signal not in ("call", "put"):
        return False

    try:
        score = int(result.get("score", 0) or 0)
    except (TypeError, ValueError):
        return False

    if score < MIN_ACCEPTED_SCORE:
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

    if str(analysis.get("strategy_version", "")) not in {"V3"}:
        return False

    # Confirmaciones estructurales obligatorias.
    pullback = analysis.get("pullback")
    if isinstance(pullback, dict):
        if pullback.get("valid") is False:
            return False
        if pullback.get("previous_candle_confirmed") is not True:
            return False
        if pullback.get("extreme_confirmed") is not True:
            return False

    if REQUIRE_STRUCTURE_BREAK:
        rejection = analysis.get("rejection_candle") or {}
        confirmation = analysis.get("confirmation_candle") or {}
        try:
            if signal == "call" and float(confirmation.get("close")) <= float(rejection.get("high")):
                return False
            if signal == "put" and float(confirmation.get("close")) >= float(rejection.get("low")):
                return False
        except (TypeError, ValueError):
            return False

    # Separacion Stochastic minima.
    try:
        sep = float(analysis.get("stochastic_separation", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if sep < MIN_STOCH_SEPARATION:
        return False

    # Espacio minimo hasta el nivel contrario.
    try:
        room_atr = float(analysis.get("room_atr", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if room_atr < MIN_ROOM_TO_OPPOSITE_ATR:
        return False

    return True


def revalidate_pending_location(pair: str, pending: Dict[str, Any]) -> bool:
    analysis = pending.get("analysis") or {}
    atr = float(analysis.get("atr") or 0.0)
    if atr <= 0.0 or IQ is None:
        return False

    last_high = analysis.get("last_swing_high") or analysis.get("resistance")
    last_low = analysis.get("last_swing_low") or analysis.get("support")

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
    opposite = level_value(last_high) if signal == "call" else level_value(last_low)
    if opposite is None:
        return False

    current = get_closed_candles(pair)
    if current is None or current.empty:
        return False

    # get_candles normalmente incluye la vela N en formacion como ultima fila.
    # Para ejecutar en el inicio de N, usamos su apertura/precio disponible;
    # si no esta disponible, caemos al ultimo cierre.
    execution_ts = int(pending.get("execution_ts") or 0)
    current_rows = current[current["from"].astype(int) == execution_ts]
    if not current_rows.empty:
        row = current_rows.iloc[-1]
        current_price = float(row["open"])
    else:
        current_price = float(current.iloc[-1]["close"])
    room = opposite - current_price if signal == "call" else current_price - opposite
    room_atr = room / atr
    valid = room_atr >= MIN_ROOM_TO_OPPOSITE_ATR

    logger.info("%s | revalidacion N | signal=%s | room=%.2f ATR | valido=%s", pair, signal, room_atr, valid)
    return valid

# ============================================================
# DIAGNOSTICO
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
        f"{label}: O={_fmt_price(candle.get('open'))} | "
        f"H={_fmt_price(candle.get('high'))} | "
        f"L={_fmt_price(candle.get('low'))} | "
        f"C={_fmt_price(candle.get('close'))} | "
        f"Rango={_fmt_price(candle.get('range'))} | "
        f"Cuerpo={_fmt_price(candle.get('body'))} | "
        f"MechaSup={_fmt_price(candle.get('upper'))} | "
        f"MechaInf={_fmt_price(candle.get('lower'))} | "
        f"PosCierre={_fmt_price(candle.get('close_position'))}"
    )


def _diagnostic_message(pair: str, signal: str, result: Dict[str, Any], rejection_label: str = "RECHAZO N-2", confirmation_label: str = "CONFIRMACIÓN N-1") -> str:
    analysis = result.get("analysis") or {}
    rejection = analysis.get("rejection_candle") or {}
    confirmation = analysis.get("confirmation_candle") or {}
    return (
        "📊 DIAGNÓSTICO TÉCNICO\n\n"
        f"Par: {pair}\n"
        f"Dirección: {str(signal).upper()}\n"
        f"Score: {result.get('score', 0)}/100\n"
        f"Calidad: {result.get('entry_quality', 0)}/100\n"
        f"Estructura: {analysis.get('structure', 'unknown')}\n"
        f"ATR: {_fmt_price(analysis.get('atr'))}\n"
        f"Soporte: {_fmt_price(analysis.get('support'))}\n"
        f"Resistencia: {_fmt_price(analysis.get('resistance'))}\n"
        f"Tolerancia: {_fmt_price(analysis.get('tolerance'))}\n\n"
        f"{_candle_diagnostic(rejection_label, rejection)}\n"
        f"{_candle_diagnostic(confirmation_label, confirmation)}\n\n"
        f"Timestamp N-2: {analysis.get('rejection_timestamp', 'N/D')}\n"
        f"Timestamp N-1: {analysis.get('confirmation_timestamp', 'N/D')}\n\n"
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

# ============================================================
# ANALISIS
# ============================================================

def analyze_closed_setup(
    pair: str,
    confirmation_ts: int,
) -> Optional[Dict[str, Any]]:
    """Analiza exclusivamente N-1 ya cerrada.

    N-2 = vela de rechazo.
    N-1 = confirmacion cerrada.
    La siguiente vela (N) es donde se ejecuta.
    """
    if trade_limit_reached() or IQ is None:
        return None

    df = get_closed_candles(pair)
    if df is None or df.empty:
        return None

    df = (
        df.sort_values("from")
        .drop_duplicates("from", keep="last")
        .reset_index(drop=True)
    )

    rows = df[df["from"].astype(int) == int(confirmation_ts)]
    if rows.empty:
        return None

    confirmation_row = rows.iloc[-1]
    history = df[df["from"].astype(int) < int(confirmation_ts)].copy()
    if len(history) < MIN_HISTORY - 1:
        return None

    result = analyze_market(
        candle_1m=confirmation_row.to_dict(),
        previous_m1=history,
        pair=pair,
    )
    signal = result.get("signal")

    if not is_force_signal(result, signal):
        return None

    analysis = result.get("analysis") or {}
    rejection_ts = analysis.get("rejection_timestamp")

    # No repetir la misma vela de rechazo en el mismo par.
    if REQUIRE_NEW_REJECTION and rejection_ts is not None:
        try:
            if LAST_REJECTION_TRADED.get(pair) == int(rejection_ts):
                return None
        except (TypeError, ValueError):
            return None

    candidate = {
        "pair": pair,
        "confirmation_ts": int(confirmation_ts),
        "execution_ts": int(confirmation_ts + TIMEFRAME),
        "result": result,
        "signal": signal,
        "score": int(result.get("score", 0) or 0),
        "analysis": analysis,
        "rejection_ts": int(rejection_ts) if rejection_ts is not None else None,
    }
    return candidate


def analyze_live_candle(
    pair: str,
    current_ts: int,
    execute: bool = True,
) -> Optional[Dict[str, Any]]:
    """Compatibilidad: current_ts es ahora el inicio de N.

    Se analiza confirmation_ts=current_ts-TIMEFRAME, que ya esta cerrada.
    """
    confirmation_ts = int(current_ts) - TIMEFRAME
    candidate = analyze_closed_setup(pair, confirmation_ts)
    if candidate is None or not execute:
        return candidate

    if LAST_TRADE_CANDLE.get(pair) == int(current_ts):
        return None
    if cooldown_active(pair):
        return None

    # Revalidacion final del espacio antes de ejecutar N.
    pending = {
        "pair": pair,
        "signal": candidate["signal"],
        "analysis": candidate["analysis"],
        "execution_ts": int(current_ts),
        "continuity_ts": int(candidate["confirmation_ts"]),
        "rejection_ts": candidate.get("rejection_ts"),
        "score": candidate["score"],
        "entry_type": candidate["result"].get("entry_type"),
        "reason": candidate["result"].get("reason", ""),
        "study_candles": [],
        "created_at": time.time(),
        "close": (candidate["analysis"].get("confirmation_candle") or {}).get("close"),
    }

    if not revalidate_pending_location(pair, pending):
        logger.info("%s | V3 descartada antes de N por espacio insuficiente", pair)
        return None

    ok, order_id = buy_binary(pair, candidate["signal"])
    if not ok:
        logger.warning("%s | orden rechazada | señal=%s", pair, candidate["signal"])
        return None

    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = int(current_ts)
    if candidate.get("rejection_ts") is not None:
        LAST_REJECTION_TRADED[pair] = int(candidate["rejection_ts"])

    result = candidate["result"]
    analysis = candidate["analysis"]
    event = {
        "event": "entry",
        "pair": pair,
        "order_id": order_id,
        "signal": candidate["signal"],
        "entry_timestamp": int(current_ts),
        "analysis_timestamp": int(candidate["confirmation_ts"]),
        "rejection_timestamp": candidate.get("rejection_ts"),
        "score": candidate["score"],
        "entry_type": result.get("entry_type"),
        "structure": analysis.get("structure"),
        "reason": result.get("reason", ""),
        "analysis": analysis,
        "created_at": pending["created_at"],
        "recorded_at": int(time.time()),
    }
    _study_write(event)

    telegram_send(
        "✅ ENTRADA V3 EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {str(candidate['signal']).upper()}\n"
        f"Score: {candidate['score']}/100\n"
        f"STOCH K: {analysis.get('stochastic_k')}\n"
        f"STOCH D: {analysis.get('stochastic_d')}\n"
        f"Separación: {analysis.get('stochastic_separation')}\n"
        f"Espacio: {analysis.get('room_atr')} ATR\n"
        f"Rechazo N-2: {candidate.get('rejection_ts')}\n"
        f"Confirmación N-1: {candidate['confirmation_ts']}\n"
        f"Entrada N: {current_ts}\n"
        f"ID: {order_id}\n\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n\n"
        f"{result.get('reason', '')}"
    )

    logger.info(
        "%s | V3 EJECUTADA | %s | score=%s | N-2=%s | N-1=%s | N=%s | ID=%s",
        pair,
        str(candidate["signal"]).upper(),
        candidate["score"],
        candidate.get("rejection_ts"),
        candidate["confirmation_ts"],
        current_ts,
        order_id,
    )
    candidate["order_id"] = order_id
    candidate["execution_ts"] = int(current_ts)
    return candidate

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

    market = PAIR_MARKET.get(pair, "binary")
    with STATE_LOCK:
        if TOTAL_TRADES >= MAX_TOTAL_TRADES:
            return False, None
        try:
            if market == "digital" and hasattr(IQ, "buy_digital_spot"):
                result = IQ.buy_digital_spot(pair, AMOUNT, signal, EXPIRATION)
            else:
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
            logger.error("%s | %s | error de orden: %s", pair, market, exc)
            return False, None

    logger.info("%s | mercado=%s | ENTRADA %s/%s | cuenta=%s", pair, market, current_trades, MAX_TOTAL_TRADES, ACCOUNT_TYPE)
    if current_trades >= MAX_TOTAL_TRADES:
        telegram_send(
            "🛑 LÍMITE DE ENTRADAS ALCANZADO\n\n"
            f"Entradas ejecutadas: {current_trades}/{MAX_TOTAL_TRADES}\n"
            f"Cuenta: {ACCOUNT_TYPE}"
        )
    return True, order_id


# ============================================================
# MOTOR
# ============================================================

def process_pair(
    pair: str,
    current_ts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if IQ is None:
        return None
    if current_ts is None:
        current_ts = floor_candle_timestamp(get_iq_server_timestamp())
    return analyze_closed_setup(pair, int(current_ts) - TIMEFRAME)


def analyze_all_pairs() -> None:
    if not BOT_RUNNING or trade_limit_reached():
        return

    refresh_available_pairs()

    # current_ts = inicio de la vela N. Por tanto N-1 ya esta cerrada.
    current_ts = int(floor_candle_timestamp(get_iq_server_timestamp()))
    confirmation_ts = current_ts - TIMEFRAME
    candidates: list[Dict[str, Any]] = []

    for pair in list(PAIRS)[:MAX_PAIRS]:
        if not BOT_RUNNING or trade_limit_reached():
            return
        try:
            candidate = process_pair(pair, current_ts)
            if candidate:
                candidates.append(candidate)
        except Exception:
            logger.exception("Error procesando %s", pair)

    if not candidates:
        return

    # Primero score; en empate, mayor separacion y mayor espacio.
    def rank(item: Dict[str, Any]) -> tuple[float, float, float]:
        analysis = item.get("analysis") or {}
        return (
            float(item.get("score", 0) or 0),
            float(analysis.get("stochastic_separation", 0) or 0),
            float(analysis.get("room_atr", 0) or 0),
        )

    candidates.sort(key=rank, reverse=True)
    best = candidates[0]
    best_pair = str(best["pair"])

    logger.info(
        "MEJOR CANDIDATO V3 | par=%s | score=%s | stoch_sep=%.2f | room=%.2f ATR | señal=%s | candidatos=%s",
        best_pair,
        best.get("score", 0),
        float((best.get("analysis") or {}).get("stochastic_separation", 0) or 0),
        float((best.get("analysis") or {}).get("room_atr", 0) or 0),
        str(best.get("signal", "")).upper(),
        len(candidates),
    )

    # Ejecutar solo al comienzo de N. Si la vela N ya avanzo, no trasladamos
    # la señal a otra vela.
    if floor_candle_timestamp(get_iq_server_timestamp()) != current_ts:
        return

    if LAST_TRADE_CANDLE.get(best_pair) == current_ts or cooldown_active(best_pair):
        return

    analyze_live_candle(best_pair, current_ts, execute=True)

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    global BOT_RUNNING, TOTAL_TRADES

    logger.info("========================================")
    logger.info("BOT MULTIMERCADO | V3 | N-2 RECHAZO + N-1 CERRADO + N")
    logger.info("TIMEFRAME=%s | EXPIRATION=%s (SOLO 1 MINUTO)", TIMEFRAME, EXPIRATION)
    logger.info("MAX PAIRS=%s | REFRESH=%ss | AMOUNT=%s | ACCOUNT=%s | MAX_TRADES=%s",
                MAX_PAIRS, int(PAIR_REFRESH_SECONDS), AMOUNT, ACCOUNT_TYPE, MAX_TOTAL_TRADES)
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

    with STATE_LOCK:
        TOTAL_TRADES = 0
        PENDING_ENTRY.clear()

    BOT_RUNNING = AUTO_START

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "📊 V3: N-2 rechazo + N-1 cerrado + N ejecución\n"
        "⏱ Solo pares con expiración de 1 minuto\n"
        "⚡ V3: selección de la mejor señal entre hasta 50 pares\n"
        f"⏳ Expiración: {EXPIRATION} minuto(s)\n"
        f"🔢 Activos analizados: {len(PAIRS)} (máximo {MAX_PAIRS})\n"
        f"🔄 Actualización de pares: cada {int(PAIR_REFRESH_SECONDS // 60)} minutos\n"
        f"🚀 Inicio automático: {'SI' if AUTO_START else 'NO'}\n\n"
        + ("🟢 Análisis automático activado." if AUTO_START else "Usa /start para activar.")
    )

    logger.info("Motor de análisis: %s", "ACTIVO AUTOMÁTICAMENTE" if AUTO_START else "ESPERANDO /start")

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
