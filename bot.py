from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, List

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code

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

# Expiración de la operación
EXPIRATION = 1


# ============================================================
# CONFIGURACIÓN DE PARES
# ============================================================

# IMPORTANTE:
#
# Ya NO utilizamos una lista fija de pares.
#
# El bot obtiene automáticamente los activos TURBO
# disponibles desde IQ Option.
#
# Después elimina:
#
# - OTC
# - -OP
# - activos cerrados
#
# y conserva hasta 6 pares reales.

MAX_ACTIVE_PAIRS = 6


# ============================================================
# REFRESH DE MERCADO
# ============================================================

PAIR_REFRESH_INTERVAL = 15

LAST_PAIR_REFRESH = 0

ACTIVE_PAIRS: List[str] = []


# ============================================================
# GESTIÓN DE MONTO
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
# CONTROL DE ACTIVOS
# ============================================================

# Guardamos los activos Turbo conocidos.

TURBO_ASSETS: Dict[str, Dict[str, Any]] = {}


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


                # ====================================================
                # START
                # ====================================================

                if text.startswith(
                    "/start"
                ):

                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "Buscando automáticamente "
                        "6 pares reales.\n"
                        "🚫 Sin OTC\n"
                        "🚫 Sin -OP\n"
                        "⏱ Expiración: 1 minuto.",
                        "start",
                    )


                # ====================================================
                # STOP
                # ====================================================

                elif text.startswith(
                    "/stop"
                ):

                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO",
                        "stop",
                    )


                # ====================================================
                # STATUS
                # ====================================================

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
                        f"⏱ Expiración: {EXPIRATION} minuto\n\n"
                        f"📊 Pares reales activos:\n"
                        f"{pairs}",
                        "status",
                    )

        except Exception as e:

            logging.debug(
                "Telegram worker: %s",
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
# EXTRAER ACTIVOS TURBO
# ============================================================

def load_turbo_assets() -> Dict[str, Dict[str, Any]]:

    global IQ
    global TURBO_ASSETS

    if IQ is None:

        return {}

    try:

        # ========================================================
        # IMPORTANTE
        #
        # Usamos get_all_init() directamente.
        #
        # NO usamos get_all_open_time().
        #
        # Esto evita el error:
        #
        # TypeError: 'NoneType' object is not subscriptable
        #
        # que aparece al consultar digital underlying.
        # ========================================================

        init_data = IQ.get_all_init()

        if not init_data:

            return {}

        result = init_data.get(
            "result",
            {},
        )

        turbo_data = result.get(
            "turbo",
            {},
        )

        turbo_actives = turbo_data.get(
            "actives",
            {},
        )

        if not isinstance(
            turbo_actives,
            dict,
        ):

            return {}

        assets = {}

        for active_id, active in turbo_actives.items():

            if not isinstance(
                active,
                dict,
            ):

                continue

            # ----------------------------------------------------
            # Nombre original
            # ----------------------------------------------------

            raw_name = str(
                active.get(
                    "name",
                    "",
                )
            ).strip()

            if not raw_name:

                continue

            # Normalmente viene como:
            #
            # turbo.EURUSD
            #
            # turbo.EURUSD-OTC
            #
            # turbo.EURUSD-OP

            if "." in raw_name:

                pair = raw_name.split(
                    ".",
                    1
                )[1]

            else:

                pair = raw_name

            pair = pair.strip()

            if not pair:

                continue

            pair_upper = pair.upper()


            # ====================================================
            # FILTRO OTC
            # ====================================================

            if "OTC" in pair_upper:

                continue


            # ====================================================
            # FILTRO -OP
            # ====================================================

            if pair_upper.endswith(
                "-OP"
            ):

                continue


            # ====================================================
            # FILTRO DE FORMATO
            # ====================================================

            # Queremos principalmente pares Forex:
            #
            # EURUSD
            # GBPUSD
            # USDJPY
            # EURGBP
            # EURJPY
            # GBPJPY
            #
            # No queremos:
            #
            # BTC...
            # GOLD
            # SPX...
            # etc.

            if len(pair) != 6:

                continue

            if not pair.isalpha():

                continue


            # ====================================================
            # VALIDAR ESTADO
            # ====================================================

            enabled = active.get(
                "enabled",
                False,
            )

            suspended = active.get(
                "is_suspended",
                False,
            )

            if not enabled:

                continue

            if suspended:

                continue


            # ====================================================
            # REGISTRAR ACTIVO
            # ====================================================

            try:

                active_id_int = int(
                    active_id
                )

            except Exception:

                continue


            assets[pair] = {

                "id": active_id_int,

                "name": pair,

                "enabled": True,

                "suspended": False,

            }


            # ====================================================
            # ACTUALIZAR CONSTANTS.ACTIVES
            # ====================================================

            #
            # Esto es importante.
            #
            # IQ.buy() utiliza:
            #
            # OP_code.ACTIVES[ACTIVES]
            #
            # Por eso registramos dinámicamente:
            #
            # EURUSD -> ID
            # GBPUSD -> ID
            # etc.
            #

            OP_code.ACTIVES[
                pair
            ] = active_id_int


        TURBO_ASSETS = assets

        return assets


    except Exception as e:

        logging.error(
            "Error cargando activos Turbo: %s",
            e,
        )

        return {}


# ============================================================
# OBTENER PARES REALES ABIERTOS
# ============================================================

def get_real_open_pairs() -> List[str]:

    global IQ

    if IQ is None:

        return []

    try:

        assets = load_turbo_assets()

        if not assets:

            return []

        result = []

        for pair, data in assets.items():

            if not data.get(
                "enabled",
                False,
            ):

                continue

            if data.get(
                "suspended",
                False,
            ):

                continue

            # ------------------------------------------------
            # SEGURIDAD EXTRA
            # ------------------------------------------------

            pair_upper = pair.upper()

            if "OTC" in pair_upper:

                continue

            if pair_upper.endswith(
                "-OP"
            ):

                continue

            # ------------------------------------------------
            # Solo pares Forex de 6 letras
            # ------------------------------------------------

            if len(pair) != 6:

                continue

            if not pair.isalpha():

                continue

            result.append(pair)


        # ====================================================
        # ORDENAR
        # ====================================================

        result.sort()

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

    open_pairs = get_real_open_pairs()

    if not open_pairs:

        return ACTIVE_PAIRS


    # ========================================================
    # SELECCIONAR MÁXIMO 6
    # ========================================================

    new_pairs = []

    for pair in open_pairs:

        if pair in new_pairs:

            continue

        new_pairs.append(pair)

        if len(new_pairs) >= MAX_ACTIVE_PAIRS:

            break


    old_pairs = set(
        ACTIVE_PAIRS
    )

    new_set = set(
        new_pairs
    )

    removed = (
        old_pairs
        - new_set
    )

    added = (
        new_set
        - old_pairs
    )


    # ========================================================
    # PARES DESCARTADOS
    # ========================================================

    for pair in removed:

        telegram_send(
            f"🔴 PAR DESCARTADO\n\n"
            f"{pair}\n"
            f"Motivo: ya no está disponible.",
            f"closed_{pair}",
        )


    # ========================================================
    # PARES NUEVOS
    # ========================================================

    for pair in added:

        telegram_send(
            f"🟢 NUEVO PAR REAL\n\n"
            f"{pair}\n"
            f"🚫 Sin OTC\n"
            f"🚫 Sin -OP\n"
            f"⏱ Expiración: {EXPIRATION} minuto",
            f"open_{pair}",
        )


    ACTIVE_PAIRS = new_pairs

    return ACTIVE_PAIRS


# ============================================================
# COMPROBAR SI EL PAR SIGUE DISPONIBLE
# ============================================================

def pair_is_open(
    pair: str,
) -> bool:

    pair = str(
        pair
    ).strip()

    if not pair:

        return False

    # --------------------------------------------------------
    # Nunca permitir OTC
    # --------------------------------------------------------

    if "OTC" in pair.upper():

        return False

    # --------------------------------------------------------
    # Nunca permitir -OP
    # --------------------------------------------------------

    if pair.upper().endswith(
        "-OP"
    ):

        return False


    # --------------------------------------------------------
    # Si está en la lista activa
    # --------------------------------------------------------

    if pair in ACTIVE_PAIRS:

        return True


    # --------------------------------------------------------
    # Actualización forzada
    # --------------------------------------------------------

    pairs = get_real_open_pairs()

    return pair in pairs


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
    # Seguridad
    # --------------------------------------------------------

    if "OTC" in pair.upper():

        return None

    if pair.upper().endswith(
        "-OP"
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

        # ----------------------------------------------------
        # Convertir precios
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
    # No analizar OTC
    # --------------------------------------------------------

    if "OTC" in pair.upper():

        return None

    # --------------------------------------------------------
    # No analizar -OP
    # --------------------------------------------------------

    if pair.upper().endswith(
        "-OP"
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

    if signal not in (
        "call",
        "put",
    ):

        return False


    # ========================================================
    # SEGURIDAD: NUNCA OTC
    # ========================================================

    if "OTC" in pair.upper():

        logging.warning(
            "Operación bloqueada OTC: %s",
            pair,
        )

        return False


    # ========================================================
    # SEGURIDAD: NUNCA -OP
    # ========================================================

    if pair.upper().endswith(
        "-OP"
    ):

        logging.warning(
            "Operación bloqueada -OP: %s",
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
            f"Motivo: el par ya no está disponible.",
            f"notopen_{pair}",
        )

        return False


    # ========================================================
    # COMPROBAR ID
    # ========================================================

    if pair not in OP_code.ACTIVES:

        # Intentar actualizar activos

        load_turbo_assets()

    if pair not in OP_code.ACTIVES:

        logging.error(
            "Activo %s no existe en ACTIVES.",
            pair,
        )

        telegram_send(
            f"⚠️ ACTIVO NO REGISTRADO\n\n"
            f"{pair}\n"
            f"No se pudo obtener su ID interno.",
            f"noactive_{pair}",
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
                f"📊 Par: {pair}\n"
                f"📈 Señal: {signal.upper()}\n"
                f"🎯 Score: {score}\n"
                f"⏱ Expiración: {EXPIRATION} min",
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
# EVITAR VARIAS OPERACIONES DEL MISMO PAR
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
        f"🚫 OTC: NO\n"
        f"🚫 -OP: NO\n"
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


    # ========================================================
    # CARGAR ACTIVOS INICIALES
    # ========================================================

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "🔎 Detectando automáticamente "
        "pares reales.\n"
        "🚫 OTC excluido\n"
        "🚫 -OP excluido\n"
        "📊 Máximo: 6 pares\n"
        "⏱ Expiración: 1 minuto",
        "ready",
    )


    # ========================================================
    # BUCLE PRINCIPAL
    # ========================================================

    while True:

        try:

            # ====================================================
            # BOT DETENIDO
            # ====================================================

            if not BOT_RUNNING:

                time.sleep(1)

                continue


            # ====================================================
            # RESULTADOS
            # ====================================================

            check_results()


            # ====================================================
            # ACTUALIZAR PARES
            # ====================================================

            pairs = refresh_active_pairs()


            # ====================================================
            # SI NO HAY PARES
            # ====================================================

            if not pairs:

                telegram_send(
                    "⚠️ SIN PARES REALES DISPONIBLES\n\n"
                    "Buscando nuevamente...",
                    "no_pairs",
                )

                time.sleep(3)

                continue


            # ====================================================
            # ANALIZAR CADA PAR
            # ====================================================

            for pair in list(
                pairs
            ):

                if not BOT_RUNNING:

                    break


                # ------------------------------------------------
                # SEGURIDAD OTC
                # ------------------------------------------------

                if "OTC" in pair.upper():

                    continue


                # ------------------------------------------------
                # SEGURIDAD -OP
                # ------------------------------------------------

                if pair.upper().endswith(
                    "-OP"
                ):

                    continue


                # ------------------------------------------------
                # EVITAR OPERAR EL MISMO PAR
                # ------------------------------------------------

                if pair_has_open_trade(
                    pair
                ):

                    continue


                # ------------------------------------------------
                # COMPROBAR QUE SIGUE ABIERTO
                # ------------------------------------------------

                if not pair_is_open(
                    pair
                ):

                    refresh_active_pairs(
                        force=True
                    )

                    continue


                # ------------------------------------------------
                # ANALIZAR
                # ------------------------------------------------

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


                # ------------------------------------------------
                # EJECUTAR SOLO SI STRATEGY AUTORIZA
                # ------------------------------------------------

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


                # ------------------------------------------------
                # PAUSA
                # ------------------------------------------------

                time.sleep(
                    0.3
                )


            # ====================================================
            # LOOP
            # ====================================================

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
