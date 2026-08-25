from __future__ import annotations

import os
import time
import threading
import requests
import pandas as pd

from typing import Dict, List, Any, Optional

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
# TIMEFRAMES
# ============================================================

TIMEFRAME_M1 = 60
TIMEFRAME_M5 = 300

M1_CANDLES = 60
M5_CANDLES = 30


# ============================================================
# OPERACIONES
# ============================================================

EXPIRATION = 1

MAX_OPEN_TRADES = 5

MIN_SCORE_TO_TRADE = 75

SCAN_INTERVAL = 2

REFRESH_INTERVAL = 300


# ============================================================
# 💰 MONEY MANAGEMENT
# ============================================================

BASE_AMOUNT = 10

CURRENT_AMOUNT = BASE_AMOUNT

MAX_AMOUNT = 200


WIN = 0
LOSS = 0


# ============================================================
# ⭐ 5 PARES PRINCIPALES
# ============================================================
#
# El bot intentará trabajar primero con estos 5.
#
# Si alguno NO está disponible:
#    ↓
# busca automáticamente un reemplazo
#    ↓
# dentro de FALLBACK_PAIRS
#
# IMPORTANTE:
# El bot nunca analiza todos los OTC.
# Solo analiza los 5 seleccionados.
#

PRIMARY_PAIRS = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "AUDUSD-OTC",
]


# ============================================================
# 🔄 PARES DE REEMPLAZO
# ============================================================

FALLBACK_PAIRS = [
    "EURGBP-OTC",
    "GBPJPY-OTC",
    "AUDJPY-OTC",
    "USDCAD-OTC",
    "USDCHF-OTC",
    "NZDUSD-OTC",
    "EURCAD-OTC",
    "GBPCAD-OTC",
    "CADJPY-OTC",
    "CHFJPY-OTC",
    "AUDCAD-OTC",
    "AUDCHF-OTC",
    "CADCHF-OTC",
    "NZDJPY-OTC",
    "EURCHF-OTC",
    "GBPCHF-OTC",
]


# ============================================================
# ESTADO DE PARES
# ============================================================

SELECTED_PAIRS: List[str] = []

AVAILABLE_OTC: List[str] = []

LAST_REFRESH = 0


# ============================================================
# ESTADO DEL BOT
# ============================================================

BOT_RUNNING = False

IQ = None


# ============================================================
# OPERACIONES ABIERTAS
# ============================================================

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}


# ============================================================
# CONTROL DE VELAS
# ============================================================

LAST_TRADE_CANDLE: Dict[str, int] = {}


# ============================================================
# TELEGRAM
# ============================================================

LAST_UPDATE = None


def send(
    msg: str,
    key: str = "msg",
    force: bool = False
):

    try:

        if not TELEGRAM_TOKEN:
            return

        if not TELEGRAM_CHAT_ID:
            return

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg
            },
            timeout=5
        )

    except Exception:
        pass


# ============================================================
# LIMPIAR NOMBRE DEL ACTIVO
# ============================================================

def clean_asset_name(
    name: str
) -> str:

    if not name:
        return ""

    name = str(name).strip()

    # --------------------------------------------------------
    # Elimina prefijos problemáticos
    # --------------------------------------------------------

    prefixes = [
        "front.",
        "binary.",
        "turbo.",
    ]

    for prefix in prefixes:

        if name.startswith(prefix):
            name = name[len(prefix):]

    # --------------------------------------------------------
    # Algunos activos pueden venir con espacios
    # --------------------------------------------------------

    name = name.strip()

    return name


# ============================================================
# OBTENER OTC DISPONIBLES
# ============================================================

def get_otc_pairs() -> List[str]:

    try:

        data = IQ.get_all_init_v2()

        pairs = set()

        for market_type in [
            "binary",
            "turbo"
        ]:

            market = data.get(
                market_type,
                {}
            )

            actives = market.get(
                "actives",
                {}
            )

            if not isinstance(
                actives,
                dict
            ):
                continue

            for active in actives.values():

                if not isinstance(
                    active,
                    dict
                ):
                    continue

                name = active.get(
                    "name",
                    ""
                )

                name = clean_asset_name(
                    name
                )

                if not name:
                    continue

                if not name.endswith(
                    "-OTC"
                ):
                    continue

                # ------------------------------------------------
                # Evitar basura
                # ------------------------------------------------

                if "front." in name.lower():
                    continue

                pairs.add(name)

        return sorted(
            pairs
        )

    except Exception as e:

        print(
            f"Error obteniendo OTC: {e}"
        )

        return []


# ============================================================
# COMPROBAR SI UN PAR EXISTE
# ============================================================

def pair_is_available(
    pair: str,
    available: List[str]
) -> bool:

    clean = clean_asset_name(
        pair
    )

    return clean in available


# ============================================================
# SELECCIONAR EXACTAMENTE HASTA 5 PARES
# ============================================================

def select_five_pairs(
    available: List[str]
) -> List[str]:

    selected = []

    # ========================================================
    # 1. PRIMERO LOS PRINCIPALES
    # ========================================================

    for pair in PRIMARY_PAIRS:

        clean = clean_asset_name(
            pair
        )

        if clean not in available:
            continue

        if clean in selected:
            continue

        selected.append(
            clean
        )

        if len(selected) >= 5:
            return selected

    # ========================================================
    # 2. SI FALTA ALGUNO,
    #    USAR REEMPLAZOS
    # ========================================================

    for pair in FALLBACK_PAIRS:

        clean = clean_asset_name(
            pair
        )

        if clean not in available:
            continue

        if clean in selected:
            continue

        selected.append(
            clean
        )

        if len(selected) >= 5:
            break

    return selected


# ============================================================
# REFRESCAR LOS 5 PARES
# ============================================================

def refresh_pairs(
    force: bool = False
):

    global AVAILABLE_OTC
    global SELECTED_PAIRS
    global LAST_REFRESH

    if not force:

        if (
            time.time() - LAST_REFRESH
            < REFRESH_INTERVAL
        ):
            return

    available = get_otc_pairs()

    if not available:

        print(
            "⚠️ No se encontraron OTC."
        )

        return

    new_selected = select_five_pairs(
        available
    )

    if not new_selected:

        print(
            "⚠️ No hay pares seleccionables."
        )

        return

    old_selected = list(
        SELECTED_PAIRS
    )

    AVAILABLE_OTC = available

    SELECTED_PAIRS = new_selected

    LAST_REFRESH = time.time()

    # ========================================================
    # INFORMAR SOLO SI CAMBIARON
    # ========================================================

    if old_selected != SELECTED_PAIRS:

        print(
            "\n=============================="
        )

        print(
            "⭐ PARES SELECCIONADOS"
        )

        for index, pair in enumerate(
            SELECTED_PAIRS,
            start=1
        ):

            print(
                f"{index}. {pair}"
            )

        print(
            "==============================\n"
        )

        send(
            (
                "🔄 PARES ACTUALIZADOS\n\n"
                "El bot trabajará solamente con:\n\n"
                + "\n".join(
                    f"{i}. {p}"
                    for i, p in enumerate(
                        SELECTED_PAIRS,
                        start=1
                    )
                )
            ),
            "pairs_changed",
            True
        )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_worker():

    global BOT_RUNNING
    global LAST_UPDATE

    while True:

        try:

            if not TELEGRAM_TOKEN:

                time.sleep(5)

                continue

            params = {}

            if LAST_UPDATE is not None:

                params["offset"] = (
                    LAST_UPDATE + 1
                )

            response = requests.get(
                (
                    "https://api.telegram.org/"
                    f"bot{TELEGRAM_TOKEN}/getUpdates"
                ),
                params=params,
                timeout=5
            )

            data = response.json()

            for update in data.get(
                "result",
                []
            ):

                LAST_UPDATE = update[
                    "update_id"
                ]

                message = update.get(
                    "message",
                    {}
                )

                text = (
                    message
                    .get("text", "")
                    .strip()
                    .lower()
                )

                chat_id = str(
                    message
                    .get("chat", {})
                    .get("id", "")
                )

                if (
                    chat_id
                    != str(TELEGRAM_CHAT_ID)
                ):
                    continue

                # ============================================
                # START
                # ============================================

                if text == "/start":

                    BOT_RUNNING = True

                    refresh_pairs(
                        force=True
                    )

                    send(
                        (
                            "🟢 BOT ACTIVADO\n\n"
                            "M1 + M5\n"
                            f"Máximo operaciones: "
                            f"{MAX_OPEN_TRADES}\n"
                            f"Expiración: "
                            f"{EXPIRATION} minuto\n\n"
                            "Pares actuales:\n"
                            + "\n".join(
                                SELECTED_PAIRS
                            )
                        ),
                        "start",
                        True
                    )

                # ============================================
                # STOP
                # ============================================

                elif text == "/stop":

                    BOT_RUNNING = False

                    send(
                        (
                            "🔴 BOT DETENIDO\n\n"
                            "No abrirá nuevas "
                            "operaciones.\n"
                            "Las operaciones abiertas "
                            "seguirán siendo controladas."
                        ),
                        "stop",
                        True
                    )

                # ============================================
                # STATUS
                # ============================================

                elif text == "/status":

                    active_pairs = [
                        trade["pair"]
                        for trade in
                        OPEN_TRADES.values()
                    ]

                    send(
                        (
                            "📊 ESTADO DEL BOT\n\n"
                            f"Estado: "
                            f"{'🟢 ON' if BOT_RUNNING else '🔴 OFF'}\n\n"
                            "⭐ Pares seleccionados:\n"
                            f"{chr(10).join(SELECTED_PAIRS)}\n\n"
                            f"Operaciones: "
                            f"{len(OPEN_TRADES)}/"
                            f"{MAX_OPEN_TRADES}\n"
                            f"WIN: {WIN}\n"
                            f"LOSS: {LOSS}\n"
                            f"Monto: ${CURRENT_AMOUNT}\n\n"
                            "Operaciones abiertas:\n"
                            f"{chr(10).join(active_pairs) if active_pairs else 'Ninguna'}"
                        ),
                        "status",
                        True
                    )

                # ============================================
                # PAIRS
                # ============================================

                elif text == "/pairs":

                    send(
                        (
                            "⭐ PARES ACTUALES\n\n"
                            + "\n".join(
                                f"{i}. {pair}"
                                for i, pair in enumerate(
                                    SELECTED_PAIRS,
                                    start=1
                                )
                            )
                        ),
                        "pairs",
                        True
                    )

        except Exception:
            pass

        time.sleep(1)


# ============================================================
# CONEXIÓN
# ============================================================

def connect():

    global IQ

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    IQ.connect()

    if IQ.check_connect():

        print(
            "🟢 Conectado a IQ Option"
        )

        send(
            "🟢 CONECTADO A IQ OPTION",
            "connect",
            True
        )

        return True

    print(
        "🔴 No se pudo conectar."
    )

    send(
        "🔴 ERROR DE CONEXIÓN IQ OPTION",
        "connection_error",
        True
    )

    return False


# ============================================================
# RECONEXIÓN
# ============================================================

def ensure_connection():

    global IQ

    try:

        if IQ is None:

            return connect()

        if IQ.check_connect():

            return True

        print(
            "⚠️ Conexión perdida. "
            "Intentando reconectar..."
        )

        IQ.connect()

        if IQ.check_connect():

            print(
                "🟢 Reconectado."
            )

            send(
                "🔄 IQ OPTION RECONectado",
                "reconnect",
                True
            )

            refresh_pairs(
                force=True
            )

            return True

    except Exception as e:

        print(
            f"Error reconectando: {e}"
        )

    return False


# ============================================================
# MONTO
# ============================================================

def update_amount(
    profit: float
):

    global CURRENT_AMOUNT

    if profit > 0:

        CURRENT_AMOUNT = min(
            round(
                CURRENT_AMOUNT * 1.10,
                2
            ),
            MAX_AMOUNT
        )

    else:

        CURRENT_AMOUNT = BASE_AMOUNT


# ============================================================
# RESULTADOS
# ============================================================

def check_results():

    global WIN
    global LOSS

    if not OPEN_TRADES:
        return

    for oid, trade in list(
        OPEN_TRADES.items()
    ):

        try:

            if (
                time.time()
                < trade["expiry"]
            ):
                continue

            result = IQ.check_win_v4(
                oid
            )

            if result is None:
                continue

            profit = float(
                result
            )

            if profit > 0:

                WIN += 1

                update_amount(
                    profit
                )

                status = "WIN 🟢"

            else:

                LOSS += 1

                update_amount(
                    profit
                )

                status = "LOSS 🔴"

            send(
                (
                    "📊 RESULTADO\n\n"
                    f"Par: {trade['pair']}\n"
                    f"Señal: "
                    f"{trade['signal'].upper()}\n"
                    f"Resultado: {status}\n"
                    f"Profit: ${profit:.2f}\n\n"
                    f"WIN: {WIN}\n"
                    f"LOSS: {LOSS}\n"
                    f"Próximo monto: "
                    f"${CURRENT_AMOUNT}"
                ),
                f"result_{oid}",
                True
            )

            del OPEN_TRADES[
                oid
            ]

        except Exception as e:

            print(
                f"Error comprobando "
                f"orden {oid}: {e}"
            )


# ============================================================
# PARES CON OPERACIONES ABIERTAS
# ============================================================

def get_open_pairs():

    return {
        trade["pair"]
        for trade in OPEN_TRADES.values()
    }


# ============================================================
# ÚLTIMA VELA
# ============================================================

def get_last_candle_id(
    candles: List[Dict[str, Any]]
):

    if not candles:
        return None

    try:

        return int(
            candles[-1].get(
                "from",
                0
            )
        )

    except Exception:

        return None


# ============================================================
# ANALIZAR UN PAR
# ============================================================

def analyze_pair(
    pair: str
) -> Optional[Dict[str, Any]]:

    try:

        # ====================================================
        # M1
        # ====================================================

        candles_m1 = IQ.get_candles(
            pair,
            TIMEFRAME_M1,
            M1_CANDLES,
            time.time()
        )

        if not candles_m1:
            return None

        if len(candles_m1) < 20:
            return None

        df_m1 = pd.DataFrame(
            candles_m1
        )

        # ====================================================
        # M5
        # ====================================================

        candles_m5 = IQ.get_candles(
            pair,
            TIMEFRAME_M5,
            M5_CANDLES,
            time.time()
        )

        if not candles_m5:
            return None

        if len(candles_m5) < 15:
            return None

        df_m5 = pd.DataFrame(
            candles_m5
        )

        # ====================================================
        # ÚLTIMA VELA M1
        # ====================================================

        last_candle = df_m1.iloc[
            -1
        ]

        # ====================================================
        # STRATEGY
        # ====================================================

        result = analyze_market(
            candle_1m=last_candle,
            previous_m1=df_m1,
            candles_m5=df_m5,
            pair=pair
        )

        if not result:
            return None

        if not result.get(
            "valid",
            False
        ):
            return None

        score = int(
            result.get(
                "score",
                0
            )
        )

        if score < MIN_SCORE_TO_TRADE:
            return None

        result["pair"] = pair

        result["candle_id"] = (
            get_last_candle_id(
                candles_m1
            )
        )

        return result

    except Exception as e:

        # No imprimir "Asset not found"
        # repetidamente si IQ rechaza un activo.
        message = str(e)

        if (
            "not found on consts"
            not in message.lower()
        ):

            print(
                f"Error analizando "
                f"{pair}: {e}"
            )

        return None


# ============================================================
# ANALIZAR SOLO LOS 5 PARES
# ============================================================

def analyze_selected_pairs():

    results = []

    open_pairs = get_open_pairs()

    # ========================================================
    # MUY IMPORTANTE:
    # SOLO SELECTED_PAIRS
    # ========================================================

    for pair in list(
        SELECTED_PAIRS
    ):

        # -----------------------------------------------
        # Si ya tiene operación abierta,
        # no buscar otra.
        # -----------------------------------------------

        if pair in open_pairs:
            continue

        result = analyze_pair(
            pair
        )

        if result is None:
            continue

        results.append(
            result
        )

    # ========================================================
    # ORDENAR
    # ========================================================

    results.sort(
        key=lambda x: (
            x.get(
                "score",
                0
            ),
            1 if x.get(
                "m5_confirmed",
                False
            ) else 0
        ),
        reverse=True
    )

    return results


# ============================================================
# CONTROL DE VELA
# ============================================================

def already_traded_this_candle(
    pair: str,
    candle_id: Any
) -> bool:

    if candle_id is None:
        return False

    return (
        LAST_TRADE_CANDLE.get(
            pair
        )
        == candle_id
    )


# ============================================================
# REGISTRAR OPERACIÓN
# ============================================================

def register_trade(
    order_id: int,
    result: Dict[str, Any]
):

    pair = result[
        "pair"
    ]

    candle_id = result.get(
        "candle_id"
    )

    OPEN_TRADES[
        order_id
    ] = {

        "pair": pair,

        "signal": result[
            "signal"
        ],

        "score": result[
            "score"
        ],

        "setup": result.get(
            "setup",
            ""
        ),

        "reason": result.get(
            "reason",
            ""
        ),

        "m5_direction": result.get(
            "m5_direction",
            "NEUTRAL"
        ),

        "m5_confirmed": result.get(
            "m5_confirmed",
            False
        ),

        "candle_id": candle_id,

        "open_time": time.time(),

        "expiry": (
            time.time()
            + EXPIRATION * 60
            + 5
        ),
    }

    if candle_id is not None:

        LAST_TRADE_CANDLE[
            pair
        ] = candle_id


# ============================================================
# ABRIR UNA OPERACIÓN
# ============================================================

def open_trade(
    result: Dict[str, Any]
) -> bool:

    pair = result[
        "pair"
    ]

    signal = result[
        "signal"
    ]

    score = int(
        result[
            "score"
        ]
    )

    candle_id = result.get(
        "candle_id"
    )

    # ========================================================
    # LÍMITE 5
    # ========================================================

    if len(OPEN_TRADES) >= MAX_OPEN_TRADES:
        return False

    # ========================================================
    # NO REPETIR PAR
    # ========================================================

    if pair in get_open_pairs():
        return False

    # ========================================================
    # NO REPETIR VELA
    # ========================================================

    if already_traded_this_candle(
        pair,
        candle_id
    ):
        return False

    # ========================================================
    # SCORE
    # ========================================================

    if score < MIN_SCORE_TO_TRADE:
        return False

    # ========================================================
    # M5 CONTRARIO
    # ========================================================

    m5_direction = result.get(
        "m5_direction",
        "NEUTRAL"
    )

    if (
        signal == "call"
        and m5_direction == "BEARISH"
    ):
        return False

    if (
        signal == "put"
        and m5_direction == "BULLISH"
    ):
        return False

    # ========================================================
    # CONEXIÓN
    # ========================================================

    if not ensure_connection():
        return False

    try:

        amount = CURRENT_AMOUNT

        ok, order_id = IQ.buy(
            amount,
            pair,
            signal,
            EXPIRATION
        )

        if not ok:

            print(
                f"❌ Orden rechazada: "
                f"{pair}"
            )

            return False

        # ====================================================
        # REGISTRAR
        # ====================================================

        register_trade(
            order_id,
            result
        )

        m5_status = (
            "CONFIRMADO ✅"
            if result.get(
                "m5_confirmed",
                False
            )
            else "NEUTRAL ⚪"
        )

        send(
            (
                "🚀 NUEVA OPERACIÓN\n\n"
                f"Par: {pair}\n"
                f"Señal: "
                f"{signal.upper()}\n"
                f"Monto: ${amount}\n"
                f"Score: {score}/100\n"
                f"Setup: "
                f"{result.get('setup', '')}\n"
                f"M1: "
                f"{result.get('direction', 'NEUTRAL')}\n"
                f"M5: "
                f"{m5_direction}\n"
                f"M5: {m5_status}\n\n"
                f"{result.get('reason', '')}"
            ),
            f"trade_{pair}",
            True
        )

        print(
            f"🚀 {pair} "
            f"{signal.upper()} "
            f"Score {score}"
        )

        return True

    except Exception as e:

        print(
            f"Error abriendo "
            f"{pair}: {e}"
        )

        return False


# ============================================================
# ABRIR LAS MEJORES
# ============================================================

def open_best_trades(
    results: List[Dict[str, Any]]
):

    if not results:
        return

    available_slots = (
        MAX_OPEN_TRADES
        -
        len(OPEN_TRADES)
    )

    if available_slots <= 0:
        return

    opened = 0

    for result in results:

        if opened >= available_slots:
            break

        if open_trade(
            result
        ):

            opened += 1

            # Pequeña separación
            # entre órdenes.
            time.sleep(0.5)


# ============================================================
# MAIN
# ============================================================

def main():

    global BOT_RUNNING

    # ========================================================
    # CONEXIÓN
    # ========================================================

    while not connect():

        print(
            "Reintentando conexión "
            "en 10 segundos..."
        )

        time.sleep(10)

    # ========================================================
    # TELEGRAM
    # ========================================================

    threading.Thread(
        target=telegram_worker,
        daemon=True
    ).start()

    # ========================================================
    # SELECCIONAR 5 PARES
    # ========================================================

    refresh_pairs(
        force=True
    )

    # ========================================================
    # MENSAJE INICIAL
    # ========================================================

    send(
        (
            "🤖 BOT LISTO\n\n"
            "⭐ Sistema de 5 pares\n"
            "M1 + M5\n"
            f"Máximo operaciones: "
            f"{MAX_OPEN_TRADES}\n"
            f"Expiración: "
            f"{EXPIRATION} minuto\n"
            f"Score mínimo: "
            f"{MIN_SCORE_TO_TRADE}\n\n"
            "Pares seleccionados:\n"
            +
            "\n".join(
                f"{i}. {pair}"
                for i, pair in enumerate(
                    SELECTED_PAIRS,
                    start=1
                )
            )
            +
            "\n\nUsa /START para comenzar."
        ),
        "ready",
        True
    )

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # CONEXIÓN
            # ------------------------------------------------

            if not ensure_connection():

                time.sleep(5)

                continue

            # ------------------------------------------------
            # ACTUALIZAR LOS 5 PARES
            # ------------------------------------------------

            refresh_pairs()

            # ------------------------------------------------
            # COMPROBAR RESULTADOS
            # ------------------------------------------------

            check_results()

            # ------------------------------------------------
            # BOT APAGADO
            # ------------------------------------------------

            if not BOT_RUNNING:

                time.sleep(1)

                continue

            # ------------------------------------------------
            # SI FALTAN PARES
            # ------------------------------------------------

            if len(
                SELECTED_PAIRS
            ) < 5:

                refresh_pairs(
                    force=True
                )

            # ------------------------------------------------
            # SI NO HAY PARES
            # ------------------------------------------------

            if not SELECTED_PAIRS:

                time.sleep(5)

                continue

            # ------------------------------------------------
            # SI YA HAY 5 OPERACIONES
            # ------------------------------------------------

            if len(
                OPEN_TRADES
            ) >= MAX_OPEN_TRADES:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # ------------------------------------------------
            # ANALIZAR SOLO 5
            # ------------------------------------------------

            results = (
                analyze_selected_pairs()
            )

            # ------------------------------------------------
            # ABRIR MEJORES
            # ------------------------------------------------

            if results:

                open_best_trades(
                    results
                )

            # ------------------------------------------------
            # ESPERAR
            # ------------------------------------------------

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            BOT_RUNNING = False

            print(
                "🛑 Bot detenido."
            )

            break

        except Exception as e:

            print(
                f"❌ Error principal: {e}"
            )

            time.sleep(3)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
