"""bot.py - OTC Binary, un solo par por señal.

- Descubre pares OTC Binary disponibles.
- Actualiza el universo cada 10 minutos.
- En cada cierre de vela M1 analiza TODOS los pares disponibles.
- Selecciona como maximo UN par con la senal mas fuerte.
- Ejecuta la entrada justo al abrir la siguiente vela M1.
- Invierte la señal: CALL -> PUT y PUT -> CALL.
- Expiracion Binary: 1 minuto.
- La estrategia esta en strategy.py y NO usa indicadores.
"""
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

from strategy import analyze_market

# ============================================================
# COMPATIBILIDAD IQOPTIONAPI - SOLO BINARY OTC
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
# CONFIGURACION
# ============================================================
IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

M1_TIMEFRAME = 60
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "500"))
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "250"))
PAIR_REFRESH_SECONDS = 600.0
POLL_SECONDS = 0.05
TRADE_COOLDOWN = 60.0

# Se descubren todos los OTC disponibles, pero SOLO se puede elegir un par
# para operar por cada cierre M1.
PAIRS: list[str] = []
LAST_PAIR_REFRESH = 0.0
LAST_ANALYZED_M1 = -1
LAST_TRADE_M1 = -1
LAST_TRADE_TIME = 0.0
PENDING_CANDIDATE: Optional[Dict[str, Any]] = None
BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
STATE_LOCK = threading.RLock()
LAST_CANDIDATE: Optional[Dict[str, Any]] = None

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
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}",
            data=data,
            timeout=timeout,
        )
        return r.status_code == 200
    except Exception as exc:
        logger.debug("Telegram %s: %s", endpoint, exc)
        return False


def telegram_send(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    threading.Thread(
        target=_telegram_post,
        args=("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message}),
        kwargs={"timeout": 3.0},
        daemon=True,
    ).start()


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
            data = requests.get(url, params=params, timeout=3).json()
            if not data.get("ok"):
                time.sleep(0.5)
                continue
            for update in data.get("result", []):
                uid = update.get("update_id")
                if uid is not None:
                    last_update_id = int(uid)
                msg = update.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue
                text = str(msg.get("text", "")).strip().lower()
                if text == "/start":
                    BOT_RUNNING = True
                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "🧠 ACCION DEL PRECIO M1\n"
                        "🔎 Analiza todos los OTC disponibles\n"
                        "🎯 Opera solo 1 par por cierre M1\n"
                        "⚡ Entrada: apertura de la siguiente M1\n"
                        "🔄 Señal invertida: CALL↔PUT\n"
                        "⏳ Expiracion: 1 minuto\n"
                        "🚫 Sin indicadores"
                    )
                elif text == "/stop":
                    BOT_RUNNING = False
                    telegram_send("🔴 BOT DETENIDO\n\nNo se abriran nuevas operaciones.")
                elif text == "/status":
                    status = "🟢 ACTIVO" if BOT_RUNNING else "🔴 DETENIDO"
                    telegram_send(
                        "📊 ESTADO\n\n"
                        f"Estado: {status}\n"
                        "Analisis: M1\n"
                        "Entrada: apertura de la siguiente M1\n"
                        "Señal: invertida CALL↔PUT\n"
                        "Expiracion: 1 minuto\n"
                        f"OTC disponibles: {len(PAIRS)}\n"
                        f"Importe: {AMOUNT:g}\n"
                        "Indicadores: NO"
                    )
        except Exception as exc:
            logger.debug("Telegram command loop: %s", exc)
            time.sleep(1)

# ============================================================
# OTC DISPONIBLES
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
        logger.warning("Catalogo Binary no disponible: %s", exc)
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
            OP_code.ACTIVES[name] = int(active_id)
        except (TypeError, ValueError):
            continue
        pairs.append(name)
    return sorted(set(pairs)), True


def discover_binary_otc_pairs() -> list[str]:
    pairs, ok = _load_binary_otc_catalog()
    return pairs if ok else []


def refresh_binary_otc_pairs(force: bool = False) -> list[str]:
    global PAIRS, LAST_PAIR_REFRESH
    now = time.time()
    if not force and now - LAST_PAIR_REFRESH < PAIR_REFRESH_SECONDS:
        return list(PAIRS)

    discovered = discover_binary_otc_pairs()
    if not discovered:
        # Si el catalogo falla, conservar el ultimo universo valido.
        logger.warning("No se pudo actualizar OTC; se conserva la lista anterior: %s", PAIRS)
        LAST_PAIR_REFRESH = now
        return list(PAIRS)

    previous = set(PAIRS)
    PAIRS = list(discovered)
    LAST_PAIR_REFRESH = now

    if set(PAIRS) != previous:
        text = ", ".join(PAIRS) if PAIRS else "NINGUNO"
        telegram_send(
            "🔄 OTC DISPONIBLES ACTUALIZADOS\n\n"
            f"Pares encontrados: {len(PAIRS)}\n"
            "Proxima actualizacion: 10 minutos\n\n"
            f"{text}"
        )
        logger.info("OTC disponibles: %d | %s", len(PAIRS), text)
    return list(PAIRS)

# ============================================================
# RELOJ Y CONEXION
# ============================================================
def get_iq_server_timestamp() -> float:
    if IQ is not None:
        try:
            value = float(IQ.get_server_timestamp())
            if value > 0:
                return value
        except Exception:
            pass
    return time.time()


def floor_m1(ts: float) -> int:
    return int(ts // M1_TIMEFRAME) * M1_TIMEFRAME


def last_closed_m5_timestamp(current_m1_open: int) -> int:
    # Solo existe un nuevo cierre M1 en 00, 05, 10, 15, etc.
    return current_m1_open - M1_TIMEFRAME


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
    logger.info("IQ conectado | server=%.3f", get_iq_server_timestamp())
    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "🧠 Analisis M1 por accion del precio\n"
        "⚡ Entrada: apertura de la siguiente M1\n"
        "🔄 Señal invertida CALL↔PUT\n"
        "⏳ Expiracion: 1 minuto"
    )
    return True


def ensure_connection() -> bool:
    global IQ
    if IQ is None:
        return connect_iq()
    try:
        if hasattr(IQ, "check_connect") and IQ.check_connect():
            return True
    except Exception:
        pass
    try:
        connected, _ = IQ.connect()
        if connected:
            refresh_binary_otc_pairs(force=True)
            return True
    except Exception as exc:
        logger.warning("Reconexión fallida: %s", exc)
    return False

# ============================================================
# DATOS M1
# ============================================================
def get_m1_candles(pair: str) -> Optional[pd.DataFrame]:
    if IQ is None:
        return None
    try:
        candles = IQ.get_candles(
            pair,
            M1_TIMEFRAME,
            CANDLE_COUNT_M1,
            get_iq_server_timestamp(),
        )
        if not candles:
            return None
        df = pd.DataFrame(candles).rename(columns={"max": "high", "min": "low"})
        required = ["from", "open", "high", "low", "close"]
        if any(c not in df.columns for c in required):
            return None
        for c in required:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=required).copy()
        df["from"] = df["from"].astype(int)
        return (
            df.drop_duplicates("from", keep="last")
              .sort_values("from")
              .reset_index(drop=True)
        )
    except Exception as exc:
        logger.debug("%s | M1: %s", pair, exc)
        return None


# ============================================================
# ANALISIS Y SELECCION DE UN SOLO PAR
# ============================================================
def _candidate_strength(c: Dict[str, Any]) -> tuple:
    a = c.get("analysis", {}) or {}
    return (
        int(c.get("score", 0)),
        1 if a.get("call_rejection") or a.get("put_rejection") else 0,
        1 if a.get("call_momentum") or a.get("put_momentum") else 0,
        1 if a.get("pullback") else 0,
        1 if a.get("reversal") else 0,
        1 if a.get("continuation") else 0,
        1 if a.get("strength") else 0,
    )


def analyze_pair_at_m1_close(pair: str, closed_m1_ts: int) -> Optional[Dict[str, Any]]:
    m1 = get_m1_candles(pair)
    if m1 is None or m1.empty:
        return None

    # Solo velas COMPLETAMENTE cerradas. La vela que acaba de abrir no participa.
    history = m1[m1["from"].astype(int) <= int(closed_m1_ts)].copy()
    if history.empty:
        return None

    row = history[history["from"].astype(int) == int(closed_m1_ts)]
    if row.empty:
        return None

    current = row.iloc[-1]
    result = analyze_market(
        df=history,
        candle_1m=current.to_dict(),
        previous_m1=history.iloc[:-1].copy(),
        pair=pair,
    )

    signal = result.get("signal")
    if signal not in ("call", "put"):
        logger.info(
            "%s | M1 %s | SIN OPERACION | %s",
            pair,
            closed_m1_ts,
            result.get("reason", ""),
        )
        return None

    return {
        "pair": pair,
        "signal": signal,
        "score": int(result.get("score", 0)),
        "m1_ts": int(closed_m1_ts),
        "execution_m1_ts": int(closed_m1_ts + M1_TIMEFRAME),
        "reason": result.get("reason", ""),
        "analysis": result.get("analysis", {}) or {},
    }


def analyze_all_pairs_at_m1_close(closed_m1_ts: int) -> Optional[Dict[str, Any]]:
    candidates: list[Dict[str, Any]] = []

    for pair in list(PAIRS):
        if not BOT_RUNNING:
            return None
        try:
            candidate = analyze_pair_at_m1_close(pair, closed_m1_ts)
            if candidate is not None:
                candidates.append(candidate)
        except Exception:
            logger.exception("Error analizando %s", pair)

    if not candidates:
        telegram_send(
            "⏸️ CIERRE M1 SIN ENTRADA\n\n"
            "Ningun OTC disponible cumplio las condiciones de accion del precio."
        )
        return None

    # Una sola operación: se elige la señal con mayor calidad de acción del precio.
    best = max(candidates, key=_candidate_strength)

    telegram_send(
        "🎯 SEÑAL M1 CONFIRMADA\n\n"
        f"Par elegido: {best['pair']}\n"
        f"Dirección original: {best['signal'].upper()}\n"
        f"Score: {best['score']}/100\n"
        f"M1 cerrada: {best['m1_ts']}\n"
        "⚡ Entrada: apertura de la siguiente M1\n"
        "⏳ Expiración: 1 minuto\n\n"
        f"{best['reason']}"
    )
    return best

# ============================================================
# EJECUCION
# ============================================================
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


def execute_candidate(candidate: Dict[str, Any], current_ts: float) -> bool:
    """Ejecuta la señal invertida en la apertura de la M1 objetivo."""
    global LAST_TRADE_M1, LAST_TRADE_TIME

    target_ts = int(candidate["execution_m1_ts"])

    # Solo se permite la apertura de la vela objetivo. Nunca se entra tarde.
    # La ventana es muy corta para absorber el polling sin mover la entrada.
    if not (target_ts <= float(current_ts) < target_ts + 0.35):
        return False

    target_m1 = target_ts
    if LAST_TRADE_M1 == target_m1:
        return False
    if time.time() - LAST_TRADE_TIME < TRADE_COOLDOWN:
        return False

    pair = str(candidate["pair"])
    original_signal = str(candidate["signal"]).lower()

    # Mantener la inversión solicitada: CALL -> PUT / PUT -> CALL.
    signal = "put" if original_signal == "call" else "call"

    ok, order_id = buy_binary(pair, signal)
    if not ok:
        telegram_send(
            "❌ ORDEN RECHAZADA\n\n"
            f"Par: {pair}\n"
            f"Señal original: {original_signal.upper()}\n"
            f"Entrada invertida: {signal.upper()}\n"
            "La señal no se trasladara a otra vela."
        )
        return False

    LAST_TRADE_M1 = target_m1
    LAST_TRADE_TIME = time.time()

    telegram_send(
        "⚡ ENTRADA EJECUTADA EN APERTURA M1\n\n"
        f"Par: {pair}\n"
        f"Señal original: {original_signal.upper()}\n"
        f"Entrada INVERTIDA: {signal.upper()}\n"
        f"M1 analizada: {candidate['m1_ts']}\n"
        f"M1 ejecutada: {target_m1}\n"
        "⚡ Ejecución: segundo 00 / apertura M1\n"
        f"ID: {order_id}\n"
        "⏳ Expiración: 1 minuto"
    )

    logger.info(
        "%s | ORIGINAL=%s | INVERTIDA=%s | M1_ANALIZADA=%s | M1_EJECUTADA=%s | apertura=00 | ID=%s",
        pair,
        original_signal.upper(),
        signal.upper(),
        candidate["m1_ts"],
        target_m1,
        order_id,
    )
    return True

# ============================================================
# CICLO PRINCIPAL
# ============================================================
def process_cycle() -> None:
    """
    1) Detecta el cierre de cada M1 usando el reloj del servidor.
    2) Analiza TODOS los OTC disponibles con la vela M1 cerrada.
    3) Elige SOLO un par.
    4) Guarda la señal para la M1 siguiente.
    5) Ejecuta la señal INVERTIDA justo en la apertura de esa M1.
    """
    global LAST_ANALYZED_M1, LAST_CANDIDATE, PENDING_CANDIDATE

    if not BOT_RUNNING:
        return

    refresh_binary_otc_pairs()
    if not PAIRS:
        return

    server_ts = get_iq_server_timestamp()
    current_m1_open = floor_m1(server_ts)

    # 1) EJECUTAR CANDIDATO PENDIENTE EN LA APERTURA DE LA M1.
    if PENDING_CANDIDATE is not None:
        if execute_candidate(PENDING_CANDIDATE, server_ts):
            PENDING_CANDIDATE = None
            LAST_CANDIDATE = None
        elif server_ts >= int(PENDING_CANDIDATE["execution_m1_ts"]) + 1:
            # Si no pudo ejecutarse prácticamente en la apertura, se cancela.
            logger.warning(
                "Entrada perdida: %s M1=%s server=%.3f",
                PENDING_CANDIDATE.get("pair"),
                PENDING_CANDIDATE.get("execution_m1_ts"),
                server_ts,
            )
            telegram_send(
                "⚠️ ENTRADA PERDIDA\n\n"
                f"Par: {PENDING_CANDIDATE.get('pair')}\n"
                "No se ejecutara tarde."
            )
            PENDING_CANDIDATE = None
            LAST_CANDIDATE = None

    # 2) Solo analizar cuando acaba de cerrar una M1.
    # Si estamos dentro de una M1, no se vuelve a analizar.
    closed_m1_ts = current_m1_open - M1_TIMEFRAME
    if closed_m1_ts == LAST_ANALYZED_M1:
        return

    # No preparar una segunda entrada mientras haya una pendiente.
    if PENDING_CANDIDATE is not None:
        return

    LAST_ANALYZED_M1 = closed_m1_ts
    candidate = analyze_all_pairs_at_m1_close(closed_m1_ts)
    LAST_CANDIDATE = candidate

    if candidate is not None:
        candidate = dict(candidate)
        # La vela recién abierta es exactamente la siguiente M1.
        candidate["execution_m1_ts"] = int(current_m1_open)
        candidate["execution_second"] = 0
        PENDING_CANDIDATE = candidate

        telegram_send(
            "⏳ ENTRADA PROGRAMADA\n\n"
            f"Par: {candidate['pair']}\n"
            f"Señal original: {candidate['signal'].upper()}\n"
            f"Entrada INVERTIDA: {'PUT' if candidate['signal'] == 'call' else 'CALL'}\n"
            f"M1 cerrada: {candidate['m1_ts']}\n"
            f"M1 objetivo: {candidate['execution_m1_ts']}\n"
            "⚡ Ejecución: apertura / segundo 00\n"
            "⏳ Expiración: 1 minuto"
        )


def main() -> None:
    global BOT_RUNNING
    logger.info("========================================")
    logger.info("BOT BINARY OTC - ACCION DEL PRECIO")
    logger.info("Analisis M1 | Entrada apertura de la siguiente M1 | Expiracion 1 minuto")
    logger.info("Opera SOLO UN par por cada cierre M1")
    logger.info("Sin EMA | Sin RSI | Sin ATR | Sin indicadores")
    logger.info("========================================")

    required = {
        "IQ_EMAIL": IQ_EMAIL,
        "IQ_PASSWORD": IQ_PASSWORD,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }
    missing = [k for k, v in required.items() if not v]
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
        "📊 Analiza TODOS los OTC disponibles en M1.\n"
        "🎯 Selecciona SOLO 1 par.\n"
        "⚡ Opera al abrir la siguiente M1.\n"
        "⏳ Expiración: 1 minuto.\n"
        "🚫 Sin indicadores.\n\n"
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
            process_cycle()
            time.sleep(POLL_SECONDS)
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
