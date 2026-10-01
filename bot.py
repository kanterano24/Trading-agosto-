from __future__ import annotations
"""Recolector + simulador M1 para IQ Option.

- Un solo par: ARBUSD-OTC (o ANALYSIS_PAIR).
- Solo precio/anatomia; sin indicadores, S/R ni rechazo.
- Operaciones REALES DESACTIVADAS.
- Captura actualizaciones del stream para estudiar el movimiento intraminuto.
"""
import logging
import os
import threading
import time
from typing import Any, Optional

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from strategy import M1, WINDOW, analyze_market, candle_metrics

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAIR_REQUESTED = os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip()
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "120"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.20"))

# Nunca se usa IQ.buy(). Es una version de observacion/simulacion.
REAL_TRADING_ENABLED = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

IQ: Optional[IQ_Option] = None
PAIR = PAIR_REQUESTED
RUNNING = True
STREAM_STARTED = False

# Ultimas velas cerradas para estrategia.
CLOSED: list[dict[str, Any]] = []

# Vela que esta siendo construida. Los samples son snapshots recibidos del stream.
CURRENT_START: Optional[int] = None
CURRENT_SAMPLES: list[dict[str, Any]] = []
LAST_CLOSED_START: Optional[int] = None

# Simulaciones pendientes: se evalua al cerrar la vela siguiente.
PENDING_SIM: Optional[dict[str, Any]] = None

STATS = {"signals": 0, "wins": 0, "losses": 0, "doji": 0}


def tg(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": str(TELEGRAM_CHAT_ID), "text": msg},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Telegram: %s", exc)


def server_ts() -> float:
    try:
        return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:
        return time.time()


def floor_m1(ts: float) -> int:
    return int(ts // M1) * M1


def normalize_candle(c: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        if not isinstance(c, dict):
            return None
        row = {
            "from": int(float(c.get("from", c.get("at", 0)))),
            "open": float(c.get("open")),
            "high": float(c.get("max", c.get("high"))),
            "low": float(c.get("min", c.get("low"))),
            "close": float(c.get("close")),
        }
        if row["from"] <= 0:
            return None
        if row["high"] < row["low"]:
            return None
        return row
    except (TypeError, ValueError):
        return None


def resolve_active(pair: str) -> Optional[int]:
    """Resolve active id and register it before start_candles_stream()."""
    global PAIR
    candidates = [pair]

    try:
        data = IQ.get_all_init_v2()
        if isinstance(data, dict):
            binary = data.get("binary", {})
            actives = binary.get("actives", {}) if isinstance(binary, dict) else {}
            for active_id, info in actives.items():
                if not isinstance(info, dict):
                    continue
                name = str(info.get("name", "")).strip()
                if name == pair or name.replace("_OTC", "-OTC") == pair:
                    aid = int(active_id)
                    OP_code.ACTIVES[pair] = aid
                    OP_code.ACTIVES[name] = aid
                    PAIR = pair
                    logger.info("Activo resuelto: %s -> %s", pair, aid)
                    return aid
    except Exception as exc:
        logger.warning("No se pudo leer catalogo binary: %s", exc)

    # Buscar variantes en constants.
    for name in candidates:
        try:
            aid = OP_code.ACTIVES.get(name)
            if aid is not None:
                logger.info("Activo encontrado en constants: %s -> %s", name, aid)
                return int(aid)
        except Exception:
            pass

    logger.error("Activo %s no encontrado", pair)
    return None


def connect() -> bool:
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    ok, reason = IQ.connect()
    if not ok:
        raise ConnectionError(reason)

    logger.info("Conectado a IQ Option")
    aid = resolve_active(PAIR_REQUESTED)
    if aid is None:
        # No hacemos start_candles_stream si el id no existe.
        tg(f"❌ No se encontro el activo {PAIR_REQUESTED} en el catalogo de IQ Option.")
        return False

    return True


def ensure_connection() -> bool:
    global IQ
    if IQ is None:
        return False
    try:
        if IQ.check_connect():
            return True
    except Exception:
        pass
    try:
        ok = IQ.connect()
        return bool(ok[0]) if isinstance(ok, tuple) else bool(ok)
    except Exception as exc:
        logger.warning("Reconectar: %s", exc)
        return False


def start_stream() -> bool:
    global STREAM_STARTED
    if not ensure_connection():
        return False

    aid = resolve_active(PAIR_REQUESTED)
    if aid is None:
        return False

    try:
        # Solo despues de resolver OP_code.ACTIVES.
        IQ.start_candles_stream(PAIR_REQUESTED, M1, CANDLE_COUNT_M1)
        STREAM_STARTED = True
        logger.info("Stream iniciado: %s M1", PAIR_REQUESTED)
        tg(
            "🟢 STREAM M1 INICIADO\n\n"
            f"Par: {PAIR_REQUESTED}\n"
            "Modo: SIMULACION\n"
            "Operaciones reales: DESACTIVADAS\n"
            "Esperando siguiente cierre..."
        )
        return True
    except Exception as exc:
        STREAM_STARTED = False
        logger.exception("No se pudo iniciar stream: %s", exc)
        tg(f"⚠️ No se pudo iniciar stream M1 de {PAIR_REQUESTED}. Se reintentara.\n\n{exc}")
        return False


def stop_stream() -> None:
    global STREAM_STARTED
    if not STREAM_STARTED or IQ is None:
        return
    try:
        IQ.stop_candles_stream(PAIR_REQUESTED, M1)
    except Exception:
        pass
    STREAM_STARTED = False


def get_stream_candles() -> list[dict[str, Any]]:
    if IQ is None or not STREAM_STARTED:
        return []
    try:
        raw = IQ.get_realtime_candles(PAIR_REQUESTED, M1)
    except Exception as exc:
        logger.warning("Realtime candles: %s", exc)
        return []
    if not isinstance(raw, dict):
        return []
    out = []
    for key, value in raw.items():
        c = normalize_candle(value)
        if c is None:
            continue
        if c["from"] == 0:
            try:
                c["from"] = int(float(key))
            except Exception:
                pass
        out.append(c)
    return sorted(out, key=lambda x: x["from"])


def candle_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["from", "open", "high", "low", "close"])
    return pd.DataFrame(candles).drop_duplicates("from").sort_values("from").reset_index(drop=True)


def format_number(x: Any) -> str:
    try:
        return f"{float(x):.8f}"
    except Exception:
        return "-"


def finalize_candle(row: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    m = candle_metrics(pd.Series(row))
    prices = [float(s["close"]) for s in samples if "close" in s]
    times = [int(s["ts"]) for s in samples if "ts" in s]

    # El high/low oficial de la vela sigue siendo la fuente principal.
    up_move = max([p - m["open"] for p in prices] + [m["high"] - m["open"], 0.0])
    down_move = max([m["open"] - p for p in prices] + [m["open"] - m["low"], 0.0])
    traveled = 0.0
    up_steps = down_steps = flat_steps = 0
    for a, b in zip(prices, prices[1:]):
        delta = b - a
        traveled += abs(delta)
        if delta > 0: up_steps += 1
        elif delta < 0: down_steps += 1
        else: flat_steps += 1

    return {
        **m,
        "sample_count": len(samples),
        "up_move": up_move,
        "down_move": down_move,
        "traveled": traveled,
        "up_steps": up_steps,
        "down_steps": down_steps,
        "flat_steps": flat_steps,
        "first_sample": prices[0] if prices else m["open"],
        "last_sample": prices[-1] if prices else m["close"],
        "sample_times": times,
    }


def format_candle_message(c: dict[str, Any]) -> str:
    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {PAIR_REQUESTED}\n"
        f"Timestamp: {c['timestamp']}\n"
        f"Color: {c['color']}\n\n"
        f"Apertura: {format_number(c['open'])}\n"
        f"Maximo: {format_number(c['high'])}\n"
        f"Minimo: {format_number(c['low'])}\n"
        f"Cierre: {format_number(c['close'])}\n\n"
        f"Mecha inferior: {format_number(c['lower_wick'])}\n"
        f"Mecha superior: {format_number(c['upper_wick'])}\n"
        f"Body: {format_number(c['body'])}\n"
        f"Rango: {format_number(c['range'])}\n"
        f"Body/R: {c['body_ratio']*100:.2f}%\n\n"
        f"Movimiento desde apertura: +{format_number(c['up_move'])} / -{format_number(c['down_move'])}\n"
        f"Recorrido acumulado observado: {format_number(c['traveled'])}\n"
        f"Actualizaciones recibidas: {c['sample_count']}\n"
        f"Movimientos: ↑ {c['up_steps']} | ↓ {c['down_steps']} | = {c['flat_steps']}\n\n"
        "Solo precio. Sin indicadores, S/R ni rechazo.\n"
        "🚫 Operaciones reales DESACTIVADAS."
    )


def format_window(candles: list[dict[str, Any]]) -> str:
    seq = " ".join("V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in candles)
    lines = ["📊 ULTIMAS 10 VELAS M1", "", f"Par: {PAIR_REQUESTED}", f"Secuencia: {seq}", ""]
    for i, c in enumerate(candles, 1):
        lines.append(
            f"{i:02d} {'V' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D'} "
            f"| O={format_number(c['open'])} | H={format_number(c['high'])} | L={format_number(c['low'])} | C={format_number(c['close'])} "
            f"| MI={format_number(c['lower_wick'])} | MS={format_number(c['upper_wick'])} | Body={format_number(c['body'])} "
            f"| R={format_number(c['range'])} | Body/R={c['body_ratio']*100:.2f}%"
        )
    lines += ["", "Las 10 velas se conservan para estudiar que caracteristicas preceden a la siguiente.", "🚫 Operaciones DESACTIVADAS."]
    return "\n".join(lines)


def simulate_and_message() -> None:
    global PENDING_SIM
    if len(CLOSED) < WINDOW:
        return
    df = candle_to_df(CLOSED)
    result = analyze_market(df=df)
    if result.get("signal") not in ("CALL", "PUT"):
        return

    PENDING_SIM = {
        "signal": result["signal"],
        "score": result["score"],
        "reason": result["reason"],
        "entry_price": CLOSED[-1]["close"],
        "signal_timestamp": CLOSED[-1]["timestamp"],
    }
    STATS["signals"] += 1
    tg(
        "🧪 SEÑAL HIPOTETICA\n\n"
        f"Par: {PAIR_REQUESTED}\n"
        f"Direccion: {result['signal']}\n"
        f"Score: {result['score']}/9\n"
        f"Entrada teorica: {format_number(PENDING_SIM['entry_price'])}\n"
        "Momento: apertura de la SIGUIENTE M1\n"
        "Expiracion teorica: 1 minuto\n\n"
        f"Motivos: {result['reason']}\n\n"
        "⚠️ No se envia ninguna orden real."
    )


def evaluate_pending(next_candle: dict[str, Any]) -> None:
    global PENDING_SIM
    if not PENDING_SIM:
        return
    signal = PENDING_SIM["signal"]
    color = next_candle["color"]
    win = (signal == "CALL" and color == "VERDE") or (signal == "PUT" and color == "ROJA")
    doji = color == "DOJI"
    if doji:
        STATS["doji"] += 1
        result = "⚪ DOJI"
    elif win:
        STATS["wins"] += 1
        result = "✅ FAVORABLE"
    else:
        STATS["losses"] += 1
        result = "❌ CONTRARIA"
    tg(
        "🧪 RESULTADO SIMULACION\n\n"
        f"Señal: {signal}\n"
        f"Entrada teorica: {format_number(PENDING_SIM['entry_price'])}\n"
        f"Siguiente vela: {color}\n"
        f"Resultado: {result}\n\n"
        f"Estadisticas: {STATS['wins']} favorables | {STATS['losses']} contrarias | {STATS['doji']} doji | {STATS['signals']} señales"
    )
    PENDING_SIM = None


def process_closed_candle(row: dict[str, Any]) -> None:
    global CLOSED
    c = finalize_candle(row, CURRENT_SAMPLES)
    CLOSED.append(c)
    CLOSED = CLOSED[-WINDOW:]

    # La vela recién cerrada es el resultado de una eventual simulación anterior.
    evaluate_pending(c)

    tg(format_candle_message(c))
    if len(CLOSED) == WINDOW:
        tg(format_window(CLOSED))
        simulate_and_message()


def process_stream_once() -> None:
    global CURRENT_START, CURRENT_SAMPLES, LAST_CLOSED_START
    candles = get_stream_candles()
    if not candles:
        return

    now_start = floor_m1(server_ts())
    # Elegimos la vela actual por timestamp; si no aparece, usamos la mas reciente.
    current = next((c for c in candles if c["from"] == now_start), candles[-1])

    if CURRENT_START is None:
        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []
        logger.info("Inicializado con vela %s; esperando siguiente cierre", CURRENT_START)
        return

    # Si cambio el timestamp, la vela anterior esta cerrada.
    if current["from"] != CURRENT_START:
        previous = next((c for c in candles if c["from"] == CURRENT_START), None)
        if previous is None:
            # Si el stream ya retiro la vela, usamos el ultimo registro conocido de samples.
            if CURRENT_SAMPLES:
                last = CURRENT_SAMPLES[-1]
                previous = {
                    "from": CURRENT_START,
                    "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"],
                }
        if previous is not None and LAST_CLOSED_START != CURRENT_START:
            process_closed_candle(previous)
            LAST_CLOSED_START = CURRENT_START
        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []

    CURRENT_SAMPLES.append({
        "ts": int(time.time()),
        "open": current["open"],
        "high": current["high"],
        "low": current["low"],
        "close": current["close"],
    })


def main() -> None:
    if not all((IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)):
        logger.error("Faltan IQ_EMAIL/IQ_PASSWORD/TELEGRAM_TOKEN/TELEGRAM_CHAT_ID")
        return

    tg("🤖 BOT M1 SIMULACION INICIANDO\n\nPar: ARBUSD-OTC\nOperaciones reales: DESACTIVADAS")

    try:
        if not connect():
            return
    except Exception as exc:
        logger.exception("Conexion: %s", exc)
        tg(f"❌ ERROR DE CONEXION\n\n{exc}")
        return

    # Reintenta el stream sin tumbar el contenedor.
    while RUNNING:
        try:
            if not ensure_connection():
                time.sleep(2)
                continue
            if not STREAM_STARTED:
                if not start_stream():
                    time.sleep(5)
                    continue
            process_stream_once()
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.exception("Error del loop principal: %s", exc)
            tg(f"⚠️ Error controlado en loop; reintentando.\n\n{type(exc).__name__}: {exc}")
            stop_stream()
            time.sleep(3)

    stop_stream()


if __name__ == "__main__":
    main()
