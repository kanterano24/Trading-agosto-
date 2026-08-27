from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option

from strategy import (
    analyze_live_candle,
    analyze_market,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# CONFIG IQ OPTION
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")


# ============================================================
# CONFIG TELEGRAM
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# MERCADO
# ============================================================

PAIRS = [
    "EURUSD-OTC",
    "EURJPY-OTC",
    "EURGBP-OTC",
    "GBPUSD-OTC",
    "USDCHF-OTC",
]

TIMEFRAME = 60

CANDLE_COUNT = 60

EXPIRATION = 4

# Frecuencia del análisis en vivo.
POLL_INTERVAL = 0.50


# ============================================================
# OPERATIVA
# ============================================================

MAX_OPEN_TRADES = 1

LAST_TRADED_CANDLE: Dict[str, int] = {}

RESULT_CHECK_DELAY = 5


# ============================================================
# CAPITAL
# ============================================================

BASE_AMOUNT = 100.0

CURRENT_AMOUNT = BASE_AMOUNT

MAX_AMOUNT = 200.0

WIN_STREAK = 0

LOSS_STREAK = 0

MODE = "compound"


# ============================================================
# ESTADO GLOBAL
# ============================================================

BOT_RUNNING = False

IQ: Optional[IQ_Option] = None

LAST_UPDATE_ID: Optional[int] = None

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}

ACTIVE_API_PAIR: Dict[str, str] = {}

STREAMS_STARTED = False


# ============================================================
# ESTADO DE VELAS EN VIVO
# ============================================================

LIVE_M1_STATE: Dict[str, Dict[str, Any]] = {}

LAST_CLOSED_M1: Dict[str, int] = {}

LAST_LIVE_LOG_SECOND: Dict[str, int] = {}


# ============================================================
# TELEGRAM ANTI-SPAM
# ============================================================

LAST_MSG: Dict[str, float] = {}

COOLDOWN = 10


def can_send(
    key: str,
) -> bool:

    now = time.time()

    if key in LAST_MSG:

        elapsed = (
            now
            - LAST_MSG[key]
        )

        if elapsed < COOLDOWN:
            return False

    LAST_MSG[key] = now

    return True


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(
    msg: str,
    key: str = "msg",
    force: bool = False,
) -> bool:

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return False

    if not force and not can_send(key):
        return False

    try:

        response = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/sendMessage"
            ),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
            },
            timeout=5,
        )

        return response.ok

    except Exception as e:

        logging.error(
            "Error Telegram: %s",
            e,
        )

        return False


# ============================================================
# CAPITAL
# ============================================================

def update_amount(
    profit: float,
) -> None:

    global CURRENT_AMOUNT
    global WIN_STREAK
    global LOSS_STREAK

    if profit > 0:

        WIN_STREAK += 1
        LOSS_STREAK = 0

        if MODE == "compound":

            multiplier = (
                1
                + WIN_STREAK * 0.25
            )

            CURRENT_AMOUNT = min(
                BASE_AMOUNT
                * multiplier,
                MAX_AMOUNT,
            )

        else:

            CURRENT_AMOUNT = BASE_AMOUNT

    elif profit < 0:

        LOSS_STREAK += 1
        WIN_STREAK = 0

        CURRENT_AMOUNT = BASE_AMOUNT

    else:

        CURRENT_AMOUNT = BASE_AMOUNT


# ============================================================
# TELEGRAM WORKER
# ============================================================

def telegram_worker() -> None:

    global LAST_UPDATE_ID
    global BOT_RUNNING

    while True:

        try:

            if not TELEGRAM_TOKEN:

                time.sleep(2)

                continue

            params = {}

            if LAST_UPDATE_ID is not None:

                params["offset"] = (
                    LAST_UPDATE_ID
                    + 1
                )

            response = requests.get(
                (
                    "https://api.telegram.org/"
                    f"bot{TELEGRAM_TOKEN}/getUpdates"
                ),
                params=params,
                timeout=10,
            )

            data = response.json()

            for update in data.get(
                "result",
                [],
            ):

                LAST_UPDATE_ID = (
                    update[
                        "update_id"
                    ]
                )

                message = (
                    update.get(
                        "message"
                    )
                    or {}
                )

                text = message.get(
                    "text",
                    "",
                )

                chat_id = str(
                    message.get(
                        "chat",
                        {},
                    ).get(
                        "id",
                        "",
                    )
                )

                if not isinstance(
                    text,
                    str,
                ):
                    continue

                if str(chat_id) != str(
                    TELEGRAM_CHAT_ID
                ):
                    continue

                command = (
                    text.lower()
                    .strip()
                )

                # ------------------------------------------------
                # START
                # ------------------------------------------------

                if command.startswith(
                    "/start"
                ):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        f"📊 Pares: "
                        f"{', '.join(PAIRS)}\n"
                        f"💵 Monto: "
                        f"${CURRENT_AMOUNT:.2f}\n"
                        f"⏱ Expiración: "
                        f"{EXPIRATION}m\n"
                        "📡 Análisis M1 en vivo",
                        "start",
                        force=True,
                    )

                # ------------------------------------------------
                # STOP
                # ------------------------------------------------

                elif command.startswith(
                    "/stop"
                ):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                        force=True,
                    )

                # ------------------------------------------------
                # STATUS
                # ------------------------------------------------

                elif command.startswith(
                    "/status"
                ):

                    telegram_send(
                        "🤖 ESTADO DEL BOT\n\n"
                        f"Estado: "
                        f"{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\n\n"
                        f"💵 Monto actual: "
                        f"${CURRENT_AMOUNT:.2f}\n"
                        f"📈 Wins seguidos: "
                        f"{WIN_STREAK}\n"
                        f"📉 Losses seguidos: "
                        f"{LOSS_STREAK}\n"
                        f"📂 Operaciones abiertas: "
                        f"{len(OPEN_TRADES)}\n"
                        f"⏱ Expiración: "
                        f"{EXPIRATION}m\n"
                        f"📡 Streams activos: "
                        f"{len(LIVE_M1_STATE)}",
                        "status",
                        force=True,
                    )

        except Exception as e:

            logging.error(
                "Error Telegram worker: %s",
                e,
            )

        time.sleep(1)


# ============================================================
# CONEXIÓN
# ============================================================

def connect() -> bool:

    global IQ

    if (
        not IQ_EMAIL
        or not IQ_PASSWORD
    ):

        logging.error(
            "Faltan IQ_EMAIL o IQ_PASSWORD"
        )

        return False

    try:

        logging.info(
            "Conectando a IQ Option..."
        )

        IQ = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        check, reason = IQ.connect()

        if not check:

            logging.error(
                "No se pudo conectar: %s",
                reason,
            )

            return False

        logging.info(
            "Conectado a IQ Option"
        )

        return True

    except Exception as e:

        logging.exception(
            "Error conexión IQ: %s",
            e,
        )

        return False


# ============================================================
# VERIFICAR CONEXIÓN
# ============================================================

def ensure_connection() -> bool:

    global IQ
    global STREAMS_STARTED

    try:

        if IQ is not None:

            if IQ.check_connect():

                return True

    except Exception:
        pass

    STREAMS_STARTED = False

    logging.warning(
        "Reconectando a IQ Option..."
    )

    return connect()


# ============================================================
# CANDIDATOS DE PAR
# ============================================================

def get_pair_candidates(
    logical_pair: str,
):

    candidates = []

    normalized = (
        logical_pair
        .upper()
        .strip()
    )

    if normalized.endswith(
        "-OTC"
    ):

        candidates.append(
            normalized
        )

        candidates.append(
            normalized[:-4]
        )

    else:

        candidates.append(
            f"{normalized}-OTC"
        )

        candidates.append(
            normalized
        )

    return candidates


# ============================================================
# OBTENER VELAS HISTÓRICAS
# ============================================================

def get_pair_candles(
    logical_pair: str,
) -> Tuple[
    Optional[str],
    Optional[pd.DataFrame],
]:

    if IQ is None:
        return None, None

    candidates = []

    if logical_pair in ACTIVE_API_PAIR:

        candidates.append(
            ACTIVE_API_PAIR[
                logical_pair
            ]
        )

    for pair in get_pair_candidates(
        logical_pair
    ):

        if pair not in candidates:

            candidates.append(pair)

    for api_pair in candidates:

        try:

            candles = IQ.get_candles(
                api_pair,
                TIMEFRAME,
                CANDLE_COUNT,
                time.time(),
            )

            if not candles:
                continue

            df = pd.DataFrame(
                candles
            )

            if df.empty:
                continue

            required = {
                "open",
                "close",
                "high",
                "low",
            }

            if not required.issubset(
                df.columns
            ):
                continue

            ACTIVE_API_PAIR[
                logical_pair
            ] = api_pair

            return (
                api_pair,
                df,
            )

        except Exception as e:

            logging.debug(
                "No disponible %s: %s",
                api_pair,
                e,
            )

    return None, None


# ============================================================
# OBTENER SOLO CERRADAS
# ============================================================

def get_closed_candles(
    df: pd.DataFrame,
) -> pd.DataFrame:

    if (
        df is None
        or df.empty
    ):

        return pd.DataFrame()

    data = df.copy()

    if "from" in data.columns:

        now = time.time()

        timestamp = pd.to_numeric(
            data["from"],
            errors="coerce",
        )

        closed = data[
            (
                timestamp
                + TIMEFRAME
            )
            <= now
        ]

        if not closed.empty:

            data = closed

    data = data.dropna(
        subset=[
            "open",
            "close",
            "high",
            "low",
        ]
    )

    data = data.reset_index(
        drop=True
    )

    return data


# ============================================================
# CONVERTIR STREAM A VELA
# ============================================================

def normalize_stream_candle(
    raw: Any,
) -> Optional[Dict[str, Any]]:

    if not isinstance(
        raw,
        dict,
    ):
        return None

    def number(
        *keys,
    ):

        for key in keys:

            value = raw.get(
                key
            )

            if value is None:
                continue

            try:

                return float(value)

            except Exception:
                continue

        return None

    open_price = number(
        "open",
    )

    close = number(
        "close",
    )

    high = number(
        "high",
        "max",
    )

    low = number(
        "low",
        "min",
    )

    if (
        open_price is None
        or close is None
        or high is None
        or low is None
    ):

        return None

    result = {
        "open": open_price,
        "close": close,
        "high": high,
        "low": low,
    }

    for key in (
        "from",
        "to",
        "at",
        "id",
    ):

        if key in raw:

            result[key] = raw[key]

    return result


# ============================================================
# INICIAR STREAMS
# ============================================================

def start_streams() -> bool:

    global STREAMS_STARTED

    if IQ is None:
        return False

    success = 0

    for logical_pair in PAIRS:

        api_pair = ACTIVE_API_PAIR.get(
            logical_pair
        )

        if not api_pair:

            api_pair, _ = (
                get_pair_candles(
                    logical_pair
                )
            )

        if not api_pair:
            continue

        try:

            IQ.start_candles_stream(
                api_pair,
                TIMEFRAME,
                CANDLE_COUNT,
            )

            success += 1

            logging.info(
                "Stream M1 iniciado: %s",
                api_pair,
            )

        except Exception as e:

            logging.error(
                "Error iniciando stream %s: %s",
                api_pair,
                e,
            )

    STREAMS_STARTED = (
        success > 0
    )

    return STREAMS_STARTED


# ============================================================
# OBTENER VELA M1 EN VIVO
# ============================================================

def get_live_candle(
    logical_pair: str,
) -> Tuple[
    Optional[str],
    Optional[Dict[str, Any]],
]:

    if IQ is None:
        return None, None

    api_pair = ACTIVE_API_PAIR.get(
        logical_pair
    )

    if not api_pair:

        api_pair, _ = (
            get_pair_candles(
                logical_pair
            )
        )

    if not api_pair:
        return None, None

    try:

        candles = (
            IQ.get_realtime_candles(
                api_pair,
                TIMEFRAME,
            )
        )

        if not candles:
            return api_pair, None

        # Las versiones habituales
        # devuelven un diccionario:
        #
        # timestamp -> candle
        #
        # Seleccionamos la vela más reciente.

        if isinstance(
            candles,
            dict,
        ):

            latest_key = max(
                candles.keys(),
                key=lambda x: float(x),
            )

            raw = candles[
                latest_key
            ]

        else:

            return api_pair, None

        normalized = (
            normalize_stream_candle(
                raw
            )
        )

        return (
            api_pair,
            normalized,
        )

    except Exception as e:

        logging.debug(
            "Error stream %s: %s",
            api_pair,
            e,
        )

        return api_pair, None


# ============================================================
# TIMESTAMP DE VELA
# ============================================================

def candle_timestamp(
    candle: Dict[str, Any],
) -> int:

    for key in (
        "from",
        "at",
    ):

        value = candle.get(
            key
        )

        if value is not None:

            try:

                return int(
                    float(value)
                )

            except Exception:
                pass

    return int(
        time.time()
        // TIMEFRAME
    ) * TIMEFRAME


# ============================================================
# HISTORIAL PARA ANÁLISIS
# ============================================================

def get_history_for_analysis(
    logical_pair: str,
) -> Optional[pd.DataFrame]:

    api_pair, df = get_pair_candles(
        logical_pair
    )

    if (
        api_pair is None
        or df is None
        or df.empty
    ):
        return None

    closed = get_closed_candles(
        df
    )

    if len(closed) < 22:
        return None

    return closed.tail(
        CANDLE_COUNT
    ).reset_index(
        drop=True
    )


# ============================================================
# ANALIZAR M1 EN VIVO
# ============================================================

def analyze_live_pair(
    logical_pair: str,
) -> Optional[Dict[str, Any]]:

    api_pair, live_candle = (
        get_live_candle(
            logical_pair
        )
    )

    if (
        api_pair is None
        or live_candle is None
    ):
        return None

    history = (
        get_history_for_analysis(
            logical_pair
        )
    )

    if history is None:
        return None

    ts = candle_timestamp(
        live_candle
    )

    elapsed = (
        time.time()
        - ts
    )

    elapsed = max(
        0.0,
        min(
            elapsed,
            60.0,
        ),
    )

    analysis = analyze_live_candle(
        candle_1m=live_candle,
        previous_m1=history,
        pair=logical_pair,
        elapsed_seconds=elapsed,
    )

    analysis[
        "logical_pair"
    ] = logical_pair

    analysis[
        "api_pair"
    ] = api_pair

    analysis[
        "candle_timestamp"
    ] = ts

    return analysis


# ============================================================
# ANALIZAR TODOS LOS PARES EN VIVO
# ============================================================

def analyze_all_pairs_live() -> Dict[
    str,
    Dict[str, Any],
]:

    results = {}

    for logical_pair in PAIRS:

        try:

            analysis = (
                analyze_live_pair(
                    logical_pair
                )
            )

            if analysis is not None:

                results[
                    logical_pair
                ] = analysis

                LIVE_M1_STATE[
                    logical_pair
                ] = analysis

        except Exception as e:

            logging.debug(
                "Error análisis vivo %s: %s",
                logical_pair,
                e,
            )

    return results


# ============================================================
# REGISTRAR CAMBIO DE VELA
# ============================================================

def detect_new_minute(
    analysis: Dict[str, Any],
) -> bool:

    logical_pair = analysis[
        "logical_pair"
    ]

    ts = int(
        analysis[
            "candle_timestamp"
        ]
    )

    previous_ts = (
        LAST_CLOSED_M1.get(
            logical_pair
        )
    )

    if previous_ts is None:

        LAST_CLOSED_M1[
            logical_pair
        ] = ts

        return False

    if ts > previous_ts:

        LAST_CLOSED_M1[
            logical_pair
        ] = ts

        return True

    return False


# ============================================================
# OBTENER LA VELA QUE ACABA DE CERRAR
# ============================================================

def get_closed_candle_after_boundary(
    logical_pair: str,
    closed_timestamp: int,
) -> Optional[
    Tuple[
        str,
        pd.Series,
        pd.DataFrame,
    ]
]:

    api_pair, df = get_pair_candles(
        logical_pair
    )

    if (
        api_pair is None
        or df is None
        or df.empty
    ):
        return None

    closed = get_closed_candles(
        df
    )

    if closed.empty:
        return None

    selected = None

    if "from" in closed.columns:

        timestamps = pd.to_numeric(
            closed["from"],
            errors="coerce",
        )

        matches = closed[
            timestamps
            == float(
                closed_timestamp
            )
        ]

        if not matches.empty:

            selected = matches.iloc[
                -1
            ]

    if selected is None:

        selected = closed.iloc[
            -1
        ]

    return (
        api_pair,
        selected,
        closed,
    )


# ============================================================
# ANALIZAR VELA CERRADA
# ============================================================

def analyze_closed_pair(
    logical_pair: str,
    closed_timestamp: int,
) -> Optional[
    Dict[str, Any]
]:

    data = (
        get_closed_candle_after_boundary(
            logical_pair,
            closed_timestamp,
        )
    )

    if data is None:
        return None

    api_pair, candle, history = data

    if len(history) < 22:
        return None

    try:

        result = analyze_market(
            candle_1m=candle,
            previous_m1=history,
            pair=logical_pair,
        )

    except Exception as e:

        logging.error(
            "Error estrategia cerrada %s: %s",
            logical_pair,
            e,
        )

        return None

    result[
        "logical_pair"
    ] = logical_pair

    result[
        "api_pair"
    ] = api_pair

    result[
        "candle_timestamp"
    ] = closed_timestamp

    return result


# ============================================================
# BUSCAR MEJOR OPERACIÓN DESPUÉS DEL CIERRE
# ============================================================

def find_best_trade(
    closed_timestamp: int,
) -> Optional[
    Dict[str, Any]
]:

    candidates = []

    for logical_pair in PAIRS:

        result = (
            analyze_closed_pair(
                logical_pair,
                closed_timestamp,
            )
        )

        if not result:
            continue

        if not result.get(
            "valid"
        ):
            continue

        signal = result.get(
            "signal"
        )

        if signal not in (
            "call",
            "put",
        ):
            continue

        score = int(
            result.get(
                "score",
                0,
            )
        )

        candidates.append(
            result
        )

        logging.info(
            "%s | %s | score=%s | pattern=%s",
            logical_pair,
            signal,
            score,
            result.get(
                "pattern",
                "UNKNOWN",
            ),
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: int(
            x.get(
                "score",
                0,
            )
        ),
        reverse=True,
    )

    return candidates[0]


# ============================================================
# EJECUTAR
# ============================================================

def execute_trade(
    trade: Dict[str, Any],
) -> bool:

    if IQ is None:
        return False

    if (
        len(OPEN_TRADES)
        >= MAX_OPEN_TRADES
    ):

        logging.info(
            "Máximo de operaciones abiertas"
        )

        return False

    logical_pair = trade[
        "logical_pair"
    ]

    api_pair = trade[
        "api_pair"
    ]

    signal = trade[
        "signal"
    ]

    score = trade.get(
        "score",
        0,
    )

    pattern = trade.get(
        "pattern",
        "UNKNOWN",
    )

    candle_timestamp = trade[
        "candle_timestamp"
    ]

    if LAST_TRADED_CANDLE.get(
        logical_pair
    ) == candle_timestamp:

        logging.info(
            "La vela ya fue operada: %s",
            logical_pair,
        )

        return False

    amount = min(
        float(CURRENT_AMOUNT),
        float(MAX_AMOUNT),
    )

    try:

        # ----------------------------------------------------
        # CONFIRMACIÓN
        # ----------------------------------------------------

        telegram_send(
            "✅ CONFIRMACIÓN COMPLETA\n\n"
            f"📊 Par: {api_pair}\n"
            f"🎯 Dirección: "
            f"{signal.upper()}\n"
            f"🧠 Patrón: {pattern}\n"
            f"⭐ Score: {score}/100\n"
            "🔎 Condiciones confirmadas\n"
            "🚀 Preparando ejecución...",
            (
                f"confirmation_"
                f"{logical_pair}_"
                f"{candle_timestamp}"
            ),
            force=True,
        )

        logging.info(
            "COMPRA | %s | %s | score=%s",
            api_pair,
            signal,
            score,
        )

        ok, order_id = IQ.buy(
            amount,
            api_pair,
            signal,
            EXPIRATION,
        )

        if not ok:

            logging.warning(
                "Compra rechazada: %s",
                api_pair,
            )

            return False

        LAST_TRADED_CANDLE[
            logical_pair
        ] = candle_timestamp

        expiry_timestamp = (
            int(time.time())
            + (
                EXPIRATION
                * 60
            )
        )

        OPEN_TRADES[
            order_id
        ] = {
            "pair": logical_pair,
            "api_pair": api_pair,
            "signal": signal,
            "pattern": pattern,
            "score": score,
            "amount": amount,
            "expiry": expiry_timestamp,
            "created": int(
                time.time()
            ),
        }

        # ----------------------------------------------------
        # EJECUCIÓN
        # ----------------------------------------------------

        telegram_send(
            "🚀 OPERACIÓN EJECUTADA\n\n"
            f"📊 Par: {api_pair}\n"
            f"🎯 Señal: "
            f"{signal.upper()}\n"
            f"🧠 Patrón: {pattern}\n"
            f"⭐ Score: {score}/100\n"
            f"💵 Monto: "
            f"${amount:.2f}\n"
            f"⏱ Expiración: "
            f"{EXPIRATION}m",
            f"trade_{order_id}",
            force=True,
        )

        return True

    except Exception as e:

        logging.exception(
            "Error ejecutando operación: %s",
            e,
        )

        return False


# ============================================================
# RESULTADOS
# ============================================================

def check_results() -> None:

    if IQ is None:
        return

    now = int(
        time.time()
    )

    for order_id, trade in list(
        OPEN_TRADES.items()
    ):

        if now < (
            trade["expiry"]
            + RESULT_CHECK_DELAY
        ):
            continue

        try:

            result = IQ.check_win_v4(
                order_id
            )

            if result is None:
                continue

            profit = float(
                result
            )

            update_amount(
                profit
            )

            if profit > 0:
                outcome = "WIN 🟢"

            elif profit < 0:
                outcome = "LOSS 🔴"

            else:
                outcome = "DRAW 🟡"

            telegram_send(
                "📊 RESULTADO\n\n"
                f"📈 {trade['api_pair']}\n"
                f"🎯 {trade['signal'].upper()}\n"
                f"🧠 {trade['pattern']}\n"
                f"🏁 {outcome}\n"
                f"💰 Resultado: "
                f"${profit:.2f}\n\n"
                f"📈 WIN STREAK: "
                f"{WIN_STREAK}\n"
                f"📉 LOSS STREAK: "
                f"{LOSS_STREAK}\n"
                f"💵 Próximo monto: "
                f"${CURRENT_AMOUNT:.2f}",
                f"result_{order_id}",
                force=True,
            )

            logging.info(
                "Resultado %s: %.2f",
                order_id,
                profit,
            )

            del OPEN_TRADES[
                order_id
            ]

        except Exception as e:

            logging.error(
                "Error resultado %s: %s",
                order_id,
                e,
            )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    global BOT_RUNNING
    global STREAMS_STARTED

    if not connect():

        logging.error(
            "No fue posible iniciar"
        )

        return

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    if (
        TELEGRAM_TOKEN
        and TELEGRAM_CHAT_ID
    ):

        threading.Thread(
            target=telegram_worker,
            daemon=True,
        ).start()

    logging.info(
        "Bot iniciado"
    )

    # --------------------------------------------------------
    # ESPERAR ACTIVACIÓN
    # --------------------------------------------------------

    while True:

        try:

            if not ensure_connection():

                time.sleep(3)

                continue

            check_results()

            if not BOT_RUNNING:

                time.sleep(1)

                continue

            # ------------------------------------------------
            # INICIAR STREAMS
            # ------------------------------------------------

            if not STREAMS_STARTED:

                start_streams()

                time.sleep(2)

                continue

            # ------------------------------------------------
            # ANALIZAR TODAS LAS M1 EN VIVO
            # ------------------------------------------------

            live_results = (
                analyze_all_pairs_live()
            )

            # ------------------------------------------------
            # DETECTAR CAMBIO DE VELA
            # ------------------------------------------------

            new_candles = []

            for logical_pair, analysis in (
                live_results.items()
            ):

                if detect_new_minute(
                    analysis
                ):

                    new_candles.append(
                        (
                            logical_pair,
                            int(
                                analysis[
                                    "candle_timestamp"
                                ]
                            ),
                        )
                    )

                # ------------------------------------------------
                # LOG DE ANÁLISIS EN VIVO
                #
                # Solo mostramos cada 10 segundos
                # para no llenar los logs.
                # NO TELEGRAM.
                # ------------------------------------------------

                elapsed = float(
                    analysis.get(
                        "elapsed_seconds",
                        0,
                    )
                )

                second_bucket = int(
                    elapsed
                    // 10
                )

                previous_bucket = (
                    LAST_LIVE_LOG_SECOND.get(
                        logical_pair
                    )
                )

                if (
                    previous_bucket
                    != second_bucket
                ):

                    LAST_LIVE_LOG_SECOND[
                        logical_pair
                    ] = second_bucket

                    logging.info(
                        (
                            "LIVE | %s | "
                            "%.0fs | "
                            "O=%.5f "
                            "H=%.5f "
                            "L=%.5f "
                            "C=%.5f | "
                            "DIR=%s | "
                            "CALL=%s | "
                            "PUT=%s"
                        ),
                        logical_pair,
                        elapsed,
                        analysis.get(
                            "open",
                            0,
                        ),
                        analysis.get(
                            "high",
                            0,
                        ),
                        analysis.get(
                            "low",
                            0,
                        ),
                        analysis.get(
                            "close",
                            0,
                        ),
                        analysis.get(
                            "direction",
                            "NEUTRAL",
                        ),
                        analysis.get(
                            "bullish_score",
                            0,
                        ),
                        analysis.get(
                            "bearish_score",
                            0,
                        ),
                    )

            # ------------------------------------------------
            # CUANDO CAMBIA LA M1:
            #
            # La vela anterior ya terminó.
            #
            # AHORA se analiza la vela cerrada.
            # ------------------------------------------------

            for (
                logical_pair,
                closed_timestamp,
            ) in new_candles:

                logging.info(
                    (
                        "CIERRE M1 | %s | "
                        "timestamp=%s | "
                        "calculando confirmación..."
                    ),
                    logical_pair,
                    closed_timestamp
                    - TIMEFRAME,
                )

            # ------------------------------------------------
            # BUSCAR MEJOR OPORTUNIDAD
            #
            # Una sola vez por vela global.
            # ------------------------------------------------

            if new_candles:

                closed_timestamp = (
                    new_candles[0][1]
                    - TIMEFRAME
                )

                # Esperamos brevemente para que
                # la API publique definitivamente
                # la vela cerrada.
                time.sleep(
                    0.25
                )

                best_trade = (
                    find_best_trade(
                        closed_timestamp
                    )
                )

                if best_trade:

                    logging.info(
                        (
                            "MEJOR OPORTUNIDAD | "
                            "%s | %s | "
                            "score=%s | "
                            "pattern=%s"
                        ),
                        best_trade[
                            "api_pair"
                        ],
                        best_trade[
                            "signal"
                        ],
                        best_trade[
                            "score"
                        ],
                        best_trade.get(
                            "pattern"
                        ),
                    )

                    # ------------------------------------------------
                    # EJECUCIÓN
                    #
                    # Estamos al inicio de N+1.
                    # ------------------------------------------------

                    execute_trade(
                        best_trade
                    )

                else:

                    logging.info(
                        "No hay oportunidad válida "
                        "en el cierre de la M1."
                    )

            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            check_results()

            time.sleep(
                POLL_INTERVAL
            )

        except KeyboardInterrupt:

            logging.info(
                "Bot detenido manualmente"
            )

            BOT_RUNNING = False

            break

        except Exception as e:

            logging.exception(
                "Error en loop principal: %s",
                e,
            )

            # Si el stream falla, se vuelve
            # a inicializar.
            STREAMS_STARTED = False

            time.sleep(2)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
