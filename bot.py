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
# CANTIDAD DE PARES
# ============================================================

MAX_ACTIVE_PAIRS = 6


# ============================================================
# PARES PREFERIDOS
# ============================================================
#
# NO SON PARES FIJOS.
#
# El bot primero intenta usar estos si están disponibles.
# Si alguno está cerrado, busca automáticamente otros
# pares reales disponibles.
#
# Nunca se aceptan pares OTC.
# ============================================================

PREFERRED_REAL_PAIRS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "EURCAD",
    "GBPCAD",
    "CADJPY",
    "CHFJPY",
]


# ============================================================
# GESTIÓN DE MONTO
# ============================================================

BASE_AMOUNT = 500

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

PAIR_REFRESH_INTERVAL = 10


# ============================================================
# CACHE DE PARES ABIERTOS
# ============================================================

OPEN_PAIR_CACHE: List[str] = []

LAST_OPEN_SCAN = 0

OPEN_SCAN_INTERVAL = 3


# ============================================================
# ANTI-SPAM TELEGRAM
# ============================================================

LAST_MSG = {}

COOLDOWN = 10


# ============================================================
# TELEGRAM COOLDOWN
# ============================================================

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

        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
            },
            timeout=5,
        )

        return response.ok

    except Exception:

        return False


# ============================================================
# ACTUALIZAR MONTO
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
            BASE_AMOUNT
            * (1 + WIN_STREAK * 0.5),
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

                LAST_UPDATE_ID = (
                    update["update_id"]
                )

                msg = update.get(
                    "message",
                    {},
                )

                text = msg.get(
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
                    msg.get(
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

                if text.startswith(
                    "/start"
                ):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "🔎 Buscando 6 pares REALES.\n"
                        "🚫 Sin OTC.\n"
                        "⏱ Solo expiración de 1 minuto.",
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
                        f"🌍 Mercado: REAL / NO OTC\n"
                        f"⏱ Expiración: "
                        f"{EXPIRATION} minuto\n\n"
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
# COMPROBAR / RESTAURAR CONEXIÓN
# ============================================================

def ensure_connection() -> bool:

    global IQ

    if IQ is None:

        return False

    try:

        if IQ.check_connect():

            return True

    except Exception:

        pass

    logging.error(
        "Conexión IQ Option perdida. "
        "Intentando reconectar..."
    )

    try:

        IQ.connect()

        time.sleep(2)

        if IQ.check_connect():

            telegram_send(
                "🔄 CONEXIÓN IQ OPTION RESTAURADA",
                "reconnect",
            )

            return True

    except Exception as e:

        logging.error(
            "Error reconectando IQ Option: %s",
            e,
        )

    return False


# ============================================================
# VALIDAR BOOLEANOS DE LA API
# ============================================================

def _is_enabled(
    value: Any,
) -> bool:

    if isinstance(
        value,
        bool,
    ):

        return value

    if isinstance(
        value,
        (int, float),
    ):

        return value != 0

    if isinstance(
        value,
        str,
    ):

        return value.strip().lower() in (
            "true",
            "1",
            "yes",
            "on",
        )

    return bool(value)


# ============================================================
# OBTENER NOMBRE DEL ACTIVO
# ============================================================

def _active_name(
    active: Dict[str, Any],
) -> Optional[str]:

    raw_name = active.get(
        "name"
    )

    if not raw_name:

        return None

    name = str(
        raw_name
    )

    if "." in name:

        name = name.split(
            "."
        )[-1]

    name = name.strip().upper()

    if not name:

        return None

    return name


# ============================================================
# OBTENER PARES REALES ABIERTOS
# ============================================================
#
# IMPORTANTE:
#
# NO usamos:
#
#     IQ.get_all_open_time()
#
# porque ese método también intenta consultar DIGITAL.
#
# En su lugar consultamos directamente:
#
#     IQ.get_all_init_v2()
#
# y únicamente:
#
#     turbo
#
# Esto permite trabajar con expiración de 1 minuto
# sin entrar en la parte DIGITAL que está provocando
# el error de tu captura.
# ============================================================

def get_open_pairs(
    force: bool = False,
) -> List[str]:

    global IQ
    global OPEN_PAIR_CACHE
    global LAST_OPEN_SCAN

    now = time.time()

    if (
        not force
        and now - LAST_OPEN_SCAN
        < OPEN_SCAN_INTERVAL
    ):

        return list(
            OPEN_PAIR_CACHE
        )

    if not ensure_connection():

        return list(
            OPEN_PAIR_CACHE
        )

    try:

        data = IQ.get_all_init_v2()

        if not isinstance(
            data,
            dict,
        ):

            logging.error(
                "get_all_init_v2 devolvió "
                "una respuesta inválida."
            )

            return list(
                OPEN_PAIR_CACHE
            )

        # ----------------------------------------------------
        # SOLO TURBO
        # ----------------------------------------------------

        turbo = data.get(
            "turbo",
            {},
        )

        if not isinstance(
            turbo,
            dict,
        ):

            logging.error(
                "No se encontró información "
                "turbo válida."
            )

            return list(
                OPEN_PAIR_CACHE
            )

        actives = turbo.get(
            "actives",
            {},
        )

        if not isinstance(
            actives,
            dict,
        ):

            logging.error(
                "turbo.actives no es válido."
            )

            return list(
                OPEN_PAIR_CACHE
            )

        result = []

        for active in actives.values():

            if not isinstance(
                active,
                dict,
            ):

                continue

            name = _active_name(
                active
            )

            if not name:

                continue


            # =================================================
            # BLOQUEAR OTC
            # =================================================

            if "OTC" in name:

                continue


            # =================================================
            # ACTIVO ABIERTO
            # =================================================

            enabled = _is_enabled(
                active.get(
                    "enabled",
                    False,
                )
            )

            suspended = _is_enabled(
                active.get(
                    "is_suspended",
                    False,
                )
            )

            if not enabled:

                continue

            if suspended:

                continue

            if name not in result:

                result.append(
                    name
                )


        # =====================================================
        # PRIORIZAR PARES PRINCIPALES
        # =====================================================
        #
        # No son fijos.
        #
        # Si EURUSD está cerrado, se salta.
        # Si GBPUSD está cerrado, se salta.
        # Y así sucesivamente.
        #
        # Después se completan los 6 con cualquier otro
        # par real disponible.
        # =====================================================

        preferred = [
            pair
            for pair in PREFERRED_REAL_PAIRS
            if pair in result
        ]

        remaining = [
            pair
            for pair in result
            if pair not in preferred
        ]

        ordered = (
            preferred
            + remaining
        )

        OPEN_PAIR_CACHE = ordered

        LAST_OPEN_SCAN = now

        return list(
            OPEN_PAIR_CACHE
        )

    except Exception as e:

        logging.error(
            "Error obteniendo pares "
            "turbo reales: %s",
            e,
        )

        return list(
            OPEN_PAIR_CACHE
        )


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

    open_pairs = get_open_pairs(
        force=True
    )

    old_pairs = set(
        ACTIVE_PAIRS
    )

    new_pairs = []

    for pair in open_pairs:

        if pair not in new_pairs:

            new_pairs.append(
                pair
            )

        if len(new_pairs) >= MAX_ACTIVE_PAIRS:

            break


    # ========================================================
    # CAMBIOS
    # ========================================================

    new_set = set(
        new_pairs
    )

    removed = (
        old_pairs - new_set
    )

    added = (
        new_set - old_pairs
    )


    # ========================================================
    # PARES CERRADOS
    # ========================================================

    for pair in removed:

        telegram_send(
            f"🔴 PAR DESCARTADO\n\n"
            f"{pair}\n"
            f"Motivo: mercado cerrado, "
            f"suspendido o ya no disponible "
            f"para 1 minuto.",
            f"closed_{pair}",
        )


    # ========================================================
    # PARES NUEVOS
    # ========================================================

    for pair in added:

        telegram_send(
            f"🟢 PAR DISPONIBLE\n\n"
            f"{pair}\n"
            f"🌍 Mercado real / NO OTC\n"
            f"⏱ Expiración: 1 minuto",
            f"open_{pair}",
        )


    ACTIVE_PAIRS = new_pairs

    return ACTIVE_PAIRS


# ============================================================
# COMPROBAR SI UN PAR SIGUE ABIERTO
# ============================================================

def pair_is_open(
    pair: str,
) -> bool:

    open_pairs = get_open_pairs()

    return pair in open_pairs


# ============================================================
# OBTENER VELAS
# ============================================================

def get_market_data(
    pair: str,
):

    global IQ

    if IQ is None:

        return None

    if not ensure_connection():

        return None

    try:

        # ----------------------------------------------------
        # USAR HORA DEL SERVIDOR
        # ----------------------------------------------------

        try:

            end_time = (
                IQ.get_server_timestamp()
            )

        except Exception:

            end_time = time.time()


        candles = IQ.get_candles(
            pair,
            TIMEFRAME,
            60,
            end_time,
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

    df = get_market_data(
        pair
    )

    if df is None:

        return None

    # strategy.py ahora utiliza
    # mínimo 12 velas.

    if len(df) < 12:

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

    if signal not in (
        "call",
        "put",
    ):

        return False


    # ========================================================
    # BLOQUEO ABSOLUTO DE OTC
    # ========================================================

    if "OTC" in str(
        pair
    ).upper():

        logging.error(
            "Operación OTC bloqueada: %s",
            pair,
        )

        return False


    # ========================================================
    # ÚLTIMA COMPROBACIÓN
    # ========================================================

    if not pair_is_open(
        pair
    ):

        telegram_send(
            f"⚠️ OPERACIÓN DESCARTADA\n\n"
            f"{pair}\n"
            f"Motivo: dejó de estar disponible "
            f"para expiración de 1 minuto.",
            f"notopen_{pair}",
        )

        refresh_active_pairs(
            force=True
        )

        return False


    try:

        amount = CURRENT_AMOUNT


        # ====================================================
        # COMPRA
        # ====================================================

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
                f"Expiración: "
                f"{EXPIRATION} min",
                f"buyfail_{pair}",
            )

            return False


        # ====================================================
        # GUARDAR OPERACIÓN
        # ====================================================

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
            f"🌍 Mercado: REAL / NO OTC\n"
            f"📈 Señal: {signal.upper()}\n"
            f"🎯 Score: {score}\n"
            f"💵 Monto: ${amount}\n"
            f"⏱ Expiración: "
            f"{EXPIRATION} minuto",
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

        if now < trade[
            "expiry"
        ]:

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

                outcome = (
                    "WIN 🟢"
                )

            elif profit < 0:

                outcome = (
                    "LOSS 🔴"
                )

            else:

                outcome = (
                    "EMPATE ⚪"
                )


            telegram_send(
                f"📊 RESULTADO\n\n"
                f"📊 {trade['pair']}\n"
                f"📈 "
                f"{trade['signal'].upper()}\n"
                f"{outcome}\n"
                f"💰 Resultado: {profit}\n\n"
                f"📈 WIN: {WIN_STREAK}\n"
                f"📉 LOSS: {LOSS_STREAK}\n"
                f"💵 Próximo monto: "
                f"${CURRENT_AMOUNT}",
                f"result_{oid}",
            )

            del OPEN_TRADES[
                oid
            ]

        except Exception as e:

            logging.error(
                "Error resultado %s: %s",
                oid,
                e,
            )


# ============================================================
# EVITAR VARIAS OPERACIONES MISMO PAR
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
        "🔎 PARES EN ANÁLISIS\n\n"
        + "\n".join(
            f"• {p}"
            for p in pairs
        )
        + "\n\n"
        "🌍 Mercado: REAL / NO OTC\n"
        f"⏱ Expiración: "
        f"{EXPIRATION} minuto",
        "active_pairs",
    )


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
    # CONEXIÓN
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
        "🌍 Mercado: REAL / NO OTC\n"
        "⏱ Expiración: 1 minuto\n"
        "🔎 Buscando automáticamente "
        "hasta 6 pares disponibles "
        "para 1 minuto.",
        "ready",
    )


    # ========================================================
    # BUCLE PRINCIPAL
    # ========================================================

    while True:

        try:


            # =================================================
            # BOT DETENIDO
            # =================================================

            if not BOT_RUNNING:

                time.sleep(1)

                continue


            # =================================================
            # COMPROBAR CONEXIÓN
            # =================================================

            if not ensure_connection():

                time.sleep(3)

                continue


            # =================================================
            # RESULTADOS
            # =================================================

            check_results()


            # =================================================
            # ACTUALIZAR LOS 6 PARES
            # =================================================

            pairs = refresh_active_pairs()


            # =================================================
            # SIN PARES
            # =================================================

            if not pairs:

                telegram_send(
                    "⚠️ SIN PARES DISPONIBLES\n\n"
                    "No hay pares reales NO OTC "
                    "disponibles para 1 minuto.\n"
                    "Buscando nuevamente...",
                    "no_pairs",
                )

                time.sleep(3)

                continue


            # =================================================
            # ANALIZAR LOS 6 PARES
            # =================================================

            for pair in list(
                pairs
            ):


                # ---------------------------------------------
                # STOP
                # ---------------------------------------------

                if not BOT_RUNNING:

                    break


                # ---------------------------------------------
                # NO OPERAR DOS VECES EL MISMO PAR
                # ---------------------------------------------

                if pair_has_open_trade(
                    pair
                ):

                    continue


                # ---------------------------------------------
                # BLOQUEO OTC
                # ---------------------------------------------

                if "OTC" in pair.upper():

                    continue


                # ---------------------------------------------
                # COMPROBAR QUE SIGUE DISPONIBLE
                # ---------------------------------------------

                if not pair_is_open(
                    pair
                ):

                    refresh_active_pairs(
                        force=True
                    )

                    continue


                # ---------------------------------------------
                # ANALIZAR
                # ---------------------------------------------

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


                # ---------------------------------------------
                # SOLO OPERAR SI STRATEGY
                # AUTORIZA
                # ---------------------------------------------

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


                # ---------------------------------------------
                # PAUSA API
                # ---------------------------------------------

                time.sleep(
                    0.3
                )


            # =================================================
            # SIGUIENTE CICLO
            # =================================================

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
