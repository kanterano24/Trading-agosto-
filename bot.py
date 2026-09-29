"""bot.py - OTC Binary, un solo par por señal.

- Descubre pares OTC Binary disponibles.
- Actualiza el universo cada 10 minutos.
- En cada cierre de vela M5 analiza TODOS los pares disponibles.
- Selecciona como maximo UN par con la senal mas fuerte.
- Ejecuta inmediatamente al abrir la siguiente vela M1.
- Expiracion Binary: 1 minuto.
- La estrategia esta en strategy.py y NO usa indicadores.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
M5_TIMEFRAME = 300
EXPIRATION = 1
AMOUNT = float(os.getenv("AMOUNT", "500"))
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "250"))
PAIR_REFRESH_SECONDS = 600.0
POLL_SECONDS = 0.05
TRADE_COOLDOWN = 60.0
ANALYSIS_WORKERS = 8
ENTRY_MAX_DELAY_SECONDS = 0.80

# Se descubren todos los OTC disponibles, pero SOLO se puede elegir un par
# para operar por cada cierre M5.
PAIRS: list[str] = []
LAST_PAIR_REFRESH = 0.0
LAST_ANALYZED_M5 = -1
LAST_TRADE_M1 = -1
LAST_TRADE_TIME = 0.0
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
                        "🧠 ACCION DEL PRECIO M5\n"
                        "🔎 Analiza todos los OTC disponibles\n"
                        "🎯 Opera solo 1 par por cierre M5\n"
                        "⚡ Entrada: apertura de la siguiente M1\n"
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
                        "Analisis: M5\n"
                        "Entrada: apertura M1 siguiente\n"
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
# DIAGNOSTICO TEMPORAL DE CONEXION
# ============================================================
def print_connection_diagnostics() -> None:
    """Muestra solo metadatos no sensibles de la conexión actual."""
    print("\n" + "=" * 70)
    print("DIAGNOSTICO CONEXION IQ OPTION")
    print("=" * 70)
    try:
        api = getattr(IQ, "api", None)
        print(
            "IQ.api: "
            f"{type(api).__name__ if api is not None else 'None'}"
        )
        if api is None:
            print("No se encontró IQ.api")
            print("=" * 70)
            return

        for attr in (
            "https_url",
            "ws_url",
            "host",
            "platform_id",
            "platformId",
            "platform",
        ):
            try:
                value = getattr(api, attr, None)
                if value is not None:
                    print(f"{attr}: {value}")
            except Exception as exc:
                print(f"{attr}: ERROR {exc}")

        print("\nAtributos relacionados encontrados:")
        found = []
        for name in dir(api):
            low = str(name).lower()
            if any(
                word in low
                for word in (
                    "platform",
                    "websocket",
                    "ws_url",
                    "https_url",
                    "host",
                )
            ):
                found.append(str(name))
        for name in sorted(set(found)):
            print(f"  - {name}")
        if not found:
            print("  Ninguno")

        print("\nAtributos relacionados en IQ_Option:")
        iq_found = []
        if IQ is not None:
            for name in dir(IQ):
                low = str(name).lower()
                if any(
                    word in low
                    for word in (
                        "platform",
                        "websocket",
                        "ws_url",
                        "https_url",
                        "host",
                    )
                ):
                    iq_found.append(str(name))
        for name in sorted(set(iq_found)):
            print(f"  - {name}")
        if not iq_found:
            print("  Ninguno")
    except Exception as exc:
        print(f"[DIAGNOSTICO] Error general: {exc}")
    print("=" * 70)
    print("FIN DIAGNOSTICO\n")


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
    # Solo existe un nuevo cierre M5 en 00, 05, 10, 15, etc.
    return current_m1_open - M5_TIMEFRAME


def connect_iq() -> bool:
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise ValueError("Faltan IQ_EMAIL/IQ_PASSWORD")
    logger.info("Conectando a IQ Option...")
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = IQ.connect()
    if not connected:
        raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")
    print_connection_diagnostics()
    refresh_binary_otc_pairs(force=True)
    logger.info("IQ conectado | server=%.3f", get_iq_server_timestamp())
    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "🧠 Analisis M5 por accion del precio\n"
        "⚡ Entrada inmediata en apertura M1\n"
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
# DATOS M1 -> M5
# ============================================================
def get_m1_candles(pair: str) -> Optional[pd.DataFrame]:
    if IQ is None:
        return None
    try:
        candles = IQ.get_candles(pair, M1_TIMEFRAME, CANDLE_COUNT_M1, get_iq_server_timestamp())
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
        return df.drop_duplicates("from", keep="last").sort_values("from").reset_index(drop=True)
    except Exception as exc:
        logger.debug("%s | M1: %s", pair, exc)
        return None


def aggregate_m1_to_m5(m1: pd.DataFrame, closed_m5_ts: int) -> pd.DataFrame:
    if m1 is None or m1.empty:
        return pd.DataFrame()
    work = m1.copy()
    work = work[work["from"].astype(int) <= int(closed_m5_ts + M5_TIMEFRAME - M1_TIMEFRAME)]
    work["block"] = (work["from"].astype(int) // M5_TIMEFRAME) * M5_TIMEFRAME
    blocks = []
    for block_ts, group in work.groupby("block", sort=True):
        group = group.sort_values("from").copy()
        expected = [int(block_ts) + i * M1_TIMEFRAME for i in range(5)]
        actual = group["from"].astype(int).tolist()
        if actual != expected:
            continue
        blocks.append({
            "from": int(block_ts),
            "open": float(group.iloc[0]["open"]),
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "close": float(group.iloc[-1]["close"]),
        })
    if not blocks:
        return pd.DataFrame(columns=["from", "open", "high", "low", "close"])
    out = pd.DataFrame(blocks).sort_values("from").reset_index(drop=True)
    return out[out["from"] <= int(closed_m5_ts)].reset_index(drop=True)

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
    )


def analyze_pair_at_m5_close(pair: str, closed_m5_ts: int) -> Optional[Dict[str, Any]]:
    m1 = get_m1_candles(pair)
    if m1 is None or m1.empty:
        return None
    m5 = aggregate_m1_to_m5(m1, closed_m5_ts)
    if m5.empty:
        return None
    row = m5[m5["from"].astype(int) == int(closed_m5_ts)]
    if row.empty:
        return None
    current = row.iloc[-1]
    history = m5[m5["from"].astype(int) <= int(closed_m5_ts)].copy()
    result = analyze_market(df=history, candle_5m=current.to_dict(), pair=pair)
    signal = result.get("signal")
    if signal not in ("call", "put"):
        logger.info("%s | M5 %s | SIN OPERACION | %s", pair, closed_m5_ts, result.get("reason", ""))
        return None
    return {
        "pair": pair,
        "signal": signal,
        "score": int(result.get("score", 0)),
        "m5_ts": int(closed_m5_ts),
        "execution_m1_ts": int(closed_m5_ts + M5_TIMEFRAME),
        "reason": result.get("reason", ""),
        "analysis": result.get("analysis", {}) or {},
    }


def analyze_all_pairs_at_m5_close(closed_m5_ts: int) -> Optional[Dict[str, Any]]:
    """Analiza todos los OTC en paralelo para reducir la latencia de N+1."""
    candidates: list[Dict[str, Any]] = []
    pairs = list(PAIRS)

    if not pairs:
        return None

    def _worker(pair: str) -> Optional[Dict[str, Any]]:
        try:
            return analyze_pair_at_m5_close(pair, closed_m5_ts)
        except Exception:
            logger.exception("Error analizando %s", pair)
            return None

    workers = max(1, min(ANALYSIS_WORKERS, len(pairs)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_worker, pair): pair for pair in pairs}
        for future in as_completed(futures):
            if not BOT_RUNNING:
                return None
            try:
                candidate = future.result()
            except Exception:
                candidate = None
            if candidate is not None:
                candidates.append(candidate)

    if not candidates:
        telegram_send(
            "⏸️ CIERRE M5 SIN ENTRADA\n\n"
            "Ningun OTC disponible cumplio las condiciones de accion del precio."
        )
        return None

    best = max(candidates, key=_candidate_strength)
    telegram_send(
        "🎯 SEÑAL M5 CONFIRMADA\n\n"
        f"Par elegido: {best['pair']}\n"
        f"Dirección: {best['signal'].upper()}\n"
        f"Score: {best['score']}/100\n"
        f"M5 cerrada: {best['m5_ts']}\n"
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


def execute_candidate(candidate: Dict[str, Any], current_m1_open: int, server_ts: float) -> bool:
    global LAST_TRADE_M1, LAST_TRADE_TIME
    target_m1 = int(candidate["execution_m1_ts"])
    if target_m1 != int(current_m1_open):
        return False
    # No ejecutar si ya pasó demasiado tiempo desde la apertura real.
    if float(server_ts) - float(target_m1) > ENTRY_MAX_DELAY_SECONDS:
        logger.warning(
            "Entrada perdida por latencia: par=%s retraso=%.3fs",
            candidate.get("pair"),
            float(server_ts) - float(target_m1),
        )
        return False
    if LAST_TRADE_M1 == current_m1_open:
        return False
    if time.time() - LAST_TRADE_TIME < TRADE_COOLDOWN:
        return False

    pair = str(candidate["pair"])
    signal = str(candidate["signal"])

    ok, order_id = buy_binary(pair, signal)
    if not ok:
        telegram_send(
            "❌ ORDEN RECHAZADA\n\n"
            f"Par: {pair}\nDirección: {signal.upper()}\n"
            "La señal no se trasladara a otra vela."
        )
        return False

    LAST_TRADE_M1 = current_m1_open
    LAST_TRADE_TIME = time.time()
    telegram_send(
        "⚡ ENTRADA EJECUTADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        f"Apertura M1: {current_m1_open}\n"
        f"M5 analizada: {candidate['m5_ts']}\n"
        f"ID: {order_id}\n"
        "⏳ Expiración: 1 minuto"
    )
    logger.info(
        "%s | %s | M5=%s | M1=%s | ID=%s",
        pair, signal.upper(), candidate["m5_ts"], current_m1_open, order_id,
    )
    return True

# ============================================================
# CICLO PRINCIPAL
# ============================================================
def process_cycle() -> None:
    global LAST_ANALYZED_M5, LAST_CANDIDATE

    if not BOT_RUNNING:
        return

    refresh_binary_otc_pairs()
    if not PAIRS:
        return

    server_ts = get_iq_server_timestamp()
    current_m1_open = floor_m1(server_ts)

    # Solo existe un cierre M5 al abrir una M1 cuyo timestamp es múltiplo de 300.
    if current_m1_open % M5_TIMEFRAME != 0:
        return

    closed_m5_ts = current_m1_open - M5_TIMEFRAME
    if closed_m5_ts == LAST_ANALYZED_M5:
        return

    LAST_ANALYZED_M5 = closed_m5_ts

    analysis_started = time.time()
    candidate = analyze_all_pairs_at_m5_close(closed_m5_ts)
    analysis_elapsed = time.time() - analysis_started

    if candidate is None:
        LAST_CANDIDATE = None
        return

    candidate = dict(candidate)
    candidate["execution_m1_ts"] = int(current_m1_open)
    LAST_CANDIDATE = candidate

    # Refrescar el reloj después del análisis. Si el análisis tardó demasiado,
    # no se entra tarde en la vela M1.
    final_server_ts = get_iq_server_timestamp()
    final_m1_open = floor_m1(final_server_ts)

    if final_m1_open != current_m1_open:
        logger.warning(
            "Señal M5 descartada: análisis tardó %.3fs y ya abrió otra M1 | par=%s",
            analysis_elapsed,
            candidate.get("pair"),
        )
        telegram_send(
            "⚠️ ENTRADA NO EJECUTADA\n\n"
            f"Par: {candidate['pair']}\n"
            f"Tiempo de análisis: {analysis_elapsed:.2f}s\n"
            "La siguiente M1 ya había comenzado; no se entra tarde."
        )
        LAST_CANDIDATE = None
        return

    if execute_candidate(
        candidate,
        final_m1_open,
        final_server_ts,
    ):
        LAST_CANDIDATE = None
    else:
        LAST_CANDIDATE = None


def main() -> None:
    global BOT_RUNNING
    logger.info("========================================")
    logger.info("BOT BINARY OTC - ACCION DEL PRECIO")
    logger.info("Analisis M5 | Entrada apertura M1 | Expiracion 1 minuto")
    logger.info("Opera SOLO UN par por cada cierre M5")
    logger.info("Analisis paralelo OTC: %d workers", ANALYSIS_WORKERS)
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
        "📊 Analiza TODOS los OTC disponibles en M5.\n"
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
