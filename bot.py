from __future__ import annotations
"""Recolector + simulador M1 para IQ Option.

Un solo par: ARBUSD-OTC.
Solo precio/anatomia. Sin indicadores, S/R ni rechazo.
OPERACIONES REALES DESACTIVADAS.

Importante: este archivo NO importa candle_metrics desde strategy.py.
Asi, aunque Railway conserve temporalmente una copia antigua de strategy.py,
el bot no cae por un ImportError de esa funcion.
"""
import logging
import os
import time
from typing import Any, Optional

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

from strategy import M1, WINDOW, analyze_market

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAIR_REQUESTED = os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip() or "ARBUSD-OTC"
CANDLE_COUNT_M1 = int(os.getenv("CANDLE_COUNT_M1", "120"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.20"))
REAL_TRADING_ENABLED = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

IQ: Optional[IQ_Option] = None
RUNNING = True
STREAM_STARTED = False
CLOSED: list[dict[str, Any]] = []
CURRENT_START: Optional[int] = None
CURRENT_SAMPLES: list[dict[str, Any]] = []
LAST_CLOSED_START: Optional[int] = None
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


def candle_metrics(row: pd.Series) -> dict[str, Any]:
    o = float(row["open"]); h = float(row["high"])
    l = float(row["low"]); c = float(row["close"])
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    color = "VERDE" if c > o else "ROJA" if c < o else "DOJI"
    return {
        "timestamp": int(row["from"]) if "from" in row and pd.notna(row["from"]) else None,
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "range": rng,
        "upper_wick": upper, "lower_wick": lower,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        "close_pos": (c - l) / rng,
        "color": color,
    }


def normalize_candle(c: Any, fallback_ts: Any = None) -> Optional[dict[str, Any]]:
    if not isinstance(c, dict):
        return None
    try:
        raw_from = c.get("from", c.get("at", fallback_ts))
        if raw_from is None:
            return None
        row = {
            "from": int(float(raw_from)),
            "open": float(c.get("open")),
            "high": float(c.get("max", c.get("high"))),
            "low": float(c.get("min", c.get("low"))),
            "close": float(c.get("close")),
        }
        if row["from"] <= 0 or row["high"] < row["low"]:
            return None
        return row
    except (TypeError, ValueError):
        return None


def resolve_active(pair: str) -> Optional[int]:
    """Resuelve active_id y lo registra antes de iniciar el stream."""
    if IQ is None:
        return None
    try:
        data = IQ.get_all_init_v2()
        if isinstance(data, dict):
            binary = data.get("binary", {})
            actives = binary.get("actives", {}) if isinstance(binary, dict) else {}
            for active_id, info in actives.items():
                if not isinstance(info, dict):
                    continue
                name = str(info.get("name", "")).strip()
                variants = {name, name.replace("_OTC", "-OTC"), name.replace("-OTC", "_OTC")}
                if pair in variants:
                    aid = int(active_id)
                    OP_code.ACTIVES[pair] = aid
                    OP_code.ACTIVES[name] = aid
                    logger.info("Activo resuelto: %s -> %s", pair, aid)
                    return aid
    except Exception as exc:
        logger.warning("Catalogo binary: %s", exc)

    try:
        aid = OP_code.ACTIVES.get(pair)
        if aid is not None:
            logger.info("Activo encontrado en constants: %s -> %s", pair, aid)
            return int(aid)
    except Exception as exc:
        logger.warning("Constants: %s", exc)
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
        tg(f"❌ No se encontro {PAIR_REQUESTED} en el catalogo de IQ Option.")
        return False
    return True


def ensure_connection() -> bool:
    if IQ is None:
        return False
    try:
        if IQ.check_connect():
            return True
    except Exception:
        pass
    try:
        result = IQ.connect()
        return bool(result[0]) if isinstance(result, tuple) else bool(result)
    except Exception as exc:
        logger.warning("Reconectar: %s", exc)
        return False


def start_stream() -> bool:
    global STREAM_STARTED
    if not ensure_connection():
        return False
    if resolve_active(PAIR_REQUESTED) is None:
        return False
    try:
        IQ.start_candles_stream(PAIR_REQUESTED, M1, CANDLE_COUNT_M1)
        STREAM_STARTED = True
        logger.info("Stream iniciado: %s M1", PAIR_REQUESTED)
        tg("🟢 STREAM M1 INICIADO\n\nPar: " + PAIR_REQUESTED + "\nModo: SIMULACION\nOperaciones reales: DESACTIVADAS\nEsperando cierres...")
        return True
    except Exception as exc:
        STREAM_STARTED = False
        logger.exception("No se pudo iniciar stream: %s", exc)
        tg(f"⚠️ Stream M1 fallo. Se reintentara.\n\n{type(exc).__name__}: {exc}")
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
        c = normalize_candle(value, key)
        if c is not None:
            out.append(c)
    return sorted(out, key=lambda x: x["from"])


def candle_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["from", "open", "high", "low", "close"])
    return pd.DataFrame(candles).drop_duplicates("from").sort_values("from").reset_index(drop=True)


def fmt(x: Any) -> str:
    try:
        return f"{float(x):.8f}"
    except Exception:
        return "-"


def finalize_candle(row: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    m = candle_metrics(pd.Series(row))
    prices = [float(s["close"]) for s in samples if "close" in s]
    times = [int(s["ts"]) for s in samples if "ts" in s]
    up_move = max([p - m["open"] for p in prices] + [m["high"] - m["open"], 0.0])
    down_move = max([m["open"] - p for p in prices] + [m["open"] - m["low"], 0.0])
    traveled = sum(abs(b - a) for a, b in zip(prices, prices[1:]))
    up_steps = sum(b > a for a, b in zip(prices, prices[1:]))
    down_steps = sum(b < a for a, b in zip(prices, prices[1:]))
    flat_steps = sum(b == a for a, b in zip(prices, prices[1:]))
    return {
        **m,
        "sample_count": len(samples),
        "up_move": up_move, "down_move": down_move, "traveled": traveled,
        "up_steps": up_steps, "down_steps": down_steps, "flat_steps": flat_steps,
        "first_sample": prices[0] if prices else m["open"],
        "last_sample": prices[-1] if prices else m["close"],
        "sample_times": times,
    }


def format_candle_message(c: dict[str, Any]) -> str:
    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {PAIR_REQUESTED}\nTimestamp: {c['timestamp']}\nColor: {c['color']}\n\n"
        f"Apertura: {fmt(c['open'])}\nMaximo: {fmt(c['high'])}\nMinimo: {fmt(c['low'])}\nCierre: {fmt(c['close'])}\n\n"
        f"Mecha inferior: {fmt(c['lower_wick'])}\nMecha superior: {fmt(c['upper_wick'])}\n"
        f"Body: {fmt(c['body'])}\nRango: {fmt(c['range'])}\nBody/R: {c['body_ratio']*100:.2f}%\n\n"
        f"Movimiento desde apertura: +{fmt(c['up_move'])} / -{fmt(c['down_move'])}\n"
        f"Recorrido acumulado observado: {fmt(c['traveled'])}\n"
        f"Actualizaciones recibidas: {c['sample_count']}\n"
        f"Movimientos: ↑ {c['up_steps']} | ↓ {c['down_steps']} | = {c['flat_steps']}\n\n"
        "Solo precio. Sin indicadores, S/R ni rechazo.\n🚫 Operaciones reales DESACTIVADAS."
    )


def format_window(candles: list[dict[str, Any]]) -> str:
    seq = " ".join("V" if c["color"] == "VERDE" else "R" if c["color"] == "ROJA" else "D" for c in candles)
    lines = ["📊 ULTIMAS 10 VELAS M1", "", f"Par: {PAIR_REQUESTED}", f"Secuencia: {seq}", ""]
    for i, c in enumerate(candles, 1):
        lines.append(
            f"{i:02d} {'V' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D'} "
            f"| O={fmt(c['open'])} | H={fmt(c['high'])} | L={fmt(c['low'])} | C={fmt(c['close'])} "
            f"| MI={fmt(c['lower_wick'])} | MS={fmt(c['upper_wick'])} | Body={fmt(c['body'])} "
            f"| R={fmt(c['range'])} | Body/R={c['body_ratio']*100:.2f}%"
        )
    lines += ["", "Las 10 velas se conservan para estudiar que precede a la siguiente.", "🚫 Operaciones DESACTIVADAS."]
    return "\n".join(lines)


def simulate_and_message() -> None:
    global PENDING_SIM
    if len(CLOSED) < WINDOW or PENDING_SIM is not None:
        return
    result = analyze_market(df=candle_to_df(CLOSED))
    if result.get("signal") not in ("CALL", "PUT"):
        return
    PENDING_SIM = {
        "signal": result["signal"],
        "score": int(result["score"]),
        "reason": result["reason"],
        "signal_timestamp": CLOSED[-1]["timestamp"],
    }
    STATS["signals"] += 1
    tg(
        "🧪 SEÑAL HIPOTETICA\n\n"
        f"Par: {PAIR_REQUESTED}\nDireccion: {result['signal']}\nScore: {result['score']}/9\n"
        "Entrada: apertura de la SIGUIENTE M1\nExpiracion teorica: 1 minuto\n\n"
        f"Motivos: {result['reason']}\n\n⚠️ No se envia ninguna orden real."
    )


def evaluate_pending(next_candle: dict[str, Any]) -> None:
    global PENDING_SIM
    if not PENDING_SIM:
        return
    signal = PENDING_SIM["signal"]
    entry = float(next_candle["open"])
    close = float(next_candle["close"])
    if close == entry:
        STATS["doji"] += 1; result = "⚪ DOJI"
    elif (signal == "CALL" and close > entry) or (signal == "PUT" and close < entry):
        STATS["wins"] += 1; result = "✅ FAVORABLE"
    else:
        STATS["losses"] += 1; result = "❌ CONTRARIA"
    tg(
        "🧪 RESULTADO SIMULACION\n\n"
        f"Señal: {signal}\nApertura siguiente M1: {fmt(entry)}\nCierre siguiente M1: {fmt(close)}\n"
        f"Resultado: {result}\n\n"
        f"Estadisticas: {STATS['wins']} favorables | {STATS['losses']} contrarias | {STATS['doji']} doji | {STATS['signals']} señales"
    )
    PENDING_SIM = None


def process_closed_candle(row: dict[str, Any]) -> None:
    global CLOSED
    c = finalize_candle(row, CURRENT_SAMPLES)
    CLOSED.append(c)
    CLOSED = CLOSED[-WINDOW:]
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
    current = next((c for c in candles if c["from"] == now_start), candles[-1])
    if CURRENT_START is None:
        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []
        logger.info("Inicializado con vela %s; esperando siguiente cierre", CURRENT_START)
        return
    if current["from"] != CURRENT_START:
        previous = next((c for c in candles if c["from"] == CURRENT_START), None)
        if previous is None and CURRENT_SAMPLES:
            last = CURRENT_SAMPLES[-1]
            previous = {"from": CURRENT_START, "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"]}
        if previous is not None and LAST_CLOSED_START != CURRENT_START:
            process_closed_candle(previous)
            LAST_CLOSED_START = CURRENT_START
        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []
    CURRENT_SAMPLES.append({
        "ts": int(time.time()), "open": current["open"], "high": current["high"], "low": current["low"], "close": current["close"]
    })


def main() -> None:
    if not all((IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)):
        logger.error("Faltan IQ_EMAIL/IQ_PASSWORD/TELEGRAM_TOKEN/TELEGRAM_CHAT_ID")
        return
    tg("🤖 BOT M1 SIMULACION INICIANDO\n\nPar: " + PAIR_REQUESTED + "\nOperaciones reales: DESACTIVADAS")
    try:
        if not connect():
            return
    except Exception as exc:
        logger.exception("Conexion: %s", exc)
        tg(f"❌ ERROR DE CONEXION\n\n{exc}")
        return
    while RUNNING:
        try:
            if not ensure_connection():
                time.sleep(2); continue
            if not STREAM_STARTED:
                if not start_stream():
                    time.sleep(5); continue
            process_stream_once()
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.exception("Error del loop principal: %s", exc)
            tg(f"⚠️ Error controlado; reintentando.\n\n{type(exc).__name__}: {exc}")
            stop_stream()
            time.sleep(3)
    stop_stream()


if __name__ == "__main__":
    main()
