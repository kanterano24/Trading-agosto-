from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_market


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
# CONFIG MERCADO
# ============================================================

PAIRS = [
    "EURUSD",
    "EURJPY",
    "EURGBP",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
]

TIMEFRAME = 60

# Cantidad de velas para análisis
CANDLE_COUNT = 60

# Expiración en minutos según IQ Option API
EXPIRATION = 4

# Analizar cerca del cierre / inicio de nueva vela
POLL_INTERVAL = 0.50


# ============================================================
# CONFIG OPERATIVA
# ============================================================

# Una sola operación abierta simultáneamente.
MAX_OPEN_TRADES = 1

# Evita volver a operar la misma vela.
LAST_TRADED_CANDLE: Dict[str, int] = {}

# Tiempo máximo para esperar resultados después del vencimiento.
RESULT_CHECK_DELAY = 5


# ============================================================
# GESTIÓN DE CAPITAL
# ============================================================

BASE_AMOUNT = 100.0
CURRENT_AMOUNT = BASE_AMOUNT

# Máximo absoluto permitido.
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


# ============================================================
# ANTI-SPAM TELEGRAM
# ============================================================

LAST_MSG: Dict[str, float] = {}

COOLDOWN = 10


def can_send(key: str) -> bool:

    now = time.time()

    if key in LAST_MSG:

        elapsed = now - LAST_MSG[key]

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

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not force and not can_send(key):
        return False

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
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
# GESTIÓN DE MONTO
# ============================================================

def update_amount(profit: float) -> None:

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
                BASE_AMOUNT * multiplier,
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
# TELEGRAM COMMANDS
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
                    LAST_UPDATE_ID + 1
                )

            response = requests.get(
                f"https://api.telegram.org/bot"
                f"{TELEGRAM_TOKEN}/getUpdates",
                params=params,
                timeout=10,
            )

            data = response.json()

            for update in data.get("result", []):

                LAST_UPDATE_ID = (
                    update["update_id"]
                )

                message = (
                    update.get("message")
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

                if command.startswith("/start"):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        f"📊 Pares: {', '.join(PAIRS)}\n"
                        f"💵 Monto: ${CURRENT_AMOUNT:.2f}\n"
                        f"⏱ Expiración: {EXPIRATION}m",
                        "start",
                        force=True,
                    )

                # ------------------------------------------------
                # STOP
                # ------------------------------------------------

                elif command.startswith("/stop"):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                        force=True,
                    )

                # ------------------------------------------------
                # STATUS
                # ------------------------------------------------

                elif command.startswith("/status"):

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
                        f"{EXPIRATION}m",
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
# CONEXIÓN IQ OPTION
# ============================================================

def connect() -> bool:

    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:

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

            telegram_send(
                "🔴 Error conectando a IQ Option",
                "connect_error",
                force=True,
            )

            return False

        logging.info(
            "Conectado a IQ Option"
        )

        telegram_send(
            "🟢 Conectado a IQ Option",
            "connect",
            force=True,
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

    try:

        if IQ is not None:

            if IQ.check_connect():

                return True

    except Exception:
        pass

    logging.warning(
        "Reconectando a IQ Option..."
    )

    return connect()


# ============================================================
# RESOLVER PAR
# ============================================================

def get_pair_candidates(
    logical_pair: str,
):

    candidates = []

    # Primero OTC.
    candidates.append(
        f"{logical_pair}-OTC"
    )

    # Después mercado normal.
    candidates.append(
        logical_pair
    )

    return candidates


# ============================================================
# OBTENER VELAS
# ============================================================

def get_pair_candles(
    logical_pair: str,
) -> Tuple[Optional[str], Optional[pd.DataFrame]]:

    if IQ is None:
        return None, None

    candidates = []

    # Si ya conocemos el activo que funciona,
    # lo intentamos primero.
    if logical_pair in ACTIVE_API_PAIR:

        candidates.append(
            ACTIVE_API_PAIR[logical_pair]
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

            return api_pair, df

        except Exception as e:

            logging.debug(
                "No disponible %s: %s",
                api_pair,
                e,
            )

    return None, None


# ============================================================
# OBTENER SOLO VELAS CERRADAS
# ============================================================

def get_closed_candles(
    df: pd.DataFrame,
) -> pd.DataFrame:

    if df is None or df.empty:

        return pd.DataFrame()

    data = df.copy()

    # Algunas versiones usan "from"
    # como timestamp de apertura.
    if "from" in data.columns:

        now = time.time()

        closed = data[
            (
                pd.to_numeric(
                    data["from"],
                    errors="coerce",
                )
                + TIMEFRAME
            )
            <= now
        ]

        if not closed.empty:

            data = closed

    # Eliminamos velas inválidas.
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
# ANALIZAR UN PAR
# ============================================================

def analyze_pair(
    logical_pair: str,
) -> Optional[Dict[str, Any]]:

    api_pair, df = get_pair_candles(
        logical_pair
    )

    if api_pair is None:

        return None

    if df is None or df.empty:

        return None

    hist = get_closed_candles(
        df
    )

    if len(hist) < 10:

        return None

    last_candle = hist.iloc[-1]

    candle_timestamp = 0

    if "from" in hist.columns:

        try:

            candle_timestamp = int(
                float(
                    last_candle["from"]
                )
            )

        except Exception:
            candle_timestamp = int(
                time.time() // 60
            )

    else:

        candle_timestamp = int(
            time.time() // 60
        )

    # Evitar operar dos veces la misma vela.
    if LAST_TRADED_CANDLE.get(
        logical_pair
    ) == candle_timestamp:

        return None

    try:

        result = analyze_market(
            candle_1m=last_candle,
            previous_m1=hist,
            pair=logical_pair,
        )

    except Exception as e:

        logging.error(
            "Error estrategia %s: %s",
            logical_pair,
            e,
        )

        return None

    result["logical_pair"] = logical_pair
    result["api_pair"] = api_pair
    result["candle_timestamp"] = (
        candle_timestamp
    )

    return result


# ============================================================
# BUSCAR MEJOR OPORTUNIDAD
# ============================================================

def find_best_trade() -> Optional[Dict[str, Any]]:

    candidates = []

    for logical_pair in PAIRS:

        result = analyze_pair(
            logical_pair
        )

        if not result:

            continue

        if not result.get("valid"):

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

    # Mejor score primero.
    candidates.sort(
        key=lambda x: (
            int(x.get("score", 0)),
        ),
        reverse=True,
    )

    return candidates[0]


# ============================================================
# COMPRAR
# ============================================================

def execute_trade(
    trade: Dict[str, Any],
) -> bool:

    if IQ is None:

        return False

    if len(OPEN_TRADES) >= MAX_OPEN_TRADES:

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

    amount = min(
        float(CURRENT_AMOUNT),
        float(MAX_AMOUNT),
    )

    try:

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

        # Marcamos la vela como operada.
        LAST_TRADED_CANDLE[
            logical_pair
        ] = candle_timestamp

        # Expiración aproximada.
        expiry_timestamp = (
            int(time.time())
            + (EXPIRATION * 60)
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
            "created": int(time.time()),
        }

        telegram_send(
            "🚀 NUEVA OPERACIÓN\n\n"
            f"📊 Par: {api_pair}\n"
            f"🎯 Señal: {signal.upper()}\n"
            f"🧠 Patrón: {pattern}\n"
            f"⭐ Score: {score}/100\n"
            f"💵 Monto: ${amount:.2f}\n"
            f"⏱ Expiración: {EXPIRATION}m",
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
# REVISAR RESULTADOS
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

        # Esperar hasta la expiración.
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
                f"💰 Resultado: ${profit:.2f}\n\n"
                f"📈 WIN STREAK: {WIN_STREAK}\n"
                f"📉 LOSS STREAK: {LOSS_STREAK}\n"
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
# ESPERAR NUEVA VELA
# ============================================================

def current_minute_timestamp() -> int:

    return int(
        time.time() // TIMEFRAME
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    global BOT_RUNNING

    if not connect():

        logging.error(
            "No fue posible iniciar"
        )

        return

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:

        threading.Thread(
            target=telegram_worker,
            daemon=True,
        ).start()

    # --------------------------------------------------------
    # MENSAJE INICIAL
    # --------------------------------------------------------

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "📊 Analizando pares:\n"
        f"{', '.join(PAIRS)}\n\n"
        "🧠 Estrategias:\n"
        "• Reversión\n"
        "• Continuidad\n"
        "• Rechazo\n"
        "• Pullback\n"
        "• Consolidación\n\n"
        f"💵 Base: ${BASE_AMOUNT:.2f}\n"
        f"🛑 Máximo: ${MAX_AMOUNT:.2f}\n"
        f"⏱ Expiración: {EXPIRATION}m",
        "ready",
        force=True,
    )

    last_processed_minute = None

    logging.info(
        "Bot iniciado"
    )

    # --------------------------------------------------------
    # LOOP PRINCIPAL
    # --------------------------------------------------------

    while True:

        try:

            if not ensure_connection():

                time.sleep(3)

                continue

            # Revisar resultados incluso
            # cuando el bot está detenido.
            check_results()

            if not BOT_RUNNING:

                time.sleep(1)

                continue

            # Solo buscamos una oportunidad
            # una vez por nueva vela.
            minute_id = (
                current_minute_timestamp()
            )

            if minute_id == (
                last_processed_minute
            ):

                time.sleep(
                    POLL_INTERVAL
                )

                continue

            # Esperamos unos segundos para
            # asegurarnos de que IQ haya
            # cerrado y publicado la vela.
            time.sleep(2)

            last_processed_minute = (
                minute_id
            )

            logging.info(
                "Nueva vela detectada. "
                "Analizando %s pares...",
                len(PAIRS),
            )

            # ------------------------------------------------
            # BUSCAR MEJOR TRADE
            # ------------------------------------------------

            best_trade = (
                find_best_trade()
            )

            if not best_trade:

                logging.info(
                    "No hay oportunidad válida"
                )

                continue

            # ------------------------------------------------
            # EJECUTAR SOLO EL MEJOR
            # ------------------------------------------------

            logging.info(
                "MEJOR OPORTUNIDAD | %s | %s | %s | score=%s",
                best_trade[
                    "api_pair"
                ],
                best_trade[
                    "signal"
                ],
                best_trade.get(
                    "pattern"
                ),
                best_trade[
                    "score"
                ],
            )

            execute_trade(
                best_trade
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

            time.sleep(2)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
