from __future__ import annotations

"""Recolector + simulador M1 para IQ Option.

IMPORTANTE:
- Solo ARBUSD-OTC.
- Solo M1.
- Solo precio.
- Sin indicadores, S/R ni rechazo.
- NO realiza operaciones reales.
- Genera senales hipoteticas y comprueba el resultado al cierre de la siguiente vela.
"""

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from strategy import M1, WINDOW, analyze_market, candle_data


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PAIR = os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip()
CANDLE_COUNT_M1 = max(30, int(os.getenv("CANDLE_COUNT_M1", "120")))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.10"))
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "5"))

SIMULATION_ONLY = True

IQ: Optional[IQ_Option] = None
RUNNING = True

# Timestamp de la vela que ya procesamos como cerrada.
LAST_CLOSED_TS: Optional[int] = None

# Actualizaciones intraminuto acumuladas por timestamp.
INTRABAR: Dict[int, Dict[str, Any]] = {}

# Ultimas 10 velas cerradas para el estudio.
CLOSED: list[Dict[str, Any]] = []

# Senal hipotetica pendiente de resolucion.
PENDING: Optional[Dict[str, Any]] = None

# Estadistica de simulacion.
SIM_STATS = {
    "signals": 0,
    "wins": 0,
    "losses": 0,
    "doji": 0,
}

LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("quant_m1")


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def tg(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram no configurado")
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Telegram: %s", exc)


def telegram_loop() -> None:
    global RUNNING

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    offset: Optional[int] = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    while True:
        try:
            params: Dict[str, Any] = {"timeout": 2}
            if offset is not None:
                params["offset"] = offset + 1

            response = requests.get(url, params=params, timeout=5)
            data = response.json()

            for update in data.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = int(update_id)

                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                command = str(message.get("text", "")).strip().lower()

                if command == "/start":
                    RUNNING = True
                    tg("🟢 SIMULADOR ACTIVADO\n\nSolo ARBUSD-OTC | M1\n🚫 Sin operaciones reales")

                elif command == "/stop":
                    RUNNING = False
                    tg("🔴 SIMULADOR DETENIDO")

                elif command == "/status":
                    tg(status_message())

        except Exception as exc:
            logger.debug("Telegram loop: %s", exc)
            time.sleep(1)


def status_message() -> str:
    with LOCK:
        pending = PENDING
        stats = dict(SIM_STATS)
        last = LAST_CLOSED_TS

    total = stats["wins"] + stats["losses"]
    accuracy = (stats["wins"] / total * 100.0) if total else 0.0

    return (
        "📊 ESTADO DEL SIMULADOR\n\n"
        f"Par: {PAIR}\n"
        "Timeframe: M1\n"
        f"Última vela procesada: {last}\n"
        f"Señales: {stats['signals']}\n"
        f"Ganadas: {stats['wins']}\n"
        f"Perdidas: {stats['losses']}\n"
        f"Doji: {stats['doji']}\n"
        f"Acierto histórico: {accuracy:.2f}%\n"
        f"Señal pendiente: {'SÍ' if pending else 'NO'}\n\n"
        "🚫 OPERACIONES REALES DESACTIVADAS"
    )


# ---------------------------------------------------------------------------
# IQ OPTION / ACTIVE ID
# ---------------------------------------------------------------------------

def connect_iq() -> bool:
    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL o IQ_PASSWORD")

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()

    if not ok:
        raise RuntimeError(f"IQ Option no conectó: {reason}")

    logger.info("Conectado a IQ Option")
    tg("🟢 IQ OPTION CONECTADO\n\nModo SIMULACIÓN\nPar solicitado: ARBUSD-OTC")

    return True


def is_connected() -> bool:
    if IQ is None:
        return False
    try:
        return bool(IQ.check_connect())
    except Exception:
        return False


def refresh_active_id(pair: str) -> Optional[int]:
    """Busca el active_id real y lo registra en OP_code.ACTIVES.

    Esto evita el error:
    'Asset ARBUSD-OTC not found on consts' / NoneType not iterable.
    """
    if IQ is None:
        return None

    try:
        catalog = IQ.get_all_init_v2()
    except Exception as exc:
        logger.warning("No se pudo obtener catálogo: %s", exc)
        return None

    binary = catalog.get("binary", {}) if isinstance(catalog, dict) else {}
    actives = binary.get("actives", {}) if isinstance(binary, dict) else {}

    wanted = pair.upper()

    for active_id, info in actives.items():
        if not isinstance(info, dict):
            continue

        name = str(info.get("name", "")).strip()
        clean = name.split(".", 1)[-1].strip()

        if clean.upper() != wanted:
            continue

        if info.get("enabled", True) is False:
            continue
        if info.get("is_suspended", info.get("suspended", False)):
            continue

        try:
            active_int = int(active_id)
            setattr(OP_code, "ACTIVES", getattr(OP_code, "ACTIVES", {}))
            OP_code.ACTIVES[clean] = active_int
            OP_code.ACTIVES[pair] = active_int
            logger.info("Activo resuelto: %s -> %s", clean, active_int)
            return active_int
        except Exception as exc:
            logger.warning("No se pudo registrar active_id: %s", exc)
            return None

    logger.error("Activo no disponible en catálogo: %s", pair)
    return None


def ensure_pair_registered() -> bool:
    active_id = refresh_active_id(PAIR)
    if active_id is None:
        tg(
            "⚠️ PAR NO DISPONIBLE\n\n"
            f"{PAIR} no aparece disponible en el catálogo OTC actual.\n"
            "El bot NO iniciará el stream y NO ejecutará operaciones."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# REALTIME CANDLES
# ---------------------------------------------------------------------------

def start_stream() -> bool:
    if IQ is None:
        return False

    if not ensure_pair_registered():
        return False

    try:
        IQ.start_candles_stream(PAIR, M1, CANDLE_COUNT_M1)
        logger.info("Stream iniciado: %s M1", PAIR)
        tg(
            "📡 STREAM M1 INICIADO\n\n"
            f"Par: {PAIR}\n"
            f"Ventana: {WINDOW} velas\n"
            "Datos intraminuto: ACTIVOS\n"
            "🚫 Operaciones reales: DESACTIVADAS"
        )
        return True
    except Exception as exc:
        logger.exception("No se pudo iniciar stream")
        tg(
            "❌ ERROR AL INICIAR STREAM\n\n"
            f"Par: {PAIR}\n"
            f"Error: {type(exc).__name__}: {exc}\n\n"
            "Se reintentará sin ejecutar operaciones."
        )
        return False


def stop_stream() -> None:
    if IQ is None:
        return
    try:
        IQ.stop_candles_stream(PAIR, M1)
    except Exception:
        pass


def get_stream() -> Dict[Any, Any]:
    if IQ is None:
        return {}

    try:
        raw = IQ.get_realtime_candles(PAIR, M1)
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        logger.debug("get_realtime_candles: %s", exc)
        return {}


def parse_snapshot(ts_key: Any, raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None

    def num(*names: str) -> Optional[float]:
        for name in names:
            if name in raw and raw[name] is not None:
                try:
                    return float(raw[name])
                except Exception:
                    return None
        return None

    ts_value = num("from", "at")
    if ts_value is None:
        try:
            ts_value = float(ts_key)
        except Exception:
            return None

    o = num("open")
    c = num("close")
    h = num("max", "high")
    l = num("min", "low")

    if None in (o, c, h, l):
        return None

    return {
        "timestamp": int(ts_value),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
    }


def record_snapshot(snap: Dict[str, Any]) -> None:
    ts = int(snap["timestamp"])
    state = INTRABAR.get(ts)

    if state is None:
        state = {
            "timestamp": ts,
            "open": float(snap["open"]),
            "high": float(snap["high"]),
            "low": float(snap["low"]),
            "close": float(snap["close"]),
            "updates": 0,
            "up_moves": 0,
            "down_moves": 0,
            "flat_moves": 0,
            "path": [],
            "first_price": float(snap["open"]),
            "last_price": float(snap["close"]),
            "max_seen": float(snap["high"]),
            "min_seen": float(snap["low"]),
            "max_seen_ts": ts,
            "min_seen_ts": ts,
        }
        INTRABAR[ts] = state

    previous_price = state["last_price"]
    current_price = float(snap["close"])

    if current_price > previous_price:
        state["up_moves"] += 1
    elif current_price < previous_price:
        state["down_moves"] += 1
    else:
        state["flat_moves"] += 1

    state["updates"] += 1
    state["open"] = float(snap["open"])
    state["high"] = max(float(state["high"]), float(snap["high"]))
    state["low"] = min(float(state["low"]), float(snap["low"]))
    state["close"] = current_price
    state["last_price"] = current_price

    if float(snap["high"]) >= float(state["max_seen"]):
        state["max_seen"] = float(snap["high"])
        state["max_seen_ts"] = ts

    if float(snap["low"]) <= float(state["min_seen"]):
        state["min_seen"] = float(snap["low"])
        state["min_seen_ts"] = ts

    path = state["path"]
    path.append(current_price)
    if len(path) > 500:
        del path[:-500]


def finalize_candle(ts: int, fallback: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    state = INTRABAR.pop(ts, None)

    if state is None:
        if fallback is None:
            return None
        state = {
            "timestamp": ts,
            "open": fallback["open"],
            "high": fallback["high"],
            "low": fallback["low"],
            "close": fallback["close"],
            "updates": 0,
            "up_moves": 0,
            "down_moves": 0,
            "flat_moves": 0,
            "path": [],
            "first_price": fallback["open"],
            "last_price": fallback["close"],
            "max_seen": fallback["high"],
            "min_seen": fallback["low"],
            "max_seen_ts": ts,
            "min_seen_ts": ts,
        }

    row = pd.Series(
        {
            "from": ts,
            "open": state["open"],
            "high": state["high"],
            "low": state["low"],
            "close": state["close"],
        }
    )

    result = candle_data(row)

    path = state["path"]
    if path:
        total_abs = sum(abs(path[i] - path[i - 1]) for i in range(1, len(path)))
    else:
        total_abs = result["range"]

    open_price = result["open"]
    result.update(
        {
            "updates": int(state["updates"]),
            "up_moves": int(state["up_moves"]),
            "down_moves": int(state["down_moves"]),
            "flat_moves": int(state["flat_moves"]),
            "path_points": len(path),
            "path_distance": total_abs,
            "up_from_open": max(0.0, result["high"] - open_price),
            "down_from_open": max(0.0, open_price - result["low"]),
            "max_seen_ts": int(state["max_seen_ts"]),
            "min_seen_ts": int(state["min_seen_ts"]),
        }
    )

    return result


# ---------------------------------------------------------------------------
# DATAFRAME / CLOSED CANDLES
# ---------------------------------------------------------------------------

def candle_dataframe() -> pd.DataFrame:
    rows = []
    for candle in CLOSED:
        rows.append(
            {
                "from": candle["timestamp"],
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            }
        )
    return pd.DataFrame(rows)


def format_price(value: float) -> str:
    return f"{float(value):.8f}"


def format_candle_message(c: Dict[str, Any]) -> str:
    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {PAIR}\n"
        f"Timestamp: {c['timestamp']}\n"
        f"Color: {c['color']}\n\n"
        f"Apertura: {format_price(c['open'])}\n"
        f"Máximo: {format_price(c['high'])}\n"
        f"Mínimo: {format_price(c['low'])}\n"
        f"Cierre: {format_price(c['close'])}\n\n"
        f"Mecha inferior: {c['lower_wick']:.8f}\n"
        f"Mecha superior: {c['upper_wick']:.8f}\n"
        f"Cuerpo: {c['body']:.8f}\n"
        f"Rango: {c['range']:.8f}\n"
        f"Body/R: {c['body_ratio']:.2f}%\n\n"
        f"↑ desde apertura: {c['up_from_open']:.8f}\n"
        f"↓ desde apertura: {c['down_from_open']:.8f}\n"
        f"Recorrido intraminuto: {c['path_distance']:.8f}\n"
        f"Actualizaciones capturadas: {c['updates']}\n"
        f"Movimientos ↑: {c['up_moves']}\n"
        f"Movimientos ↓: {c['down_moves']}\n"
        f"Sin cambio: {c['flat_moves']}\n"
        f"Puntos de recorrido: {c['path_points']}"
    )


def format_window() -> str:
    sequence = " ".join(
        "V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D"
        for c in CLOSED[-WINDOW:]
    )

    lines = [
        "📊 ÚLTIMAS 10 VELAS M1",
        "",
        f"Par: {PAIR}",
        f"Secuencia: {sequence}",
        "",
    ]

    for i, c in enumerate(CLOSED[-WINDOW:], 1):
        color = "V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D"
        lines.append(
            f"{i:02d} {color} | O={c['open']:.8f} | H={c['high']:.8f} | "
            f"L={c['low']:.8f} | C={c['close']:.8f} | "
            f"MI={c['lower_wick']:.8f} | MS={c['upper_wick']:.8f} | "
            f"Body={c['body']:.8f} | R={c['range']:.8f} | "
            f"Body/R={c['body_ratio']:.2f}%"
        )

    lines.append("")
    lines.append("Las 10 velas se conservan para estudiar que características preceden a la siguiente.")
    lines.append("🚫 Operaciones REALES desactivadas.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SIMULATION
# ---------------------------------------------------------------------------

def evaluate_pending(next_candle: Dict[str, Any]) -> Optional[str]:
    global PENDING

    if not PENDING:
        return None

    pending = PENDING
    PENDING = None

    signal = pending["signal"]
    actual = next_candle["color"]

    if actual == "DOJI":
        result = "DOJI / SIN RESULTADO"
        SIM_STATS["doji"] += 1
    elif (signal == "CALL" and actual == "VERDE") or (signal == "PUT" and actual == "ROJA"):
        result = "✅ CONTINUACIÓN"
        SIM_STATS["wins"] += 1
    else:
        result = "❌ CONTRA LA SEÑAL"
        SIM_STATS["losses"] += 1

    total = SIM_STATS["wins"] + SIM_STATS["losses"]
    accuracy = (SIM_STATS["wins"] / total * 100.0) if total else 0.0

    return (
        "📊 RESULTADO DE SIMULACIÓN\n\n"
        f"Par: {PAIR}\n"
        f"Señal hipotética: {signal}\n"
        f"Entrada teórica: {pending['entry_price']:.8f}\n"
        f"Vela siguiente: {actual}\n"
        f"Apertura siguiente: {next_candle['open']:.8f}\n"
        f"Cierre siguiente: {next_candle['close']:.8f}\n"
        f"Body/R siguiente: {next_candle['body_ratio']:.2f}%\n\n"
        f"Resultado: {result}\n"
        f"Histórico: {SIM_STATS['wins']} W / {SIM_STATS['losses']} L\n"
        f"Acierto: {accuracy:.2f}%\n\n"
        "🚫 No se envió ninguna orden real."
    )


def process_closed_candle(candle: Dict[str, Any]) -> None:
    global PENDING, CLOSED, LAST_CLOSED_TS

    with LOCK:
        LAST_CLOSED_TS = int(candle["timestamp"])

        # Primero resolvemos la señal que esperaba esta vela.
        result_message = evaluate_pending(candle)

        CLOSED.append(candle)
        if len(CLOSED) > max(WINDOW + 20, CANDLE_COUNT_M1):
            del CLOSED[:-max(WINDOW + 20, CANDLE_COUNT_M1)]

        # Telegram con anatomía completa.
        tg(format_candle_message(candle))

        if result_message:
            tg(result_message)

        if len(CLOSED) >= WINDOW:
            tg(format_window())

            df = candle_dataframe()
            analysis = analyze_market(df)

            signal = analysis.get("signal")
            score = int(analysis.get("score", 0))
            reason = str(analysis.get("reason", ""))

            if signal in ("CALL", "PUT"):
                PENDING = {
                    "signal": signal,
                    "score": score,
                    "reason": reason,
                    "entry_price": float(candle["close"]),
                    "signal_candle_ts": int(candle["timestamp"]),
                    "next_candle_ts": int(candle["timestamp"] + M1),
                }
                SIM_STATS["signals"] += 1

                tg(
                    "🎯 SEÑAL HIPOTÉTICA\n\n"
                    f"Par: {PAIR}\n"
                    f"Dirección: {signal}\n"
                    f"Score: {score}/9\n"
                    f"Entrada teórica próxima M1: {candle['close']:.8f}\n"
                    "Expiración teórica: 1 minuto\n\n"
                    f"Motivo:\n{reason}\n\n"
                    "⚠️ SIMULACIÓN — NO SE EJECUTA ORDEN REAL"
                )
            else:
                tg(
                    "🔎 SIN SEÑAL HIPOTÉTICA\n\n"
                    f"Score: {score}/9\n"
                    f"{reason}\n\n"
                    "Se espera la siguiente vela.\n"
                    "🚫 Operaciones reales desactivadas."
                )


# ---------------------------------------------------------------------------
# STREAM PROCESSOR
# ---------------------------------------------------------------------------

def process_stream_once() -> None:
    global LAST_CLOSED_TS

    raw = get_stream()
    if not raw:
        return

    snapshots: Dict[int, Dict[str, Any]] = {}

    for key, value in raw.items():
        snap = parse_snapshot(key, value)
        if snap is None:
            continue
        snapshots[int(snap["timestamp"])] = snap
        record_snapshot(snap)

    if not snapshots:
        return

    current_ts = max(snapshots)

    # Si ya tenemos la hora del servidor, podemos determinar qué vela está abierta.
    try:
        server = int(float(IQ.get_server_timestamp())) if IQ else int(time.time())
        open_ts = (server // M1) * M1
    except Exception:
        open_ts = current_ts

    closed_candidates = sorted(ts for ts in snapshots if ts < open_ts)

    # En el primer ciclo usamos el historial del stream para inicializar sin
    # mandar todas las velas antiguas como eventos nuevos.
    if LAST_CLOSED_TS is None:
        if closed_candidates:
            latest = closed_candidates[-1]
            for ts in closed_candidates[-WINDOW:]:
                fallback = snapshots.get(ts)
                candle = finalize_candle(ts, fallback)
                if candle:
                    CLOSED.append(candle)
            CLOSED[:] = CLOSED[-WINDOW:]
            LAST_CLOSED_TS = latest
            logger.info("Inicializado con %d velas cerradas; esperando siguiente cierre", len(CLOSED))
        return

    new_closed = [ts for ts in closed_candidates if ts > LAST_CLOSED_TS]

    for ts in new_closed:
        fallback = snapshots.get(ts)
        candle = finalize_candle(ts, fallback)
        if candle:
            process_closed_candle(candle)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID no configurados")

    threading.Thread(target=telegram_loop, daemon=True).start()

    try:
        connect_iq()
    except Exception as exc:
        logger.exception("No se pudo conectar")
        tg(f"❌ ERROR DE INICIO\n\n{type(exc).__name__}: {exc}")
        return

    # Reintento controlado si el par todavía no está disponible.
    while True:
        try:
            if not is_connected():
                logger.warning("Conexión perdida; reconectando")
                try:
                    IQ.connect()
                except Exception:
                    pass
                time.sleep(2)
                continue

            if not start_stream():
                time.sleep(10)
                continue

            tg(
                "🤖 SIMULADOR LISTO\n\n"
                f"Par: {PAIR}\n"
                "M1 → siguiente M1\n"
                "Expiración teórica: 1 minuto\n\n"
                "Se estudiarán las últimas 10 velas y se registrará el resultado de cada señal.\n"
                "🚫 NINGUNA ORDEN REAL SERÁ ENVIADA."
            )

            while True:
                if not RUNNING:
                    time.sleep(0.5)
                    continue

                if not is_connected():
                    break

                process_stream_once()
                time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.exception("Error del loop principal: %s", exc)
            tg(f"⚠️ ERROR CONTROLADO\n\n{type(exc).__name__}: {exc}\n\nSe reiniciará el stream.")
            try:
                stop_stream()
            except Exception:
                pass
            time.sleep(3)


if __name__ == "__main__":
    main()
