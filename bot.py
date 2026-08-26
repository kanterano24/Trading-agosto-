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
# CONFIGURACIÓN DE MERCADO
# ============================================================

# SOLO MERCADO REAL
# NO SE UTILIZA NINGÚN PAR OTC
# NO SE UTILIZA NINGÚN PAR -OP

MAX_ACTIVE_PAIRS = 6


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

ACTIVE_PAIRS: List[str] = []

LAST_PAIR_REFRESH = 0

PAIR_REFRESH_INTERVAL = 15


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
                        "id"
                    )
                )

                if chat_id != str(
                    TELEGRAM_CHAT_ID
                ):
                    continue


                # =================================================
                # START
                # =================================================

                if text.startswith("/start"):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "🔎 Buscando automáticamente "
                        "6 pares REALES.\n\n"
                        "🚫 Sin OTC\n"
                        "🚫 Sin -OP\n"
                        "⏱ Expiración: 1 minuto.",
                        "start",
                    )


                # =================================================
                # STOP
                # =================================================

                elif text.startswith("/stop"):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                    )


                # =================================================
                # STATUS
                # =================================================

                elif text.startswith("/status"):

                    pairs = ", ".join(
                        ACTIVE_PAIRS
                    )

                    if not pairs:
                        pairs = "Ninguno"

                    telegram_send(
                        f"🤖 ESTADO DEL BOT\n\n"
                        f"Estado: "
                        f"{'ACTIVO 🟢' if BOT_RUNNING else 'DETENIDO 🔴'}\n\n"
                        f"💰 Monto: ${CURRENT_AMOUNT}\n"
                        f"⏱ Expiración: {EXPIRATION} minuto\n\n"
                        f"📊 Pares reales activos:\n"
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
# VALIDAR PAR REAL
# ============================================================

def is_real_pair(
    pair: str,
) -> bool:

    if not isinstance(
        pair,
        str,
    ):
        return False

    pair = pair.strip().upper()

    if not pair:

        return False

    # --------------------------------------------------------
    # PROHIBIDO OTC
    # --------------------------------------------------------

    if "OTC" in pair:

        return False

    # --------------------------------------------------------
    # PROHIBIDO -OP
    # --------------------------------------------------------

    if "-OP" in pair:

        return False

    # --------------------------------------------------------
    # OTROS SUFIJOS QUE NO QUEREMOS
    # --------------------------------------------------------

    if pair.endswith(
        "-L"
    ):
        return False

    if pair.endswith(
        "-C"
    ):
        return False

    # --------------------------------------------------------
    # Solo pares de divisas de 6 caracteres
    # Ejemplo:
    #
    # EURUSD
    # GBPUSD
    # USDJPY
    # EURJPY
    # --------------------------------------------------------

    if len(pair) != 6:

        return False

    if not pair.isalpha():

        return False

    return True


# ============================================================
# OBTENER PARES REALES
# ============================================================

def get_real_pairs() -> List[str]:

    global IQ

    if IQ is None:
        return []

    try:

        # =====================================================
        # IMPORTANTE
        #
        # NO usamos:
        #
        # IQ.get_all_open_time()
        #
        # porque esa función intenta consultar DIGITAL
        # y en tu instalación está provocando:
        #
        # TypeError: 'NoneType' object is not subscriptable
        #
        # =====================================================

        data = IQ.get_all_init_v2()

        if not data:

            logging.error(
                "get_all_init_v2() devolvió vacío."
            )

            return []

        candidates = []

        # =====================================================
        # TURBO
        #
        # Aquí están los activos utilizables para
        # expiraciones cortas como 1 minuto.
        # =====================================================

        turbo = data.get(
            "turbo",
            {},
        )

        if isinstance(
            turbo,
            dict,
        ):

            actives = turbo.get(
                "actives",
                {},
            )

            if isinstance(
                actives,
                dict,
            ):

                for active_id, active in actives.items():

                    if not isinstance(
                        active,
                        dict,
                    ):
                        continue

                    name = active.get(
                        "name"
                    )

                    if not name:
                        continue

                    # -----------------------------------------
                    # La API puede entregar algo como:
                    #
                    # "turbo.EURUSD"
                    #
                    # Nos quedamos solamente con EURUSD.
                    # -----------------------------------------

                    if "." in str(name):

                        name = str(
                            name
                        ).split(
                            "."
                        )[-1]

                    name = str(
                        name
                    ).upper().strip()

                    # -----------------------------------------
                    # SOLO REAL
                    # -----------------------------------------

                    if not is_real_pair(
                        name
                    ):
                        continue

                    # -----------------------------------------
                    # ACTIVO HABILITADO
                    # -----------------------------------------

                    enabled = active.get(
                        "enabled",
                        True,
                    )

                    suspended = active.get(
                        "is_suspended",
                        False,
                    )

                    if enabled is False:
                        continue

                    if suspended is True:
                        continue

                    if name not in candidates:

                        candidates.append(
                            name
                        )


        # =====================================================
        # BINARY
        #
        # Se utiliza como respaldo.
        # =====================================================

        binary = data.get(
            "binary",
            {},
        )

        if isinstance(
            binary,
            dict,
        ):

            actives = binary.get(
                "actives",
                {},
            )

            if isinstance(
                actives,
                dict,
            ):

                for active_id, active in actives.items():

                    if not isinstance(
                        active,
                        dict,
                    ):
                        continue

                    name = active.get(
                        "name"
                    )

                    if not name:
                        continue

                    if "." in str(name):

                        name = str(
                            name
                        ).split(
                            "."
                        )[-1]

                    name = str(
                        name
                    ).upper().strip()

                    if not is_real_pair(
                        name
                    ):
                        continue

                    enabled = active.get(
                        "enabled",
                        True,
                    )

                    suspended = active.get(
                        "is_suspended",
                        False,
                    )

                    if enabled is False:
                        continue

                    if suspended is True:
                        continue

                    if name not in candidates:

                        candidates.append(
                            name
                        )


        # =====================================================
        # ORDENAR
        # =====================================================

        candidates = sorted(
            set(candidates)
        )

        # =====================================================
        # SOLO 6
        # =====================================================

        result = candidates[
            :MAX_ACTIVE_PAIRS
        ]

        logging.info(
            "PARES REALES ENCONTRADOS: %s",
            result,
        )

        return result

    except Exception as e:

        logging.error(
            "Error obteniendo pares reales: %s",
            e,
        )

        return []


# ============================================================
# ACTUALIZAR LOS 6 PARES
# ============================================================

def refresh_active_pairs(
    force: bool = False,
) -> List[str]:

    global ACTIVE_PAIRS
    global LAST_PAIR_REFRESH

    now = time.time()

    if (
        not force
        and now - LAST_PAIR_REFRESH
        < PAIR_REFRESH_INTERVAL
    ):

        return ACTIVE_PAIRS

    LAST_PAIR_REFRESH = now

    new_pairs = get_real_pairs()

    # --------------------------------------------------------
    # SEGURIDAD EXTRA
    # Nunca permitir OTC ni -OP
    # --------------------------------------------------------

    new_pairs = [
        p
        for p in new_pairs
        if is_real_pair(p)
    ]

    new_pairs = new_pairs[
        :MAX_ACTIVE_PAIRS
    ]

    old_set = set(
        ACTIVE_PAIRS
    )

    new_set = set(
        new_pairs
    )

    removed = old_set - new_set

    added = new_set - old_set


    # ========================================================
    # PARES DESCARTADOS
    # ========================================================

    for pair in removed:

        telegram_send(
            f"🔴 PAR DESCARTADO\n\n"
            f"{pair}\n"
            f"Motivo: dejó de estar disponible.",
            f"closed_{pair}",
        )


    # ========================================================
    # PARES NUEVOS
    # ========================================================

    for pair in added:

        telegram_send(
            f"🟢 PAR REAL DETECTADO\n\n"
            f"📊 {pair}\n"
            f"🚫 Sin OTC\n"
            f"🚫 Sin -OP\n"
            f"⏱ Expiración: 1 minuto.",
            f"open_{pair}",
        )


    ACTIVE_PAIRS = new_pairs

    return ACTIVE_PAIRS


# ============================================================
# COMPROBAR PAR DISPONIBLE
# ============================================================

def pair_is_open(
    pair: str,
) -> bool:

    if not is_real_pair(
        pair
    ):
        return False

    try:

        data = IQ.get_all_init_v2()

        if not data:
            return False

        # ----------------------------------------------------
        # Primero turbo
        # ----------------------------------------------------

        turbo = data.get(
            "turbo",
            {},
        )

        if isinstance(
            turbo,
            dict,
        ):

            actives = turbo.get(
                "actives",
                {},
            )

            if isinstance(
                actives,
                dict,
            ):

                for active in actives.values():

                    if not isinstance(
                        active,
                        dict,
                    ):
                        continue

                    name = active.get(
                        "name",
                        "",
                    )

                    if "." in str(name):

                        name = str(
                            name
                        ).split(
                            "."
                        )[-1]

                    name = str(
                        name
                    ).upper().strip()

                    if name != pair:
                        continue

                    if active.get(
                        "enabled",
                        True,
                    ) is False:
                        return False

                    if active.get(
                        "is_suspended",
                        False,
                    ):
                        return False

                    return True


        # ----------------------------------------------------
        # Después binary
        # ----------------------------------------------------

        binary = data.get(
            "binary",
            {},
        )

        if isinstance(
            binary,
            dict,
        ):

            actives = binary.get(
                "actives",
                {},
            )

            if isinstance(
                actives,
                dict,
            ):

                for active in actives.values():

                    if not isinstance(
                        active,
                        dict,
                    ):
                        continue

                    name = active.get(
                        "name",
                        "",
                    )

                    if "." in str(name):

                        name = str(
                            name
                        ).split(
                            "."
                        )[-1]

                    name = str(
                        name
                    ).upper().strip()

                    if name != pair:
                        continue

                    if active.get(
                        "enabled",
                        True,
                    ) is False:
                        return False

                    if active.get(
                        "is_suspended",
                        False,
                    ):
                        return False

                    return True

        return False

    except Exception as e:

        logging.error(
            "Error comprobando %s: %s",
            pair,
            e,
        )

        return False


# ============================================================
# OBTENER VELAS
# ============================================================

def get_market_data(
    pair: str,
):

    global IQ

    if IQ is None:
        return None

    # Seguridad:
    # nunca pedir velas de OTC o -OP

    if not is_real_pair(
        pair
    ):
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

    if not is_real_pair(
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

    if not is_real_pair(
        pair
    ):
        return False

    if signal not in (
        "call",
        "put",
    ):
        return False


    # --------------------------------------------------------
    # ÚLTIMA COMPROBACIÓN
    # --------------------------------------------------------

    if not pair_is_open(
        pair
    ):

        telegram_send(
            f"⚠️ OPERACIÓN DESCARTADA\n\n"
            f"{pair}\n"
            f"Motivo: dejó de estar disponible.",
            f"notopen_{pair}",
        )

        refresh_active_pairs(
            force=True
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


        OPEN_TRADES[oid] = {

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

    now = int(
        time.time()
    )

    for oid, trade in list(
        OPEN_TRADES.items()
    ):

        if now < trade["expiry"]:
            continue

        try:

            result = IQ.check_win_v4(
                oid
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
# MOSTRAR PARES ACTIVOS
# ============================================================

def send_active_pairs():

    pairs = ACTIVE_PAIRS

    if not pairs:
        return

    telegram_send(
        "🔎 PARES REALES EN ANÁLISIS\n\n"
        + "\n".join(
            f"• {p}"
            for p in pairs
        )
        + "\n\n"
        "🚫 Sin OTC\n"
        "🚫 Sin -OP\n"
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


    # ========================================================
    # TELEGRAM
    # ========================================================

    threading.Thread(
        target=telegram_worker,
        daemon=True,
    ).start()


    # ========================================================
    # PRIMERA BÚSQUEDA
    # ========================================================

    pairs = refresh_active_pairs(
        force=True
    )

    if pairs:

        telegram_send(
            "🤖 BOT LISTO\n\n"
            "🟢 Mercado REAL\n"
            "🚫 Sin OTC\n"
            "🚫 Sin -OP\n\n"
            f"🔎 Pares detectados: {len(pairs)}\n"
            + "\n".join(
                f"• {p}"
                for p in pairs
            )
            + "\n\n"
            "⏱ Expiración: 1 minuto.",
            "ready",
        )

    else:

        telegram_send(
            "🤖 BOT LISTO\n\n"
            "⚠️ Todavía no se detectaron "
            "pares reales disponibles.\n\n"
            "El bot continuará buscando "
            "automáticamente.",
            "ready_no_pairs",
        )


    # ========================================================
    # BUCLE PRINCIPAL
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # DETENIDO
            # ------------------------------------------------

            if not BOT_RUNNING:

                time.sleep(1)

                continue


            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            check_results()


            # ------------------------------------------------
            # ACTUALIZAR PARES
            # ------------------------------------------------

            pairs = refresh_active_pairs()


            # ------------------------------------------------
            # SIN PARES
            # ------------------------------------------------

            if not pairs:

                telegram_send(
                    "⚠️ SIN PARES REALES DISPONIBLES\n\n"
                    "🚫 No se usarán OTC.\n"
                    "🚫 No se usarán -OP.\n\n"
                    "🔎 Buscando nuevamente...",
                    "no_real_pairs",
                )

                time.sleep(5)

                continue


            # ------------------------------------------------
            # ANALIZAR CADA PAR
            # ------------------------------------------------

            for pair in list(
                pairs
            ):

                if not BOT_RUNNING:
                    break


                # --------------------------------------------
                # SEGURIDAD
                # --------------------------------------------

                if not is_real_pair(
                    pair
                ):
                    continue


                # --------------------------------------------
                # NO REPETIR PAR
                # --------------------------------------------

                if pair_has_open_trade(
                    pair
                ):

                    continue


                # --------------------------------------------
                # COMPROBAR DISPONIBILIDAD
                # --------------------------------------------

                if not pair_is_open(
                    pair
                ):

                    refresh_active_pairs(
                        force=True
                    )

                    continue


                # --------------------------------------------
                # ANALIZAR
                # --------------------------------------------

                result = analyze_pair(
                    pair
                )

                if not result:
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
                # EJECUTAR
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


                time.sleep(
                    0.3
                )


            time.sleep(
                1
            )


        except Exception as e:

            logging.error(
                "MAIN ERROR: %s",
                e,
            )

            time.sleep(
                2
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
