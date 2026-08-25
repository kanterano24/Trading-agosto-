from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
from strategy import (
    analyze_live_candle,
    analyze_market,
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# TEMPORALIDADES
# ============================================================

TIMEFRAME = 60

# Máximo de velas utilizadas.
CANDLE_COUNT = 62


# ============================================================
# OPERACIÓN
# ============================================================

AMOUNT = 116
EXPIRATION = 1


# ============================================================
# EJECUCIÓN
# ============================================================

POLL_INTERVAL = 0.05

# Entrada solamente en N+1.
MAX_ENTRY_DELAY = 5


# ============================================================
# SELECCIÓN DEL MERCADO
# ============================================================

MIN_MARKET_SCORE = 82

# Número máximo de candidatos para registrar.
TOP_MARKETS_TO_LOG = 5

# Actualizar activos disponibles cada cierto tiempo.
ASSET_REFRESH_INTERVAL = 60


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_POLL_INTERVAL = 1.0
TELEGRAM_HTTP_TIMEOUT = 3.0


# ============================================================
# ESTADO GLOBAL
# ============================================================

BOT_RUNNING = False

IQ: Optional[IQ_Option] = None

LAST_UPDATE_ID: Optional[int] = None

AVAILABLE_OTC_PAIRS: List[str] = []

LAST_ASSET_REFRESH = 0.0

STREAMS_STARTED_FOR: Dict[str, bool] = {}

LAST_PROCESSED_MINUTE: Optional[int] = None

PENDING_ENTRY: Optional[Dict[str, Any]] = None

LAST_TRADE_CANDLE: Optional[int] = None

LIVE_M1_STATE: Dict[
    str,
    Dict[str, Any],
] = {}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(
    message: str,
) -> bool:

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=TELEGRAM_HTTP_TIMEOUT,
        )

        return response.status_code == 200

    except Exception as exc:

        logger.warning(
            "Telegram no disponible: %s",
            exc,
        )

        return False


# ============================================================
# TELEGRAM WORKER
# ============================================================

def telegram_worker() -> None:

    global LAST_UPDATE_ID
    global BOT_RUNNING

    if not TELEGRAM_TOKEN:
        return

    logger.info(
        "Telegram worker iniciado."
    )

    while True:

        try:

            url = (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/getUpdates"
            )

            params: Dict[str, Any] = {
                "timeout": 0,
            }

            if LAST_UPDATE_ID is not None:

                params["offset"] = (
                    LAST_UPDATE_ID + 1
                )

            response = requests.get(
                url,
                params=params,
                timeout=TELEGRAM_HTTP_TIMEOUT,
            )

            if response.status_code != 200:

                time.sleep(
                    TELEGRAM_POLL_INTERVAL
                )

                continue

            data = response.json()

            if not data.get("ok"):

                time.sleep(
                    TELEGRAM_POLL_INTERVAL
                )

                continue

            for update in data.get(
                "result",
                [],
            ):

                LAST_UPDATE_ID = (
                    update.get("update_id")
                )

                message = update.get(
                    "message",
                    {},
                )

                text = str(
                    message.get(
                        "text",
                        "",
                    )
                ).strip().lower()

                chat_id = str(
                    message.get(
                        "chat",
                        {},
                    ).get(
                        "id",
                        "",
                    )
                )

                if (
                    chat_id
                    != str(TELEGRAM_CHAT_ID)
                ):
                    continue

                if text == "/start":

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "MODO MULTI-OTC\n"
                        "Analizando mercados OTC disponibles.\n"
                        "Buscando la mejor estructura.\n"
                        "Operación solo en el mejor candidato."
                    )

                    logger.info(
                        "BOT ACTIVADO"
                    )

                elif text == "/stop":

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO\n\n"
                        "No se abrirán nuevas operaciones."
                    )

                elif text == "/status":

                    status = (
                        "🟢 ACTIVO"
                        if BOT_RUNNING
                        else "🔴 DETENIDO"
                    )

                    pending_text = (
                        "Sí"
                        if PENDING_ENTRY
                        else "No"
                    )

                    telegram_send(
                        "📊 ESTADO\n\n"
                        f"Estado: {status}\n"
                        "Modo: MULTI-OTC\n"
                        "Estrategia: Continuidad\n"
                        f"OTC disponibles: "
                        f"{len(AVAILABLE_OTC_PAIRS)}\n"
                        f"Importe: ${AMOUNT}\n"
                        f"Expiración: "
                        f"{EXPIRATION} minuto\n"
                        f"Señal pendiente: "
                        f"{pending_text}\n"
                        "Entrada: N+1 segundos 00-05"
                    )

        except Exception as exc:

            logger.warning(
                "Telegram worker: %s",
                exc,
            )

        time.sleep(
            TELEGRAM_POLL_INTERVAL
        )


# ============================================================
# TIMESTAMP DEL SERVIDOR
# ============================================================

def get_server_timestamp() -> Optional[int]:

    if IQ is None:
        return None

    try:

        timestamp = (
            IQ.get_server_timestamp()
        )

        if timestamp is None:
            return None

        return int(
            float(timestamp)
        )

    except Exception as exc:

        logger.warning(
            "Error timestamp servidor: %s",
            exc,
        )

        return None


# ============================================================
# CONEXIÓN IQ OPTION
# ============================================================

def connect_iq() -> bool:

    global IQ

    if not IQ_EMAIL:
        raise ValueError(
            "Falta IQ_EMAIL"
        )

    if not IQ_PASSWORD:
        raise ValueError(
            "Falta IQ_PASSWORD"
        )

    logger.info(
        "Conectando a IQ Option..."
    )

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    connected, reason = IQ.connect()

    if not connected:

        raise ConnectionError(
            f"No se pudo conectar: {reason}"
        )

    logger.info(
        "IQ Option conectado."
    )

    refresh_otc_assets(
        force=True
    )

    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "Modo MULTI-OTC\n"
        "Buscando automáticamente los OTC disponibles."
    )

    return True


def ensure_connection() -> bool:

    global IQ

    try:

        if IQ is None:
            return connect_iq()

        if IQ.check_connect():
            return True

        logger.warning(
            "Conexión perdida. Reconectando..."
        )

        connected, reason = IQ.connect()

        if not connected:

            logger.error(
                "Reconexión fallida: %s",
                reason,
            )

            return False

        refresh_otc_assets(
            force=True
        )

        telegram_send(
            "🟢 IQ OPTION RECONECTADO"
        )

        return True

    except Exception as exc:

        logger.error(
            "Error conexión IQ: %s",
            exc,
        )

        return False


# ============================================================
# DESCUBRIR TODOS LOS OTC DISPONIBLES
# ============================================================

def is_asset_usable(
    active: Dict[str, Any],
) -> bool:

    try:

        if not isinstance(
            active,
            dict,
        ):
            return False

        if not bool(
            active.get(
                "enabled",
                False,
            )
        ):
            return False

        if bool(
            active.get(
                "is_suspended",
                False,
            )
        ):
            return False

        return True

    except Exception:

        return False


def extract_symbol(
    active: Dict[str, Any],
) -> Optional[str]:

    try:

        raw_name = str(
            active.get(
                "name",
                "",
            )
        )

        if not raw_name:
            return None

        if "." in raw_name:

            return raw_name.split(
                ".",
                1,
            )[1]

        return raw_name

    except Exception:

        return None


def discover_otc_assets() -> List[str]:

    if IQ is None:
        return []

    found = set()

    try:

        init_data = (
            IQ.get_all_init_v2()
        )

        if not isinstance(
            init_data,
            dict,
        ):

            logger.warning(
                "get_all_init_v2() inválido."
            )

            return []

        for option_type in (
            "binary",
            "turbo",
        ):

            option_data = (
                init_data.get(
                    option_type,
                    {},
                )
            )

            if not isinstance(
                option_data,
                dict,
            ):
                continue

            actives = (
                option_data.get(
                    "actives",
                    {},
                )
            )

            if not isinstance(
                actives,
                dict,
            ):
                continue

            for active in actives.values():

                if not is_asset_usable(
                    active
                ):
                    continue

                symbol = extract_symbol(
                    active
                )

                if not symbol:
                    continue

                if (
                    "-OTC"
                    not in symbol.upper()
                ):
                    continue

                found.add(
                    symbol
                )

    except Exception as exc:

        logger.warning(
            "Error descubriendo OTC: %s",
            exc,
        )

    return sorted(found)


def refresh_otc_assets(
    force: bool = False,
) -> None:

    global AVAILABLE_OTC_PAIRS
    global LAST_ASSET_REFRESH

    now = time.time()

    if (
        not force
        and now - LAST_ASSET_REFRESH
        < ASSET_REFRESH_INTERVAL
    ):
        return

    pairs = discover_otc_assets()

    if pairs:

        previous = set(
            AVAILABLE_OTC_PAIRS
        )

        current = set(pairs)

        added = current - previous
        removed = previous - current

        AVAILABLE_OTC_PAIRS = pairs

        if added:

            logger.info(
                "OTC añadidos: %s",
                ", ".join(
                    sorted(added)
                ),
            )

        if removed:

            logger.info(
                "OTC eliminados: %s",
                ", ".join(
                    sorted(removed)
                ),
            )

        logger.info(
            "OTC disponibles: %s",
            len(
                AVAILABLE_OTC_PAIRS
            ),
        )

    LAST_ASSET_REFRESH = now


# ============================================================
# STREAM DE VELAS
# ============================================================

def ensure_pair_stream(
    pair: str,
) -> bool:

    if IQ is None:
        return False

    if STREAMS_STARTED_FOR.get(
        pair,
        False,
    ):
        return True

    try:

        IQ.start_candles_stream(
            pair,
            TIMEFRAME,
            CANDLE_COUNT,
        )

        STREAMS_STARTED_FOR[pair] = True

        logger.info(
            "%s | stream M1 iniciado",
            pair,
        )

        return True

    except Exception as exc:

        logger.warning(
            "%s | error iniciando stream: %s",
            pair,
            exc,
        )

        return False


# ============================================================
# DATAFRAME REALTIME
# ============================================================

def realtime_dataframe(
    pair: str,
) -> Optional[pd.DataFrame]:

    if IQ is None:
        return None

    try:

        candles = (
            IQ.get_realtime_candles(
                pair,
                TIMEFRAME,
            )
        )

        if not candles:
            return None

        rows = []

        for timestamp, candle in candles.items():

            try:

                rows.append(
                    {
                        "from": int(
                            float(timestamp)
                        ),
                        "open": float(
                            candle["open"]
                        ),
                        "close": float(
                            candle["close"]
                        ),
                        "high": float(
                            candle.get(
                                "max",
                                candle.get("high"),
                            )
                        ),
                        "low": float(
                            candle.get(
                                "min",
                                candle.get("low"),
                            )
                        ),
                        "volume": float(
                            candle.get(
                                "volume",
                                0,
                            )
                        ),
                    }
                )

            except Exception:
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)

        df.sort_values(
            "from",
            inplace=True,
        )

        df.drop_duplicates(
            subset=["from"],
            keep="last",
            inplace=True,
        )

        df.reset_index(
            drop=True,
            inplace=True,
        )

        return df.tail(
            CANDLE_COUNT
        ).copy()

    except Exception as exc:

        logger.warning(
            "%s | realtime error: %s",
            pair,
            exc,
        )

        return None


# ============================================================
# ANALIZAR VELA VIVA
# ============================================================

def monitor_live_market(
    pair: str,
    df: pd.DataFrame,
    server_ts: int,
) -> Optional[Dict[str, Any]]:

    if df is None or len(df) == 0:
        return None

    try:

        live = df.iloc[-1]

        live_ts = int(
            live["from"]
        )

        current_minute = (
            int(server_ts)
            // TIMEFRAME
        ) * TIMEFRAME

        if live_ts != current_minute:
            return None

        live_analysis = (
            analyze_live_candle(
                live
            )
        )

        elapsed = int(
            server_ts - live_ts
        )

        previous = LIVE_M1_STATE.get(
            pair
        )

        if (
            previous is None
            or previous.get(
                "timestamp"
            ) != live_ts
        ):

            LIVE_M1_STATE[pair] = {
                "timestamp": live_ts,
                "last_second": elapsed,
                "analysis": live_analysis,
            }

        else:

            previous[
                "last_second"
            ] = elapsed

            previous[
                "analysis"
            ] = live_analysis

        return live_analysis

    except Exception:

        logger.exception(
            "%s | error monitoreo live",
            pair,
        )

        return None


# ============================================================
# OBTENER VELA CERRADA
# ============================================================

def get_closed_candle(
    df: pd.DataFrame,
    server_ts: int,
) -> Optional[pd.Series]:

    if df is None or len(df) < 2:
        return None

    current_minute = (
        int(server_ts)
        // TIMEFRAME
    ) * TIMEFRAME

    candidates = df[
        df["from"] < current_minute
    ]

    if len(candidates) == 0:
        return None

    return candidates.iloc[-1]


# ============================================================
# ANALIZAR UN PAR AL CIERRE
# ============================================================

def analyze_pair_closed(
    pair: str,
    df: pd.DataFrame,
    closed_candle: pd.Series,
) -> Dict[str, Any]:

    closed_ts = int(
        closed_candle["from"]
    )

    history = df[
        df["from"] <= closed_ts
    ].copy()

    result = analyze_market(
        closed_candle,
        previous_m1=history,
    )

    result["pair"] = pair
    result["minute_timestamp"] = closed_ts

    return result


# ============================================================
# ANALIZAR TODOS LOS MERCADOS
# ============================================================

def analyze_all_markets(
    server_ts: int,
) -> List[Dict[str, Any]]:

    candidates: List[
        Dict[str, Any]
    ] = []

    for pair in AVAILABLE_OTC_PAIRS:

        if not BOT_RUNNING:
            break

        try:

            if not ensure_pair_stream(
                pair
            ):
                continue

            df = realtime_dataframe(
                pair
            )

            if df is None:
                continue

            if len(df) < 10:
                continue

            # ------------------------------------------------
            # ANALIZAR LA VELA QUE ESTÁ VIVA
            # ------------------------------------------------

            monitor_live_market(
                pair,
                df,
                server_ts,
            )

            # ------------------------------------------------
            # OBTENER LA ÚLTIMA VELA CERRADA
            # ------------------------------------------------

            closed_candle = (
                get_closed_candle(
                    df,
                    server_ts,
                )
            )

            if closed_candle is None:
                continue

            result = analyze_pair_closed(
                pair,
                df,
                closed_candle,
            )

            candidates.append(
                result
            )

        except Exception:

            logger.exception(
                "%s | error analizando",
                pair,
            )

    return candidates


# ============================================================
# ELEGIR EL MEJOR MERCADO
# ============================================================

def select_best_market(
    results: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:

    valid_results = [
        result
        for result in results
        if result.get("valid")
        and result.get("signal")
        in ("call", "put")
        and int(
            result.get(
                "score",
                0,
            )
        ) >= MIN_MARKET_SCORE
    ]

    if not valid_results:
        return None

    valid_results.sort(
        key=lambda item: (
            int(item.get("score", 0)),
            int(
                item.get(
                    "continuity",
                    {},
                ).get(
                    "score",
                    0,
                )
            ),
            int(
                item.get(
                    "confirmation",
                    {},
                ).get(
                    "score",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    return valid_results[0]


# ============================================================
# LOG RANKING
# ============================================================

def log_market_ranking(
    results: List[Dict[str, Any]],
) -> None:

    ranking = sorted(
        results,
        key=lambda item: int(
            item.get(
                "score",
                0,
            )
        ),
        reverse=True,
    )

    for result in ranking[
        :TOP_MARKETS_TO_LOG
    ]:

        logger.info(
            "RANK | %s | score=%s | "
            "direction=%s | signal=%s | %s",
            result.get("pair"),
            result.get("score"),
            result.get("direction"),
            result.get("signal"),
            result.get("reason"),
        )


# ============================================================
# CREAR SEÑAL DEL MEJOR MERCADO
# ============================================================

def create_pending_entry(
    result: Dict[str, Any],
) -> None:

    global PENDING_ENTRY

    pair = result.get("pair")
    signal = result.get("signal")
    minute_ts = result.get(
        "minute_timestamp"
    )

    if not pair:
        return

    if signal not in (
        "call",
        "put",
    ):
        return

    if minute_ts is None:
        return

    minute_ts = int(
        minute_ts
    )

    next_ts = (
        minute_ts + TIMEFRAME
    )

    if (
        PENDING_ENTRY is not None
        and PENDING_ENTRY.get(
            "next_timestamp"
        ) == next_ts
    ):
        return

    direction = (
        "CALL 🟢"
        if signal == "call"
        else "PUT 🔴"
    )

    PENDING_ENTRY = {
        "pair": pair,
        "signal": signal,
        "score": result.get(
            "score",
            0,
        ),
        "minute_timestamp": minute_ts,
        "next_timestamp": next_ts,
        "minute_open": result.get(
            "minute_open"
        ),
        "minute_close": result.get(
            "minute_close"
        ),
        "reason": result.get(
            "reason",
            ""
        ),
        "attempts": 0,
        "last_attempt": 0.0,
        "last_rejection": None,
        "entry_notified": False,
        "created_at": time.time(),
    }

    structure = result.get(
        "structure",
        {}
    )

    continuity = result.get(
        "continuity",
        {}
    )

    confirmation = result.get(
        "confirmation",
        {}
    )

    telegram_send(
        "🏆 MEJOR MERCADO SELECCIONADO\n\n"
        f"Par: {pair}\n"
        f"Dirección: {direction}\n"
        f"Score: {result.get('score')}/100\n\n"
        "ESTRUCTURA\n"
        f"{structure.get('reason')}\n"
        f"Score: {structure.get('score')}\n\n"
        "CONTINUIDAD\n"
        f"{continuity.get('reason')}\n\n"
        "CONFIRMACIÓN\n"
        f"{confirmation.get('reason')}\n\n"
        "VELA N\n"
        f"Apertura: "
        f"{result.get('minute_open')}\n"
        f"Cierre: "
        f"{result.get('minute_close')}\n\n"
        "🎯 ENTRADA EXCLUSIVA EN N+1\n"
        f"Timestamp N+1: {next_ts}\n"
        "Ventana: segundos 00-05"
    )

    logger.info(
        "MEJOR MERCADO | %s | %s | "
        "score=%s | N=%s | N+1=%s",
        pair,
        signal.upper(),
        result.get("score"),
        minute_ts,
        next_ts,
    )


# ============================================================
# COMPRA
# ============================================================

def buy_binary(
    pair: str,
    signal: str,
) -> tuple[bool, Optional[Any], Any]:

    if IQ is None:
        return (
            False,
            None,
            "IQ=None",
        )

    try:

        response = IQ.buy(
            AMOUNT,
            pair,
            signal,
            EXPIRATION,
        )

        if isinstance(
            response,
            tuple,
        ):

            if len(response) >= 2:

                return (
                    bool(response[0]),
                    response[1],
                    response,
                )

            if len(response) == 1:

                return (
                    bool(response[0]),
                    None,
                    response,
                )

        if response is True:

            return (
                True,
                None,
                response,
            )

        return (
            False,
            None,
            response,
        )

    except Exception as exc:

        logger.exception(
            "%s | error IQ.buy",
            pair,
        )

        return (
            False,
            None,
            str(exc),
        )


# ============================================================
# EJECUTAR SEÑAL PENDIENTE
# ============================================================

def execute_pending_entry() -> bool:

    global PENDING_ENTRY
    global LAST_TRADE_CANDLE

    pending = PENDING_ENTRY

    if pending is None:
        return False

    server_ts = get_server_timestamp()

    if server_ts is None:
        return False

    n1_timestamp = int(
        pending["next_timestamp"]
    )

    if server_ts < n1_timestamp:
        return False

    if (
        LAST_TRADE_CANDLE
        == n1_timestamp
    ):

        PENDING_ENTRY = None

        return True

    elapsed = (
        server_ts - n1_timestamp
    )

    if elapsed > MAX_ENTRY_DELAY:

        logger.warning(
            "%s | ventana agotada | "
            "último rechazo=%s",
            pending["pair"],
            pending.get(
                "last_rejection"
            ),
        )

        telegram_send(
            "❌ ENTRADA CANCELADA\n\n"
            f"Par: {pending['pair']}\n"
            f"Dirección: "
            f"{pending['signal'].upper()}\n"
            "Motivo: ventana N+1 agotada\n\n"
            f"Última respuesta IQ:\n"
            f"{pending.get('last_rejection')}"
        )

        PENDING_ENTRY = None

        return False

    pair = pending["pair"]
    signal = pending["signal"]

    server_second = (
        server_ts % TIMEFRAME
    )

    if not pending.get(
        "entry_notified"
    ):

        pending[
            "entry_notified"
        ] = True

        telegram_send(
            "⚡ N+1 DETECTADA\n\n"
            f"Par: {pair}\n"
            f"Dirección: "
            f"{signal.upper()}\n"
            f"Score: "
            f"{pending['score']}/100\n\n"
            f"Segundo servidor: "
            f"{server_second:02d}\n\n"
            "🎯 Intentando entrada"
        )

    last_attempt = float(
        pending.get(
            "last_attempt",
            0.0,
        )
    )

    if (
        last_attempt > 0
        and time.time() - last_attempt
        < POLL_INTERVAL
    ):
        return False

    pending["last_attempt"] = (
        time.time()
    )

    pending["attempts"] = int(
        pending.get(
            "attempts",
            0,
        )
    ) + 1

    logger.info(
        "%s | IQ.buy #%s | %s | "
        "N+1=%s | segundo=%02d",
        pair,
        pending["attempts"],
        signal.upper(),
        n1_timestamp,
        server_second,
    )

    ok, order_id, raw_result = (
        buy_binary(
            pair,
            signal,
        )
    )

    if not ok:

        pending[
            "last_rejection"
        ] = raw_result

        logger.warning(
            "%s | IQ RECHAZÓ | "
            "intento=%s | respuesta=%s",
            pair,
            pending["attempts"],
            raw_result,
        )

        return False

    LAST_TRADE_CANDLE = (
        n1_timestamp
    )

    telegram_send(
        "✅ OPERACIÓN ABIERTA\n\n"
        f"🏆 Mejor mercado: {pair}\n"
        f"Dirección: "
        f"{signal.upper()}\n"
        f"Score: "
        f"{pending['score']}/100\n\n"
        "VELA N\n"
        f"Apertura: "
        f"{pending['minute_open']}\n"
        f"Cierre: "
        f"{pending['minute_close']}\n\n"
        "ENTRADA N+1\n"
        f"Timestamp: "
        f"{n1_timestamp}\n"
        f"Segundo: "
        f"{server_second:02d}\n\n"
        f"💵 Importe: ${AMOUNT}\n"
        f"⏱ Expiración: "
        f"{EXPIRATION} minuto\n"
        f"🆔 ID: {order_id}\n"
        f"🔁 Intentos: "
        f"{pending['attempts']}"
    )

    logger.info(
        "%s | OPERACIÓN ABIERTA | "
        "%s | score=%s | id=%s",
        pair,
        signal.upper(),
        pending["score"],
        order_id,
    )

    PENDING_ENTRY = None

    return True


# ============================================================
# PROCESAR EL CIERRE DE UN MINUTO
# ============================================================

def process_market_cycle() -> None:

    global LAST_PROCESSED_MINUTE

    server_ts = get_server_timestamp()

    if server_ts is None:
        return

    current_minute = (
        server_ts // TIMEFRAME
    ) * TIMEFRAME

    # --------------------------------------------------------
    # SIEMPRE INTENTAR LA ENTRADA PENDIENTE PRIMERO
    # --------------------------------------------------------

    execute_pending_entry()

    # --------------------------------------------------------
    # ANALIZAR EN VIVO TODOS LOS OTC
    # --------------------------------------------------------

    refresh_otc_assets()

    # Solo procesar el cierre una vez.
    closed_minute = (
        current_minute - TIMEFRAME
    )

    if (
        LAST_PROCESSED_MINUTE
        == closed_minute
    ):

        return

    results = analyze_all_markets(
        server_ts
    )

    if not results:
        return

    LAST_PROCESSED_MINUTE = (
        closed_minute
    )

    log_market_ranking(
        results
    )

    # --------------------------------------------------------
    # SI YA EXISTE UNA OPERACIÓN PENDIENTE,
    # NO CREAR OTRA
    # --------------------------------------------------------

    if PENDING_ENTRY is not None:

        logger.info(
            "Ya existe una entrada pendiente."
        )

        return

    best_market = select_best_market(
        results
    )

    if best_market is None:

        logger.info(
            "NO TRADE | ningún OTC alcanzó "
            "el score mínimo."
        )

        telegram_send(
            "🔎 ANÁLISIS MULTI-OTC\n\n"
            f"Mercados analizados: "
            f"{len(results)}\n\n"
            "🚫 SIN OPERACIÓN\n"
            "Ningún mercado alcanzó las "
            "condiciones mínimas."
        )

        return

    create_pending_entry(
        best_market
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    global BOT_RUNNING

    logger.info(
        "======================================"
    )

    logger.info(
        "BOT IQ OPTION MULTI-OTC"
    )

    logger.info(
        "ESTRATEGIA: CONTINUIDAD"
    )

    logger.info(
        "ANÁLISIS: TODOS LOS OTC DISPONIBLES"
    )

    logger.info(
        "SELECCIÓN: MEJOR MERCADO"
    )

    logger.info(
        "TIMEFRAME: 1 MINUTO"
    )

    logger.info(
        "AMOUNT: $%s",
        AMOUNT,
    )

    logger.info(
        "EXPIRATION: %s MINUTO",
        EXPIRATION,
    )

    logger.info(
        "MIN SCORE: %s",
        MIN_MARKET_SCORE,
    )

    logger.info(
        "ENTRADA N+1: 00-%ss",
        MAX_ENTRY_DELAY,
    )

    logger.info(
        "======================================"
    )

    required = {
        "IQ_EMAIL": IQ_EMAIL,
        "IQ_PASSWORD": IQ_PASSWORD,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": (
            TELEGRAM_CHAT_ID
        ),
    }

    missing = [
        key
        for key, value
        in required.items()
        if not value
    ]

    if missing:

        logger.error(
            "Faltan variables: %s",
            ", ".join(missing),
        )

        return

    try:

        connect_iq()

    except Exception as exc:

        logger.exception(
            "No se pudo conectar."
        )

        telegram_send(
            "❌ ERROR IQ OPTION\n\n"
            f"{exc}"
        )

        return

    telegram_thread = (
        threading.Thread(
            target=telegram_worker,
            daemon=True,
        )
    )

    telegram_thread.start()

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "MODO MULTI-OTC\n\n"
        "🔎 Descubre OTC disponibles\n"
        "📊 Analiza cada estructura\n"
        "👁 Monitorea velas M1 en vivo\n"
        "🏆 Compara los mercados\n"
        "🎯 Selecciona el mejor\n"
        "➡️ Opera a favor de la continuidad\n\n"
        "BLOQUEOS:\n"
        "🚫 Rango\n"
        "🚫 Agotamiento\n"
        "🚫 Rechazo fuerte\n"
        "🚫 Soporte/Resistencia\n"
        "🚫 Confirmación débil\n\n"
        f"💵 ${AMOUNT}\n"
        "⏱ 1 minuto\n"
        "⚡ Entrada N+1 segundos 00-05"
    )

    while True:

        try:

            if not BOT_RUNNING:

                time.sleep(0.20)

                continue

            if not ensure_connection():

                time.sleep(1)

                continue

            process_market_cycle()

            time.sleep(
                POLL_INTERVAL
            )

        except KeyboardInterrupt:

            BOT_RUNNING = False

            telegram_send(
                "🔴 BOT DETENIDO MANUALMENTE"
            )

            logger.info(
                "Bot detenido."
            )

            break

        except Exception:

            logger.exception(
                "Error principal"
            )

            time.sleep(0.5)


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":

    main()
