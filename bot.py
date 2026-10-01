from __future__ import annotations

"""
bot.py - Recolector + reconocedor M1 para IQ Option.

- Un solo par solicitado por ANALYSIS_PAIR (por defecto ARBUSD-OTC).
- Solo M1.
- Solo precio/anatomia/contexto.
- Sin indicadores, S/R ni rechazo.
- OPERACIONES REALES DESACTIVADAS.
- Captura snapshots intraminuto.
- Conserva las ultimas 10 velas cerradas.
- En cada cierre ejecuta strategy.analyze_market().
- La estrategia reconoce estados y decide:
    CALL / PUT / NO SIGNAL
  para la SIGUIENTE M1.
- No se envia ninguna orden a IQ Option.

Variables:
IQ_EMAIL
IQ_PASSWORD
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
ANALYSIS_PAIR=ARBUSD-OTC
AUTO_SELECT_OTC=1
CANDLE_COUNT_M1=120
POLL_SECONDS=0.20
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


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PAIR_REQUESTED = (
    os.getenv("ANALYSIS_PAIR", "ARBUSD-OTC").strip()
    or "ARBUSD-OTC"
)

AUTO_SELECT_OTC = (
    os.getenv("AUTO_SELECT_OTC", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)

CATALOG_REFRESH_SECONDS = float(
    os.getenv("CATALOG_REFRESH_SECONDS", "60")
)

CANDLE_COUNT_M1 = max(
    WINDOW,
    int(os.getenv("CANDLE_COUNT_M1", "120"))
)

POLL_SECONDS = max(
    0.05,
    float(os.getenv("POLL_SECONDS", "0.20"))
)

# DEMO: solo se habilita con ENABLE_DEMO_TRADING=1.
# REAL queda bloqueado de forma permanente en este archivo.
DEMO_TRADING_ENABLED = (
    os.getenv("ENABLE_DEMO_TRADING", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)
DEMO_AMOUNT = float(os.getenv("AMOUNT", "150"))
DEMO_EXPIRATION = 1
MAX_DEMO_TRADES = 1
DEMO_TRADES_EXECUTED = 0
DEMO_ORDER_ID = None
DEMO_PENDING_SIGNAL = None
REAL_TRADING_ENABLED = False


# ---------------------------------------------------------------------------
# ESTADO
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("m1_reconocedor")

IQ: Optional[IQ_Option] = None

ACTIVE_PAIR = PAIR_REQUESTED
RUNNING = True
STREAM_STARTED = False

CLOSED: list[dict[str, Any]] = []

CURRENT_START: Optional[int] = None
CURRENT_SAMPLES: list[dict[str, Any]] = []

LAST_CLOSED_START: Optional[int] = None

# Prediccion pendiente. Se resuelve al cierre de la siguiente M1.
PENDING_SIM: Optional[dict[str, Any]] = None

STATS = {
    "predictions": 0,
    "wins": 0,
    "losses": 0,
    "doji": 0,
    "no_signal": 0,
}

LAST_CATALOG_CHECK = 0.0


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def tg(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            "https://api.telegram.org/bot"
            + str(TELEGRAM_TOKEN)
            + "/sendMessage",
            data={
                "chat_id": str(TELEGRAM_CHAT_ID),
                "text": msg,
            },
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Telegram: %s", exc)


# ---------------------------------------------------------------------------
# TIEMPO
# ---------------------------------------------------------------------------

def server_ts() -> float:
    try:
        return (
            float(IQ.get_server_timestamp())
            if IQ is not None
            else time.time()
        )
    except Exception:
        return time.time()


def floor_m1(ts: float) -> int:
    return int(ts // M1) * M1


# ---------------------------------------------------------------------------
# NORMALIZACION DE VELAS
# ---------------------------------------------------------------------------

def normalize_candle(
    c: Any,
    fallback_ts: Any = None,
) -> Optional[dict[str, Any]]:
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

        if row["from"] <= 0:
            return None

        if row["high"] < row["low"]:
            return None

        return row

    except (TypeError, ValueError):
        return None


def candle_to_df(
    candles: list[dict[str, Any]],
) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(
            columns=[
                "from",
                "open",
                "high",
                "low",
                "close",
            ]
        )

    # finalize_candle guarda el tiempo como "timestamp"; la estrategia
    # consume la columna "from". Normalizamos ambos formatos antes de ordenar.
    frame = pd.DataFrame(candles).copy()
    if "from" not in frame.columns and "timestamp" in frame.columns:
        frame["from"] = frame["timestamp"]
    elif "from" in frame.columns and "timestamp" in frame.columns:
        frame["from"] = frame["from"].fillna(frame["timestamp"])

    required = ["from", "open", "high", "low", "close"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        logger.warning("Velas incompletas para analisis; faltan: %s", missing)
        return pd.DataFrame(columns=required)

    frame["from"] = pd.to_numeric(frame["from"], errors="coerce")
    frame = frame.dropna(subset=required)
    return (
        frame.drop_duplicates("from")
        .sort_values("from")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# CATALOGO IQ OPTION
# ---------------------------------------------------------------------------

def _catalog_active_names() -> dict[str, int]:
    """
    Obtiene activos de la sesion actual.

    init_v2 es la fuente principal. constants.py queda solo como
    respaldo porque puede estar desactualizado.
    """
    found: dict[str, int] = {}

    if IQ is None:
        return found

    try:
        data = IQ.get_all_init_v2()

        if isinstance(data, dict):
            for market in ("binary", "turbo"):
                section = data.get(market, {})

                if not isinstance(section, dict):
                    continue

                actives = section.get("actives", {})

                if not isinstance(actives, dict):
                    continue

                for active_id, info in actives.items():
                    if not isinstance(info, dict):
                        continue

                    name = str(
                        info.get("name", "")
                    ).strip()

                    if not name:
                        continue

                    try:
                        aid = int(active_id)
                    except (TypeError, ValueError):
                        continue

                    variants = {
                        name,
                        name.replace("_OTC", "-OTC"),
                        name.replace("-OTC", "_OTC"),
                        name.split(".", 1)[-1],
                    }

                    for variant in variants:
                        if variant:
                            found[variant] = aid

    except Exception as exc:
        logger.warning(
            "Catalogo init_v2: %s",
            exc,
        )

    # Fallback estatico.
    try:
        for name, aid in OP_code.ACTIVES.items():
            try:
                found.setdefault(
                    str(name),
                    int(aid),
                )
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    return found


def discover_otc_pairs() -> list[str]:
    names = _catalog_active_names()

    pairs: set[str] = set()

    for name in names:
        upper = name.upper()

        if (
            upper.endswith("-OTC")
            or upper.endswith("_OTC")
        ):
            pairs.add(
                name.replace("_OTC", "-OTC")
            )

    return sorted(pairs)


def resolve_active(pair: str) -> Optional[int]:
    global LAST_CATALOG_CHECK

    if IQ is None:
        return None

    try:
        names = _catalog_active_names()
        LAST_CATALOG_CHECK = time.time()

        variants = (
            pair,
            pair.replace("-OTC", "_OTC"),
            pair.replace("_OTC", "-OTC"),
        )

        for candidate in variants:
            if candidate in names:
                aid = int(names[candidate])

                OP_code.ACTIVES[pair] = aid
                OP_code.ACTIVES[candidate] = aid

                logger.info(
                    "Activo resuelto: %s -> %s",
                    pair,
                    aid,
                )

                return aid

        target = pair.upper().replace(
            "_OTC",
            "-OTC",
        )

        for name, aid in names.items():
            canonical = name.upper().replace(
                "_OTC",
                "-OTC",
            )

            if canonical.endswith(target):
                OP_code.ACTIVES[pair] = int(aid)

                logger.info(
                    "Activo resuelto por coincidencia: "
                    "%s -> %s (%s)",
                    pair,
                    aid,
                    name,
                )

                return int(aid)

    except Exception as exc:
        logger.warning(
            "Resolucion de activo: %s",
            exc,
        )

    logger.warning(
        "Activo no encontrado: %s",
        pair,
    )

    return None


def choose_otc_pair() -> Optional[str]:
    requested = PAIR_REQUESTED

    if resolve_active(requested) is not None:
        return requested

    if not AUTO_SELECT_OTC:
        return None

    pairs = discover_otc_pairs()

    if not pairs:
        return None

    requested_base = (
        requested.upper()
        .replace("-OTC", "")
        .replace("_OTC", "")
    )

    preferred = [
        p
        for p in pairs
        if (
            p.upper()
            .replace("-OTC", "")
            .replace("_OTC", "")
            == requested_base
        )
    ]

    return preferred[0] if preferred else pairs[0]


# ---------------------------------------------------------------------------
# CONEXION
# ---------------------------------------------------------------------------

def connect() -> bool:
    global IQ, ACTIVE_PAIR

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    ok, reason = IQ.connect()

    if not ok:
        raise ConnectionError(reason)

    logger.info("Conectado a IQ Option")

    selected = choose_otc_pair()

    if selected is None:
        available = discover_otc_pairs()

        preview = (
            ", ".join(available[:12])
            if available
            else "ninguno"
        )

        tg(
            "❌ PAR OTC NO DISPONIBLE\n\n"
            f"Solicitado: {PAIR_REQUESTED}\n"
            f"OTC detectados: {preview}\n\n"
            "El bot reintentara."
        )

        return False

    ACTIVE_PAIR = selected

    if ACTIVE_PAIR != PAIR_REQUESTED:
        tg(
            "ℹ️ PAR SOLICITADO NO DISPONIBLE\n\n"
            f"Solicitado: {PAIR_REQUESTED}\n"
            f"Seleccionado: {ACTIVE_PAIR}\n\n"
            "Solo se analizara este par."
        )

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

        if isinstance(result, tuple):
            return bool(result[0])

        return bool(result)

    except Exception as exc:
        logger.warning(
            "Reconectar: %s",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# STREAM
# ---------------------------------------------------------------------------

def start_stream() -> bool:
    global STREAM_STARTED

    if not ensure_connection():
        return False

    if resolve_active(ACTIVE_PAIR) is None:
        return False

    try:
        IQ.start_candles_stream(
            ACTIVE_PAIR,
            M1,
            CANDLE_COUNT_M1,
        )

        STREAM_STARTED = True

        logger.info(
            "Stream iniciado: %s M1",
            ACTIVE_PAIR,
        )

        tg(
            "🟢 STREAM M1 INICIADO\n\n"
            f"Par: {ACTIVE_PAIR}\n"
            "Modo: RECONOCIMIENTO + SIMULACION\n"
            "Operaciones reales: DESACTIVADAS\n\n"
            "Esperando cierres..."
        )

        return True

    except Exception as exc:
        STREAM_STARTED = False

        logger.exception(
            "No se pudo iniciar stream: %s",
            exc,
        )

        tg(
            "⚠️ FALLO STREAM M1\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "Se reintentara."
        )

        return False


def stop_stream() -> None:
    global STREAM_STARTED

    if not STREAM_STARTED or IQ is None:
        return

    try:
        IQ.stop_candles_stream(
            ACTIVE_PAIR,
            M1,
        )
    except Exception:
        pass

    STREAM_STARTED = False


def get_stream_candles() -> list[dict[str, Any]]:
    if IQ is None or not STREAM_STARTED:
        return []

    try:
        raw = IQ.get_realtime_candles(
            ACTIVE_PAIR,
            M1,
        )
    except Exception as exc:
        logger.warning(
            "Realtime candles: %s",
            exc,
        )
        return []

    if not isinstance(raw, dict):
        return []

    result: list[dict[str, Any]] = []

    for key, value in raw.items():
        candle = normalize_candle(
            value,
            key,
        )

        if candle is not None:
            result.append(candle)

    return sorted(
        result,
        key=lambda x: x["from"],
    )


# ---------------------------------------------------------------------------
# ANATOMIA / INTRAMINUTO
# ---------------------------------------------------------------------------

def candle_metrics(
    row: pd.Series,
) -> dict[str, Any]:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    rng = max(
        h - l,
        1e-12,
    )

    body = abs(c - o)

    upper = max(
        0.0,
        h - max(o, c),
    )

    lower = max(
        0.0,
        min(o, c) - l,
    )

    color = (
        "VERDE"
        if c > o
        else "ROJA"
        if c < o
        else "DOJI"
    )

    return {
        "timestamp": int(row["from"]),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "body": body,
        "range": rng,
        "upper_wick": upper,
        "lower_wick": lower,
        "body_ratio": body / rng,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        "close_pos": (c - l) / rng,
        "color": color,
    }


def finalize_candle(
    row: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    m = candle_metrics(
        pd.Series(row)
    )

    prices = [
        float(s["close"])
        for s in samples
        if "close" in s
    ]

    if prices:
        first = prices[0]
        last = prices[-1]
    else:
        first = m["open"]
        last = m["close"]

    up_move = max(
        [
            p - m["open"]
            for p in prices
        ]
        + [m["high"] - m["open"], 0.0]
    )

    down_move = max(
        [
            m["open"] - p
            for p in prices
        ]
        + [m["open"] - m["low"], 0.0]
    )

    traveled = sum(
        abs(b - a)
        for a, b in zip(
            prices,
            prices[1:],
        )
    )

    up_steps = sum(
        b > a
        for a, b in zip(
            prices,
            prices[1:],
        )
    )

    down_steps = sum(
        b < a
        for a, b in zip(
            prices,
            prices[1:],
        )
    )

    flat_steps = sum(
        b == a
        for a, b in zip(
            prices,
            prices[1:],
        )
    )

    return {
        **m,
        "sample_count": len(samples),
        "up_move": up_move,
        "down_move": down_move,
        "traveled": traveled,
        "up_steps": up_steps,
        "down_steps": down_steps,
        "flat_steps": flat_steps,
        "first_sample": first,
        "last_sample": last,
    }


# ---------------------------------------------------------------------------
# FORMATO TELEGRAM
# ---------------------------------------------------------------------------

def fmt(x: Any) -> str:
    try:
        return f"{float(x):.8f}"
    except Exception:
        return "-"


def format_candle_message(
    c: dict[str, Any],
) -> str:
    return (
        "🕯️ VELA M1 CERRADA\n\n"
        f"Par: {ACTIVE_PAIR}\n"
        f"Timestamp: {c['timestamp']}\n"
        f"Color: {c['color']}\n\n"
        f"Apertura: {fmt(c['open'])}\n"
        f"Maximo: {fmt(c['high'])}\n"
        f"Minimo: {fmt(c['low'])}\n"
        f"Cierre: {fmt(c['close'])}\n\n"
        f"Mecha inferior: {fmt(c['lower_wick'])}\n"
        f"Mecha superior: {fmt(c['upper_wick'])}\n"
        f"Body: {fmt(c['body'])}\n"
        f"Rango: {fmt(c['range'])}\n"
        f"Body/R: {c['body_ratio'] * 100:.2f}%\n\n"
        f"Movimiento desde apertura: "
        f"+{fmt(c['up_move'])} / "
        f"-{fmt(c['down_move'])}\n"
        f"Recorrido acumulado observado: "
        f"{fmt(c['traveled'])}\n"
        f"Actualizaciones recibidas: "
        f"{c['sample_count']}\n"
        f"Movimientos: ↑ {c['up_steps']} | "
        f"↓ {c['down_steps']} | "
        f"= {c['flat_steps']}\n\n"
        "Solo precio. Sin indicadores, S/R ni rechazo.\n"
        "🚫 Operaciones reales DESACTIVADAS."
    )


def format_window(
    candles: list[dict[str, Any]],
) -> str:
    seq = " ".join(
        "V"
        if c["color"] == "VERDE"
        else "R"
        if c["color"] == "ROJA"
        else "D"
        for c in candles
    )

    lines = [
        "📊 ULTIMAS 10 VELAS M1",
        "",
        f"Par: {ACTIVE_PAIR}",
        f"Secuencia: {seq}",
        "",
    ]

    for i, c in enumerate(
        candles,
        1,
    ):
        color = (
            "V"
            if c["color"] == "VERDE"
            else "R"
            if c["color"] == "ROJA"
            else "D"
        )

        lines.append(
            f"{i:02d} {color} | "
            f"O={fmt(c['open'])} | "
            f"H={fmt(c['high'])} | "
            f"L={fmt(c['low'])} | "
            f"C={fmt(c['close'])} | "
            f"MI={fmt(c['lower_wick'])} | "
            f"MS={fmt(c['upper_wick'])} | "
            f"Body={fmt(c['body'])} | "
            f"R={fmt(c['range'])} | "
            f"Body/R={c['body_ratio'] * 100:.2f}%"
        )

    lines += [
        "",
        "Las 10 velas se conservan para el reconocimiento.",
        "🚫 Operaciones DESACTIVADAS.",
    ]

    return "\n".join(lines)


def format_analysis(
    result: dict[str, Any],
) -> str:
    prediction = result.get(
        "prediction",
        "NO SIGNAL",
    )

    state = result.get(
        "state",
        "SIN_HISTORIAL",
    )

    direction = result.get(
        "state_direction",
        "NEUTRAL",
    )

    quality = result.get(
        "state_quality",
        "BAJA",
    )

    pred_quality = result.get(
        "prediction_quality",
        "BAJA",
    )

    score = result.get(
        "score",
        0,
    )

    evidence = result.get(
        "evidence",
        [],
    )

    intrabar = result.get(
        "intrabar",
        {},
    )

    intrabar_quality = intrabar.get(
        "quality",
        "NO_DISPONIBLE",
    )

    intrabar_direction = intrabar.get(
        "direction",
        "NEUTRAL",
    )

    samples = intrabar.get(
        "samples",
        0,
    )

    reason = result.get(
        "prediction_reason",
        result.get("reason", ""),
    )

    if prediction == "CALL":
        pred_text = "🟢 CALL"
    elif prediction == "PUT":
        pred_text = "🔴 PUT"
    else:
        pred_text = "⚪ NO SIGNAL"

    evidence_text = (
        "\n".join(
            f"• {item}"
            for item in evidence
        )
        if evidence
        else "• Sin evidencia suficiente"
    )

    return (
        "🧠 RECONOCIMIENTO M1\n\n"
        f"Par: {ACTIVE_PAIR}\n"
        f"Estado: {state}\n"
        f"Direccion del estado: {direction}\n"
        f"Calidad del estado: {quality}\n"
        f"Score estructural: {score}/3\n\n"
        "Evidencia:\n"
        f"{evidence_text}\n\n"
        "INTRAMINUTO\n"
        f"Calidad: {intrabar_quality}\n"
        f"Direccion: {intrabar_direction}\n"
        f"Actualizaciones: {samples}\n\n"
        "PREDICCION SIGUIENTE M1\n"
        f"{pred_text}\n"
        f"Calidad prediccion: {pred_quality}\n"
        f"Razon: {reason}\n\n"
        "Entrada teorica: apertura siguiente M1\n"
        "Expiracion teorica: 1 minuto\n"
        "🚫 Ninguna orden real sera enviada."
    )


# ---------------------------------------------------------------------------
# EJECUCION DEMO
# ---------------------------------------------------------------------------

def configure_demo_account() -> bool:
    if not DEMO_TRADING_ENABLED:
        return False
    if IQ is None:
        return False
    try:
        IQ.change_balance("PRACTICE")
        mode_fn = getattr(IQ, "get_balance_mode", None)
        if callable(mode_fn):
            mode = mode_fn()
            if mode and str(mode).upper() != "PRACTICE":
                raise RuntimeError(
                    f"La cuenta activa no es PRACTICE: {mode}"
                )
        tg(
            "🟢 DEMO HABILITADO\n\n"
            "Cuenta: PRACTICE\n"
            f"Importe: {DEMO_AMOUNT:.2f}\n"
            "Expiracion: 1 minuto\n"
            "Limite: 1 entrada"
        )
        return True
    except Exception as exc:
        logger.exception("Configurar PRACTICE: %s", exc)
        tg(
            "❌ DEMO NO HABILITADO\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "No se enviara ninguna orden."
        )
        return False


def execute_demo_entry() -> None:
    global DEMO_TRADES_EXECUTED, DEMO_ORDER_ID
    global DEMO_PENDING_SIGNAL

    if not DEMO_TRADING_ENABLED:
        return
    if DEMO_TRADES_EXECUTED >= MAX_DEMO_TRADES:
        return
    if DEMO_PENDING_SIGNAL not in {"CALL", "PUT"}:
        return
    if IQ is None:
        return

    action = DEMO_PENDING_SIGNAL.lower()
    signal = DEMO_PENDING_SIGNAL

    try:
        # Ultimo seguro: cambiar nuevamente a PRACTICE justo antes de comprar.
        IQ.change_balance("PRACTICE")
        mode_fn = getattr(IQ, "get_balance_mode", None)
        if callable(mode_fn):
            mode = mode_fn()
            if mode and str(mode).upper() != "PRACTICE":
                raise RuntimeError(
                    f"Bloqueo de seguridad: cuenta activa {mode}"
                )

        ok, order_id = IQ.buy(
            DEMO_AMOUNT,
            ACTIVE_PAIR,
            action,
            DEMO_EXPIRATION,
        )

        if not ok:
            tg(
                "❌ ORDEN DEMO RECHAZADA\n\n"
                f"Par: {ACTIVE_PAIR}\n"
                f"Direccion: {signal}\n"
                f"Importe: {DEMO_AMOUNT:.2f}\n"
                f"Respuesta: {order_id}\n\n"
                "No se contara como entrada ejecutada."
            )
            return

        DEMO_ORDER_ID = order_id
        DEMO_TRADES_EXECUTED += 1

        tg(
            "🚨 DEMO ENTRY EJECUTADA\n\n"
            f"Par: {ACTIVE_PAIR}\n"
            f"Direccion: {signal}\n"
            f"Importe: {DEMO_AMOUNT:.2f}\n"
            "Expiracion: 1 minuto\n"
            f"ID: {order_id}\n\n"
            "Cuenta: PRACTICE\n"
            "Entradas demo ejecutadas: "
            f"{DEMO_TRADES_EXECUTED}/{MAX_DEMO_TRADES}"
        )
    except Exception as exc:
        logger.exception("Orden DEMO: %s", exc)
        tg(
            "❌ ERROR ORDEN DEMO\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "No se contara como ejecutada."
        )
    finally:
        DEMO_PENDING_SIGNAL = None


def check_demo_result() -> None:
    if not DEMO_TRADING_ENABLED or DEMO_ORDER_ID is None:
        return
    try:
        result = IQ.check_win_v3(DEMO_ORDER_ID)
        tg(
            "🏁 RESULTADO ORDEN DEMO\n\n"
            f"ID: {DEMO_ORDER_ID}\n"
            f"Resultado API: {result}"
        )
    except Exception as exc:
        logger.warning("Resultado DEMO: %s", exc)


# ---------------------------------------------------------------------------
# SIMULACION / VALIDACION
# ---------------------------------------------------------------------------

def analyze_and_message() -> None:
    global PENDING_SIM

    if len(CLOSED) < WINDOW:
        return

    # No sustituimos una prediccion pendiente hasta resolverla.
    if PENDING_SIM is not None:
        return

    result = analyze_market(
        df=candle_to_df(CLOSED)
    )

    tg(
        format_analysis(result)
    )

    prediction = result.get(
        "prediction",
        "NO SIGNAL",
    )

    if prediction not in {"CALL", "PUT"}:
        STATS["no_signal"] += 1
        return

    PENDING_SIM = {
        "signal": prediction,
        "state": result.get("state"),
        "quality": result.get(
            "prediction_quality",
            "BAJA",
        ),
        "score": result.get(
            "score",
            0,
        ),
        "reason": result.get(
            "prediction_reason",
            "",
        ),
        "signal_timestamp": CLOSED[-1][
            "timestamp"
        ],
    }

    STATS["predictions"] += 1

    # La orden DEMO se ejecuta en la apertura detectada de la siguiente M1.
    global DEMO_PENDING_SIGNAL
    if DEMO_TRADING_ENABLED and DEMO_TRADES_EXECUTED < MAX_DEMO_TRADES:
        DEMO_PENDING_SIGNAL = prediction


def evaluate_pending(
    next_candle: dict[str, Any],
) -> None:
    global PENDING_SIM

    if PENDING_SIM is None:
        return

    signal = PENDING_SIM["signal"]

    entry = float(
        next_candle["open"]
    )

    close = float(
        next_candle["close"]
    )

    if close == entry:
        STATS["doji"] += 1
        outcome = "⚪ DOJI"

    elif (
        signal == "CALL"
        and close > entry
    ) or (
        signal == "PUT"
        and close < entry
    ):
        STATS["wins"] += 1
        outcome = "✅ FAVORABLE"

    else:
        STATS["losses"] += 1
        outcome = "❌ CONTRARIA"

    tg(
        "🧪 RESULTADO PREDICCION\n\n"
        f"Prediccion: {signal}\n"
        f"Estado anterior: "
        f"{PENDING_SIM.get('state')}\n"
        f"Apertura siguiente M1: "
        f"{fmt(entry)}\n"
        f"Cierre siguiente M1: "
        f"{fmt(close)}\n\n"
        f"Resultado: {outcome}\n\n"
        f"Predicciones direccionales: "
        f"{STATS['predictions']}\n"
        f"Favorables: {STATS['wins']}\n"
        f"Contrarias: {STATS['losses']}\n"
        f"Doji: {STATS['doji']}\n"
        f"No signal: {STATS['no_signal']}\n\n"
        "🚫 Solo simulacion."
    )

    PENDING_SIM = None


# ---------------------------------------------------------------------------
# PROCESAMIENTO DE CIERRES
# ---------------------------------------------------------------------------

def process_closed_candle(
    row: dict[str, Any],
) -> None:
    global CLOSED

    candle = finalize_candle(
        row,
        CURRENT_SAMPLES,
    )

    # Primero se resuelve la prediccion que se hizo con la vela anterior.
    evaluate_pending(candle)

    CLOSED.append(candle)

    # Conservamos solo las ultimas 10.
    CLOSED = CLOSED[-WINDOW:]

    # Mensaje individual de la vela cerrada.
    tg(
        format_candle_message(candle)
    )

    # Cuando tenemos 10 velas, mostramos contexto y hacemos la nueva
    # prediccion para la siguiente M1.
    if len(CLOSED) == WINDOW:
        tg(
            format_window(CLOSED)
        )

        analyze_and_message()


# ---------------------------------------------------------------------------
# LOOP DEL STREAM
# ---------------------------------------------------------------------------

def process_stream_once() -> None:
    global CURRENT_START
    global CURRENT_SAMPLES
    global LAST_CLOSED_START

    candles = get_stream_candles()

    if not candles:
        return

    now_start = floor_m1(
        server_ts()
    )

    current = next(
        (
            c
            for c in candles
            if c["from"] == now_start
        ),
        candles[-1],
    )

    if CURRENT_START is None:
        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []

        logger.info(
            "Inicializado con vela %s; "
            "esperando cierre.",
            CURRENT_START,
        )

        return

    # Cambio de vela: la anterior acaba de cerrar.
    if current["from"] != CURRENT_START:
        previous = next(
            (
                c
                for c in candles
                if c["from"] == CURRENT_START
            ),
            None,
        )

        # Fallback con el ultimo snapshot recibido.
        if previous is None and CURRENT_SAMPLES:
            last = CURRENT_SAMPLES[-1]

            previous = {
                "from": CURRENT_START,
                "open": last["open"],
                "high": last["high"],
                "low": last["low"],
                "close": last["close"],
            }

        if (
            previous is not None
            and LAST_CLOSED_START != CURRENT_START
        ):
            process_closed_candle(
                previous
            )

            # Ya cambio la M1: este es el punto de entrada de la siguiente vela.
            execute_demo_entry()

            LAST_CLOSED_START = CURRENT_START

        CURRENT_START = current["from"]
        CURRENT_SAMPLES = []

    # Snapshot intraminuto.
    CURRENT_SAMPLES.append(
        {
            "ts": int(time.time()),
            "open": current["open"],
            "high": current["high"],
            "low": current["low"],
            "close": current["close"],
        }
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not all(
        (
            IQ_EMAIL,
            IQ_PASSWORD,
            TELEGRAM_TOKEN,
            TELEGRAM_CHAT_ID,
        )
    ):
        logger.error(
            "Faltan IQ_EMAIL, IQ_PASSWORD, "
            "TELEGRAM_TOKEN o TELEGRAM_CHAT_ID."
        )
        return

    tg(
        "🤖 BOT M1 RECONOCEDOR INICIANDO\n\n"
        f"Par solicitado: {PAIR_REQUESTED}\n"
        "Modo: reconocimiento + DEMO opcional\n"
        "Operaciones reales: BLOQUEADAS\n"
        f"Demo: {'ACTIVADA' if DEMO_TRADING_ENABLED else 'DESACTIVADA'}\n\n"
        "La estrategia reconoce estado, "
        "contexto, confirmacion y espera "
        "cuando no existe evidencia suficiente."
    )

    try:
        if not connect():
            return

        if DEMO_TRADING_ENABLED and not configure_demo_account():
            return

    except Exception as exc:
        logger.exception(
            "Conexion: %s",
            exc,
        )

        tg(
            "❌ ERROR DE CONEXION\n\n"
            f"{type(exc).__name__}: {exc}"
        )

        return

    while RUNNING:
        try:
            if not ensure_connection():
                time.sleep(2)
                continue

            if not STREAM_STARTED:
                if not start_stream():
                    time.sleep(10)
                    continue

            process_stream_once()

            time.sleep(
                POLL_SECONDS
            )

        except KeyboardInterrupt:
            break

        except Exception as exc:
            logger.exception(
                "Error loop principal: %s",
                exc,
            )

            tg(
                "⚠️ ERROR CONTROLADO\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "Reintentando."
            )

            stop_stream()
            time.sleep(3)

    stop_stream()


if __name__ == "__main__":
    main()
