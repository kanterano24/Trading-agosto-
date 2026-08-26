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

# EXPIRACIÓN REAL DE LA OPERACIÓN
EXPIRATION = 1


# ============================================================
# PARES CANDIDATOS
# ============================================================

PAIR_CANDIDATES = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDCHF-OTC",
    "EURGBP-OTC",
    "EURJPY-OTC",
    "GBPJPY-OTC",
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "USDCAD-OTC",
    "NZDUSD-OTC",
    "EURCAD-OTC",
    "GBPCAD-OTC",
    "AUDJPY-OTC",
    "CADJPY-OTC",
    "CHFJPY-OTC",
]


# ============================================================
# CANTIDAD DE PARES QUE QUEREMOS MANTENER ACTIVOS
# ============================================================

MAX_ACTIVE_PAIRS = 5


# ============================================================
# 💰 GESTIÓN DE MONTO
# ============================================================

BASE_AMOUNT = 556

CURRENT_AMOUNT = BASE_AMOUNT

# IMPORTANTE:
# Debe ser mayor que BASE_AMOUNT si quieres que el monto
# pueda crecer después de ganancias.
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

ACTIVE_PAIRS: List[str] = []

LAST_PAIR_REFRESH = 0

PAIR_REFRESH_INTERVAL = 10


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

def telegram_send(msg: str, key: str = "msg"):

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

    except Exception:
        return False


# ============================================================
# 💰 ACTUALIZAR MONTO
# ============================================================

def update_amount(profit: float):

    global CURRENT_AMOUNT
    global WIN_STREAK
    global LOSS_STREAK

    if profit > 0:

        WIN_STREAK += 1
        LOSS_STREAK = 0

        CURRENT_AMOUNT = min(
            BASE_AMOUNT * (1 + WIN_STREAK * 0.5),
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
                params["offset"] = LAST_UPDATE_ID + 1

            res = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params,
                timeout=5,
            ).json()

            for u in res.get("result", []):

                LAST_UPDATE_ID = u["update_id"]

                msg = u.get("message", {})

                text = msg.get("text", "")

                if not isinstance(text, str):
                    continue

                text = text.lower().strip()

                chat_id = str(
                    msg.get("chat", {}).get("id")
                )

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue


                # ------------------------------------------------
                # START
                # ------------------------------------------------

                if text.startswith("/start"):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "Buscando pares OTC abiertos "
                        "con expiración de 1 minuto.",
                        "start",
                    )


                # ------------------------------------------------
                # STOP
                # ------------------------------------------------

                elif text.startswith("/stop"):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                    )


                # ------------------------------------------------
                # STATUS
                # ------------------------------------------------

                elif text.startswith("/status"):

                    pairs = ", ".join(ACTIVE_PAIRS)

                    if not pairs:
                        pairs = "Ninguno"

                    telegram_send(
                        f"🤖 ESTADO DEL BOT\n\n"
                        f"Estado: "
                        f"{'ACTIVO 🟢' if BOT_RUNNING else 'DETENIDO 🔴'}\n\n"
                        f"💰 Monto: ${CURRENT_AMOUNT}\n"
                        f"⏱ Expiración: {EXPIRATION} minuto\n\n"
                        f"📊 Pares activos:\n"
                        f"{pairs}",
                        "status",
                    )

        except Exception:
            pass

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

        check = IQ.check_connect()

    except Exception:

        check = False

    if not check:

        raise RuntimeError(
            "No se pudo conectar a IQ Option."
        )

    telegram_send(
        "🟢 CONECTADO A IQ OPTION",
        "connect",
    )


# ============================================================
# OBTENER ESTADO DE PARES
# ============================================================

def get_open_pairs() -> List[str]:

    global IQ

    if IQ is None:
        return []

    try:

        open_time = IQ.get_all_open_time()

        if not open_time:
            return []

        binary = open_time.get("binary", {})

        digital = open_time.get("digital", {})

        result = []

        for pair in PAIR_CANDIDATES:

            is_open = False


            # ------------------------------------------------
            # BINARY
            # ------------------------------------------------

            if pair in binary:

                data = binary[pair]

                if isinstance(data, dict):

                    is_open = bool(
                        data.get("open", False)
                    )


            # ------------------------------------------------
            # DIGITAL
            # ------------------------------------------------
            # No usamos DIGITAL para comprar porque este bot
            # utiliza IQ.buy(), es decir, binary options.
            # Esta sección solamente evita considerar digital
            # como binary disponible.
            # ------------------------------------------------

            if is_open:

                result.append(pair)

        return result

    except Exception as e:

        logging.error(
            "Error obteniendo pares abiertos: %s",
            e,
        )

        return []


# ============================================================
# ACTUALIZAR LOS 5 PARES
# ============================================================

def refresh_active_pairs(force: bool = False) -> List[str]:

    global ACTIVE_PAIRS
    global LAST_PAIR_REFRESH

    now = time.time()

    if (
        not force
        and now - LAST_PAIR_REFRESH < PAIR_REFRESH_INTERVAL
    ):

        return ACTIVE_PAIRS

    LAST_PAIR_REFRESH = now

    open_pairs = get_open_pairs()

    old_pairs = set(ACTIVE_PAIRS)

    new_pairs = []

    for pair in open_pairs:

        if pair not in new_pairs:

            new_pairs.append(pair)

        if len(new_pairs) >= MAX_ACTIVE_PAIRS:
            break


    # --------------------------------------------------------
    # Detectar cambios
    # --------------------------------------------------------

    new_set = set(new_pairs)

    removed = old_pairs - new_set

    added = new_set - old_pairs


    if removed:

        for pair in removed:

            telegram_send(
                f"🔴 PAR DESCARTADO\n\n"
                f"{pair}\n"
                f"Motivo: mercado cerrado/no disponible.",
                f"closed_{pair}",
            )


    if added:

        for pair in added:

            telegram_send(
                f"🟢 PAR DISPONIBLE\n\n"
                f"{pair}\n"
                f"⏱ Expiración configurada: 1 minuto.",
                f"open_{pair}",
            )


    ACTIVE_PAIRS = new_pairs

    return ACTIVE_PAIRS


# ============================================================
# COMPROBAR SI UN PAR SIGUE ABIERTO
# ============================================================

def pair_is_open(pair: str) -> bool:

    open_pairs = get_open_pairs()

    return pair in open_pairs


# ============================================================
# OBTENER VELAS
# ============================================================

def get_market_data(pair: str):

    global IQ

    if IQ is None:
        return None

    try:

        candles = IQ.get_candles(
            pair,
            TIMEFRAME,
            60,
            time.time(),
        )

        if not candles:
            return None

        df = pd.DataFrame(candles)

        if df.empty:
            return None

        required = {
            "open",
            "close",
            "high",
            "low",
        }

        if not required.issubset(df.columns):

            logging.error(
                "Faltan columnas en %s",
                pair,
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

def analyze_pair(pair: str):

    df = get_market_data(pair)

    if df is None:
        return None

    if len(df) < 6:
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

    if signal not in ("call", "put"):
        return False


    # --------------------------------------------------------
    # ÚLTIMA COMPROBACIÓN ANTES DE OPERAR
    # --------------------------------------------------------

    if not pair_is_open(pair):

        telegram_send(
            f"⚠️ OPERACIÓN DESCARTADA\n\n"
            f"{pair}\n"
            f"Motivo: el par se cerró antes de entrar.",
            f"notopen_{pair}",
        )

        return False


    try:

        amount = CURRENT_AMOUNT

        ok, oid = IQ.buy(
            amount,
            pair,
            signal,
            EXPIRATION,
        )

        if not ok:

            telegram_send(
                f"⚠️ NO SE PUDO EJECUTAR\n\n"
                f"{pair}\n"
                f"Señal: {signal.upper()}\n"
                f"Score: {score}\n"
                f"Expiración: {EXPIRATION} min",
                f"buyfail_{pair}",
            )

            return False


        # ----------------------------------------------------
        # GUARDAR OPERACIÓN
        # ----------------------------------------------------

        OPEN_TRADES[oid] = {

            "pair": pair,

            "signal": signal,

            "amount": amount,

            "score": score,

            "opened": int(time.time()),

            "expiry": int(
                time.time()
                + EXPIRATION * 60
            ),
        }


        telegram_send(
            f"🚀 TRADE EJECUTADO\n\n"
            f"📊 Par: {pair}\n"
            f"📈 Señal: {signal.upper()}\n"
            f"🎯 Score: {score}\n"
            f"💵 Monto: ${amount}\n"
            f"⏱ Expiración: {EXPIRATION} minuto",
            f"trade_{oid}",
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

    now = int(time.time())

    for oid, trade in list(
        OPEN_TRADES.items()
    ):

        if now < trade["expiry"]:
            continue

        try:

            result = IQ.check_win_v4(oid)

            if result is None:
                continue

            profit = float(result)

            update_amount(profit)

            if profit > 0:

                outcome = "WIN 🟢"

            elif profit < 0:

                outcome = "LOSS 🔴"

            else:

                outcome = "EMPATE ⚪"


            telegram_send(
                f"📊 RESULTADO\n\n"
                f"📊 {trade['pair']}\n"
                f"📈 {trade['signal'].upper()}\n"
                f"{outcome}\n"
                f"💰 Resultado: {profit}\n\n"
                f"📈 WIN: {WIN_STREAK}\n"
                f"📉 LOSS: {LOSS_STREAK}\n"
                f"💵 Próximo monto: ${CURRENT_AMOUNT}",
                f"result_{oid}",
            )

            del OPEN_TRADES[oid]

        except Exception as e:

            logging.error(
                "Error resultado %s: %s",
                oid,
                e,
            )


# ============================================================
# EVITAR OPERAR VARIAS VECES EL MISMO PAR
# ============================================================

def pair_has_open_trade(pair: str) -> bool:

    for trade in OPEN_TRADES.values():

        if trade.get("pair") == pair:
            return True

    return False


# ============================================================
# MOSTRAR PARES ACTIVOS
# ============================================================

def send_active_pairs():

    pairs = ACTIVE_PAIRS

    if not pairs:
        return

    telegram_send(
        "🔎 PARES EN ANÁLISIS\n\n"
        + "\n".join(
            f"• {p}"
            for p in pairs
        )
        + "\n\n"
        f"⏱ Expiración: {EXPIRATION} minuto",
        "active_pairs",
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global BOT_RUNNING

    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    connect()


    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    threading.Thread(
        target=telegram_worker,
        daemon=True,
    ).start()


    telegram_send(
        "🤖 BOT LISTO\n\n"
        "⏱ Expiración: 1 minuto\n"
        "🔎 Buscando hasta 5 pares OTC abiertos.",
        "ready",
    )


    # --------------------------------------------------------
    # BUCLE PRINCIPAL
    # --------------------------------------------------------

    while True:

        try:

            # ------------------------------------------------
            # Si está detenido
            # ------------------------------------------------

            if not BOT_RUNNING:

                time.sleep(1)

                continue


            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            check_results()


            # ------------------------------------------------
            # ACTUALIZAR LOS 5 PARES
            # ------------------------------------------------

            pairs = refresh_active_pairs()


            # ------------------------------------------------
            # SI NO HAY PARES
            # ------------------------------------------------

            if not pairs:

                telegram_send(
                    "⚠️ SIN PARES DISPONIBLES\n\n"
                    "Buscando nuevamente...",
                    "no_pairs",
                )

                time.sleep(3)

                continue


            # ------------------------------------------------
            # ANALIZAR CADA PAR
            # ------------------------------------------------

            for pair in list(pairs):

                if not BOT_RUNNING:
                    break


                # --------------------------------------------
                # No analizar un par que ya tiene operación
                # --------------------------------------------

                if pair_has_open_trade(pair):

                    continue


                # --------------------------------------------
                # Comprobar nuevamente que esté abierto
                # --------------------------------------------

                if not pair_is_open(pair):

                    refresh_active_pairs(
                        force=True
                    )

                    continue


                # --------------------------------------------
                # ANALIZAR MERCADO
                # --------------------------------------------

                result = analyze_pair(pair)


                if not result:

                    continue


                score = int(
                    result.get("score", 0)
                )

                signal = result.get(
                    "signal"
                )


                # --------------------------------------------
                # SOLO OPERAR SI LA ESTRATEGIA
                # LO AUTORIZA
                # --------------------------------------------

                if result.get("valid"):

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
                # Pequeña pausa para no saturar API
                # --------------------------------------------

                time.sleep(0.3)


            # ------------------------------------------------
            # LOOP
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
