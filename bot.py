from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, List

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_market


# ============================================================
# CONFIGURACIÓN
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# TIMEFRAME / EXPIRACIÓN
# ============================================================

TIMEFRAME = 60

# Expiración real de la operación
EXPIRATION = 1


# ============================================================
# 6 PARES REALES FIJOS
#
# IMPORTANTE:
# - NO OTC
# - NO -OP
# - NO se obtienen automáticamente
# - NO se usa get_all_open_time()
# ============================================================

REAL_PAIRS = [
    "EURUSD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "GBPUSD",
    "NZDUSD",
]


# ============================================================
# 💰 GESTIÓN DE MONTO
# ============================================================

BASE_AMOUNT = 556

CURRENT_AMOUNT = BASE_AMOUNT

MAX_AMOUNT = 5000

WIN_STREAK = 0
LOSS_STREAK = 0

MODE = "compound"


# ============================================================
# ESTADO DEL BOT
# ============================================================

BOT_RUNNING = False

IQ: Optional[IQ_Option] = None

LAST_UPDATE_ID = None

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}


# ============================================================
# ANTI-SPAM TELEGRAM
# ============================================================

LAST_MSG = {}

COOLDOWN = 10


def can_send(key: str) -> bool:

    now = time.time()

    if key in LAST_MSG:

        if now - LAST_MSG[key] < COOLDOWN:
            return False

    LAST_MSG[key] = now

    return True


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(
    msg: str,
    key: str = "msg",
):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not can_send(key):
        return False

    try:

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
            },
            timeout=5,
        )

        return True

    except Exception as e:

        logging.error(
            "Telegram error: %s",
            e,
        )

        return False


# ============================================================
# 💰 ACTUALIZAR MONTO
# ============================================================

def update_amount(
    profit: float,
):

    global CURRENT_AMOUNT
    global WIN_STREAK
    global LOSS_STREAK

    if profit > 0:

        WIN_STREAK += 1
        LOSS_STREAK = 0

        CURRENT_AMOUNT = min(
            BASE_AMOUNT * (
                1 + WIN_STREAK * 0.5
            ),
            MAX_AMOUNT,
        )

    elif profit < 0:

        LOSS_STREAK += 1
        WIN_STREAK = 0

        CURRENT_AMOUNT = BASE_AMOUNT

    else:

        CURRENT_AMOUNT = BASE_AMOUNT


# ============================================================
# TELEGRAM WORKER
# ============================================================

def telegram_worker():

    global LAST_UPDATE_ID
    global BOT_RUNNING

    while True:

        try:

            if not TELEGRAM_TOKEN:

                time.sleep(5)
                continue

            params = {}

            if LAST_UPDATE_ID is not None:

                params["offset"] = (
                    LAST_UPDATE_ID + 1
                )

            response = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params,
                timeout=5,
            )

            data = response.json()

            for update in data.get(
                "result",
                [],
            ):

                LAST_UPDATE_ID = update[
                    "update_id"
                ]

                message = update.get(
                    "message",
                    {},
                )

                text = message.get(
                    "text",
                    "",
                )

                if not isinstance(
                    text,
                    str,
                ):
                    continue

                text = text.lower().strip()

                chat_id = str(
                    message.get(
                        "chat",
                        {},
                    ).get(
                        "id",
                    )
                )

                if chat_id != str(
                    TELEGRAM_CHAT_ID
                ):
                    continue


                # =================================================
                # START
                # =================================================

                if text.startswith(
                    "/start"
                ):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "📊 Mercado REAL\n"
                        "🚫 Sin OTC\n"
                        "🚫 Sin -OP\n\n"
                        "🔎 Analizando 6 pares fijos:\n"
                        "• EURUSD\n"
                        "• EURGBP\n"
                        "• EURJPY\n"
                        "• GBPJPY\n"
                        "• GBPUSD\n"
                        "• NZDUSD\n\n"
                        "⏱ Expiración: 1 minuto.",
                        "start",
                    )


                # =================================================
                # STOP
                # =================================================

                elif text.startswith(
                    "/stop"
                ):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                    )


                # =================================================
                # STATUS
                # =================================================

                elif text.startswith(
                    "/status"
                ):

                    pairs = "\n".join(
                        f"• {pair}"
                        for pair in REAL_PAIRS
                    )

                    telegram_send(
                        "🤖 ESTADO DEL BOT\n\n"
                        f"Estado: "
                        f"{'ACTIVO 🟢' if BOT_RUNNING else 'DETENIDO 🔴'}\n\n"
                        f"💰 Monto: ${CURRENT_AMOUNT}\n"
                        f"⏱ Expiración: {EXPIRATION} minuto\n\n"
                        "📊 PARES REALES FIJOS:\n"
                        f"{pairs}",
                        "status",
                    )

        except Exception as e:

            logging.error(
                "Telegram worker error: %s",
                e,
            )

        time.sleep(1)


# ============================================================
# CONEXIÓN IQ OPTION
# ============================================================

def connect():

    global IQ

    if not IQ_EMAIL or not IQ_PASSWORD:

        raise RuntimeError(
            "Faltan IQ_EMAIL o IQ_PASSWORD."
        )

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    IQ.connect()

    time.sleep(2)

    try:

        connected = IQ.check_connect()

    except Exception:

        connected = False

    if not connected:

        raise RuntimeError(
            "No se pudo conectar a IQ Option."
        )

    telegram_send(
        "🟢 CONECTADO A IQ OPTION\n\n"
        "📊 Mercado REAL\n"
        "🚫 Sin OTC\n"
        "🚫 Sin -OP\n\n"
        "6 pares fijos cargados.",
        "connect",
    )


# ============================================================
# VALIDAR NOMBRE DEL PAR
# ============================================================
#
# Esta función evita que accidentalmente entre:
#
# EURUSD-OTC
# EURUSD-OP
# EURUSD-OTC-OP
#
# El bot solamente acepta los nombres de REAL_PAIRS.
# ============================================================

def is_valid_real_pair(
    pair: str,
) -> bool:

    if not isinstance(
        pair,
        str,
    ):
        return False

    pair = pair.strip().upper()

    if pair not in REAL_PAIRS:
        return False

    if "-OTC" in pair:
        return False

    if "-OP" in pair:
        return False

    return True


# ============================================================
# OBTENER VELAS
# ============================================================

def get_market_data(
    pair: str,
):

    global IQ

    if IQ is None:
        return None


    # --------------------------------------------------------
    # PROTECCIÓN ABSOLUTA CONTRA OTC / -OP
    # --------------------------------------------------------

    if not is_valid_real_pair(pair):

        logging.error(
            "PAR RECHAZADO: %s",
            pair,
        )

        return None


    try:

        # IMPORTANTE:
        # Se envía exactamente:
        #
        # EURUSD
        # GBPUSD
        # etc.
        #
        # Nunca EURUSD-OP
        # Nunca EURUSD-OTC

        candles = IQ.get_candles(
            pair,
            TIMEFRAME,
            60,
            time.time(),
        )

        if not candles:

            logging.warning(
                "Sin velas para %s",
                pair,
            )

            return None

        df = pd.DataFrame(
            candles
        )

        if df.empty:
            return None

        required = {
            "open",
            "close",
            "high",
            "low",
        }

        if not required.issubset(
            df.columns
        ):

            logging.error(
                "Faltan columnas en %s",
                pair,
            )

            return None


        # ----------------------------------------------------
        # LIMPIAR DATOS
        # ----------------------------------------------------

        for column in required:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        df.dropna(
            subset=list(required),
            inplace=True,
        )

        df.reset_index(
            drop=True,
            inplace=True,
        )

        if len(df) < 8:

            logging.warning(
                "Pocas velas para %s: %s",
                pair,
                len(df),
            )

            return None

        return df

    except Exception as e:

        logging.error(
            "Error obteniendo velas %s: %s",
            pair,
            e,
        )

        return None


# ============================================================
# ANALIZAR PAR
# ============================================================

def analyze_pair(
    pair: str,
):

    # --------------------------------------------------------
    # Nunca permitir nombres incorrectos
    # --------------------------------------------------------

    if not is_valid_real_pair(
        pair
    ):

        return None

    df = get_market_data(
        pair
    )

    if df is None:
        return None

    if len(df) < 8:
        return None

    try:

        result = analyze_market(
            df.iloc[-1],
            previous_m1=df,
            pair=pair,
        )

        return result

    except Exception as e:

        logging.error(
            "Error strategy %s: %s",
            pair,
            e,
        )

        return None


# ============================================================
# EJECUTAR OPERACIÓN
# ============================================================

def execute_trade(
    pair: str,
    signal: str,
    score: int = 0,
):

    global IQ
    global OPEN_TRADES

    if IQ is None:
        return False


    # --------------------------------------------------------
    # VALIDAR PAR
    # --------------------------------------------------------

    if not is_valid_real_pair(
        pair
    ):

        logging.error(
            "OPERACIÓN RECHAZADA - PAR NO REAL: %s",
            pair,
        )

        telegram_send(
            f"⚠️ OPERACIÓN RECHAZADA\n\n"
            f"Par inválido: {pair}\n"
            f"Solo se permiten pares reales.",
            f"invalid_{pair}",
        )

        return False


    # --------------------------------------------------------
    # VALIDAR SEÑAL
    # --------------------------------------------------------

    if signal not in (
        "call",
        "put",
    ):

        return False


    # --------------------------------------------------------
    # EVITAR OPERAR DOS VECES
    # --------------------------------------------------------

    if pair_has_open_trade(
        pair
    ):

        return False


    try:

        amount = CURRENT_AMOUNT

        ok, order_id = IQ.buy(
            amount,
            pair,
            signal,
            EXPIRATION,
        )

        if not ok:

            telegram_send(
                "⚠️ NO SE PUDO EJECUTAR\n\n"
                f"📊 Par: {pair}\n"
                f"📈 Señal: {signal.upper()}\n"
                f"🎯 Score: {score}\n"
                f"💵 Monto: ${amount}\n"
                f"⏱ Expiración: {EXPIRATION} min",
                f"buyfail_{pair}",
            )

            return False


        # ----------------------------------------------------
        # GUARDAR OPERACIÓN
        # ----------------------------------------------------

        OPEN_TRADES[
            order_id
        ] = {

            "pair": pair,

            "signal": signal,

            "amount": amount,

            "score": score,

            "opened": int(
                time.time()
            ),

            "expiry": int(
                time.time()
                + EXPIRATION * 60
            ),
        }


        telegram_send(
            "🚀 TRADE EJECUTADO\n\n"
            f"📊 Par: {pair}\n"
            f"📈 Señal: {signal.upper()}\n"
            f"🎯 Score: {score}\n"
            f"💵 Monto: ${amount}\n"
            f"⏱ Expiración: {EXPIRATION} minuto",
            f"trade_{order_id}",
        )

        return True

    except Exception as e:

        logging.error(
            "Error ejecutando %s: %s",
            pair,
            e,
        )

        return False


# ============================================================
# RESULTADOS
# ============================================================

def check_results():

    global OPEN_TRADES

    if IQ is None:
        return

    now = int(
        time.time()
    )

    for order_id, trade in list(
        OPEN_TRADES.items()
    ):

        if now < trade["expiry"]:
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

                outcome = "EMPATE ⚪"


            telegram_send(
                "📊 RESULTADO\n\n"
                f"📊 {trade['pair']}\n"
                f"📈 {trade['signal'].upper()}\n"
                f"{outcome}\n"
                f"💰 Resultado: {profit}\n\n"
                f"📈 WIN: {WIN_STREAK}\n"
                f"📉 LOSS: {LOSS_STREAK}\n"
                f"💵 Próximo monto: ${CURRENT_AMOUNT}",
                f"result_{order_id}",
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
# EVITAR OPERAR VARIAS VECES EL MISMO PAR
# ============================================================

def pair_has_open_trade(
    pair: str,
) -> bool:

    for trade in OPEN_TRADES.values():

        if trade.get(
            "pair"
        ) == pair:

            return True

    return False


# ============================================================
# MOSTRAR LOS 6 PARES
# ============================================================

def send_active_pairs():

    telegram_send(
        "🔎 PARES EN ANÁLISIS\n\n"
        "📊 MERCADO REAL\n"
        "🚫 SIN OTC\n"
        "🚫 SIN -OP\n\n"
        + "\n".join(
            f"• {pair}"
            for pair in REAL_PAIRS
        )
        + "\n\n"
        f"⏱ Expiración: {EXPIRATION} minuto",
        "active_pairs",
    )


# ============================================================
# ANALIZAR LOS 6 PARES
# ============================================================

def analyze_all_pairs():

    results = []

    for pair in REAL_PAIRS:

        # ----------------------------------------------------
        # Protección adicional
        # ----------------------------------------------------

        if not is_valid_real_pair(
            pair
        ):
            continue

        result = analyze_pair(
            pair
        )

        if result:

            results.append(
                result
            )

    return results


# ============================================================
# MAIN
# ============================================================

def main():

    global BOT_RUNNING

    logging.basicConfig(
        level=logging.ERROR,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )


    # ========================================================
    # CONECTAR
    # ========================================================

    connect()


    # ========================================================
    # TELEGRAM
    # ========================================================

    threading.Thread(
        target=telegram_worker,
        daemon=True,
    ).start()


    telegram_send(
        "🤖 BOT LISTO\n\n"
        "📊 Mercado REAL\n"
        "🚫 Sin OTC\n"
        "🚫 Sin -OP\n\n"
        "🔎 6 pares fijos cargados:\n"
        "• EURUSD\n"
        "• EURGBP\n"
        "• EURJPY\n"
        "• GBPJPY\n"
        "• GBPUSD\n"
        "• NZDUSD\n\n"
        "⏱ Expiración: 1 minuto\n\n"
        "Usa /START para comenzar.",
        "ready",
    )


    # ========================================================
    # BUCLE PRINCIPAL
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # BOT DETENIDO
            # ------------------------------------------------

            if not BOT_RUNNING:

                time.sleep(1)

                continue


            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            check_results()


            # ------------------------------------------------
            # ANALIZAR LOS 6 PARES FIJOS
            #
            # NO HAY:
            #
            # get_all_open_time()
            # refresh_active_pairs()
            # get_open_pairs()
            #
            # ------------------------------------------------

            for pair in REAL_PAIRS:

                if not BOT_RUNNING:
                    break


                # --------------------------------------------
                # Protección contra nombres incorrectos
                # --------------------------------------------

                if not is_valid_real_pair(
                    pair
                ):

                    continue


                # --------------------------------------------
                # No operar si ya existe una operación
                # --------------------------------------------

                if pair_has_open_trade(
                    pair
                ):

                    continue


                # --------------------------------------------
                # ANALIZAR
                # --------------------------------------------

                result = analyze_pair(
                    pair
                )


                if not result:

                    time.sleep(0.3)
                    continue


                score = int(
                    result.get(
                        "score",
                        0,
                    )
                )

                signal = result.get(
                    "signal"
                )


                # --------------------------------------------
                # LOG INTERNO
                # --------------------------------------------

                logging.info(
                    "%s | direction=%s | score=%s | signal=%s",
                    pair,
                    result.get(
                        "direction"
                    ),
                    score,
                    signal,
                )


                # --------------------------------------------
                # OPERAR SOLO SI STRATEGY AUTORIZA
                # --------------------------------------------

                if result.get(
                    "valid"
                ):

                    if signal in (
                        "call",
                        "put",
                    ):

                        execute_trade(
                            pair,
                            signal,
                            score,
                        )


                # --------------------------------------------
                # PEQUEÑA PAUSA
                # --------------------------------------------

                time.sleep(
                    0.5
                )


            # ------------------------------------------------
            # SIGUIENTE CICLO
            # ------------------------------------------------

            time.sleep(1)


        except Exception as e:

            logging.error(
                "MAIN ERROR: %s",
                e,
            )

            time.sleep(2)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
