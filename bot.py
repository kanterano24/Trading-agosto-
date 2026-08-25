from __future__ import annotations

import os
import time
import threading
import requests
import pandas as pd

from typing import Dict, List, Any

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_market


# ============================================================
# CONFIG
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
# TRADE CONFIG
# ============================================================

EXPIRATION = 1

MAX_OPEN_TRADES = 5

MIN_SCORE_TO_TRADE = 75

SCAN_INTERVAL = 1

# Evita volver a analizar/entrar varias veces
# sobre la misma vela M1.
CANDLE_COOLDOWN = 2


# ============================================================
# MONEY MANAGEMENT
# ============================================================

BASE_AMOUNT = 10

CURRENT_AMOUNT = BASE_AMOUNT

MAX_AMOUNT = 200

WIN = 0
LOSS = 0


# ============================================================
# STATE
# ============================================================

BOT_RUNNING = False

IQ = None

AVAILABLE_PAIRS: List[str] = []

LAST_REFRESH = 0

REFRESH_INTERVAL = 900


OPEN_TRADES: Dict[int, Dict[str, Any]] = {}


# Última vela operada por cada par.
LAST_TRADE_CANDLE: Dict[str, int] = {}


# ============================================================
# TELEGRAM
# ============================================================

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
# OTC
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

            for active in actives.values():

                name = active.get(
                    "name",
                    ""
                )

                if (
                    name
                    and
                    "-OTC" in name
                ):
                    pairs.add(name)

        return sorted(
            list(pairs)
        )

    except Exception as e:

        print(
            f"Error obteniendo OTC: {e}"
        )

        return []


def refresh_pairs():

    global AVAILABLE_PAIRS
    global LAST_REFRESH

    if (
        time.time() - LAST_REFRESH
        < REFRESH_INTERVAL
    ):
        return

    pairs = get_otc_pairs()

    if pairs:

        AVAILABLE_PAIRS = pairs

        LAST_REFRESH = time.time()

        send(
            (
                "🔄 OTC ACTUALIZADOS\n\n"
                f"Pares disponibles: "
                f"{len(AVAILABLE_PAIRS)}\n"
                "El bot continuará buscando "
                "las mejores oportunidades."
            ),
            "refresh",
            True
        )


# ============================================================
# TELEGRAM CONTROL
# ============================================================

LAST_UPDATE = None


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

                # --------------------------------------------
                # START
                # --------------------------------------------

                if text == "/start":

                    BOT_RUNNING = True

                    send(
                        (
                            "🟢 BOT ACTIVADO\n\n"
                            "Analizando OTC durante "
                            "todo el día.\n"
                            f"Máximo operaciones: "
                            f"{MAX_OPEN_TRADES}\n"
                            "M1 + M5"
                        ),
                        "start",
                        True
                    )

                # --------------------------------------------
                # STOP
                # --------------------------------------------

                elif text == "/stop":

                    BOT_RUNNING = False

                    send(
                        (
                            "🔴 BOT DETENIDO\n\n"
                            "No abrirá nuevas "
                            "operaciones.\n"
                            "Las operaciones abiertas "
                            "continuarán siendo controladas."
                        ),
                        "stop",
                        True
                    )

                # --------------------------------------------
                # STATUS
                # --------------------------------------------

                elif text == "/status":

                    active_pairs = [
                        t["pair"]
                        for t in OPEN_TRADES.values()
                    ]

                    send(
                        (
                            "📊 ESTADO DEL BOT\n\n"
                            f"Estado: "
                            f"{'🟢 ON' if BOT_RUNNING else '🔴 OFF'}\n"
                            f"OTC disponibles: "
                            f"{len(AVAILABLE_PAIRS)}\n"
                            f"Operaciones abiertas: "
                            f"{len(OPEN_TRADES)}/"
                            f"{MAX_OPEN_TRADES}\n"
                            f"WIN: {WIN}\n"
                            f"LOSS: {LOSS}\n"
                            f"Capital por operación: "
                            f"${CURRENT_AMOUNT}\n\n"
                            f"Pares activos: "
                            f"{', '.join(active_pairs) if active_pairs else 'Ninguno'}"
                        ),
                        "status",
                        True
                    )

        except Exception:
            pass

        time.sleep(1)


# ============================================================
# CONNECTION
# ============================================================

def connect():

    global IQ

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    IQ.connect()

    if IQ.check_connect():

        send(
            "🟢 CONECTADO A IQ OPTION",
            "connect",
            True
        )

    else:

        send(
            "🔴 ERROR DE CONEXIÓN IQ OPTION",
            "connect_error",
            True
        )

        raise RuntimeError(
            "No se pudo conectar a IQ Option"
        )


# ============================================================
# RECONNECT
# ============================================================

def ensure_connection():

    global IQ

    try:

        if IQ is None:
            connect()
            return True

        if IQ.check_connect():
            return True

        print(
            "⚠️ Conexión perdida. Reconectando..."
        )

        IQ.connect()

        if IQ.check_connect():

            send(
                "🔄 IQ OPTION RECONectado",
                "reconnect",
                True
            )

            return True

    except Exception as e:

        print(
            f"Error reconectando: {e}"
        )

    return False


# ============================================================
# CURRENT AMOUNT
# ============================================================

def update_amount(
    profit: float
):

    global CURRENT_AMOUNT

    # --------------------------------------------------------
    # WIN
    # --------------------------------------------------------

    if profit > 0:

        # Crecimiento moderado.
        # No depende del número total de wins.
        CURRENT_AMOUNT = min(
            max(
                BASE_AMOUNT,
                round(
                    CURRENT_AMOUNT * 1.10,
                    2
                )
            ),
            MAX_AMOUNT
        )

    # --------------------------------------------------------
    # LOSS
    # --------------------------------------------------------

    else:

        # Después de pérdida,
        # vuelve a la base.
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

                result_text = "WIN 🟢"

            else:

                LOSS += 1

                update_amount(
                    profit
                )

                result_text = "LOSS 🔴"

            send(
                (
                    "📊 RESULTADO\n\n"
                    f"Par: {trade['pair']}\n"
                    f"Dirección: "
                    f"{trade['signal'].upper()}\n"
                    f"Resultado: "
                    f"{result_text}\n"
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
                f"Error resultado {oid}: {e}"
            )


# ============================================================
# OPEN PAIRS
# ============================================================

def get_open_pairs():

    return {
        trade["pair"]
        for trade in OPEN_TRADES.values()
    }


# ============================================================
# GET CANDLE ID
# ============================================================

def get_last_candle_id(
    candles: List[Dict[str, Any]]
):

    if not candles:
        return None

    last = candles[-1]

    return int(
        last.get(
            "from",
            0
        )
    )


# ============================================================
# ANALYZE ONE PAIR
# ============================================================

def analyze_pair(
    pair: str
):

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
        # ÚLTIMA VELA
        # ====================================================

        last_candle = df_m1.iloc[
            -1
        ]

        # ====================================================
        # ESTRATEGIA
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

        print(
            f"Error analizando "
            f"{pair}: {e}"
        )

        return None


# ============================================================
# ANALYZE ALL OTC
# ============================================================

def analyze_all():

    results = []

    open_pairs = get_open_pairs()

    for pair in AVAILABLE_PAIRS:

        # ----------------------------------------------------
        # No analizar para entrada
        # si ya tenemos operación.
        # ----------------------------------------------------

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
    # ORDENAR POR SCORE
    # ========================================================

    results.sort(
        key=lambda x: (
            x.get("score", 0),
            1 if x.get(
                "m5_confirmed",
                False
            ) else 0
        ),
        reverse=True
    )

    return results


# ============================================================
# CHECK SAME CANDLE
# ============================================================

def already_traded_this_candle(
    pair: str,
    candle_id: Any
) -> bool:

    if candle_id is None:
        return False

    previous = LAST_TRADE_CANDLE.get(
        pair
    )

    return previous == candle_id


# ============================================================
# REGISTER TRADE
# ============================================================

def register_trade(
    oid: int,
    result: Dict[str, Any]
):

    pair = result["pair"]

    candle_id = result.get(
        "candle_id"
    )

    OPEN_TRADES[oid] = {
        "pair": pair,
        "signal": result["signal"],
        "score": result["score"],
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
        )
    }

    if candle_id is not None:

        LAST_TRADE_CANDLE[
            pair
        ] = candle_id


# ============================================================
# OPEN TRADE
# ============================================================

def open_trade(
    result: Dict[str, Any]
):

    pair = result["pair"]

    signal = result["signal"]

    score = result["score"]

    candle_id = result.get(
        "candle_id"
    )

    # ========================================================
    # CONTROL DE OPERACIONES
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
    # VALIDAR SCORE
    # ========================================================

    if score < MIN_SCORE_TO_TRADE:
        return False

    # ========================================================
    # COMPROBAR CONEXIÓN
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
                f"❌ No se pudo abrir "
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
            "✅ CONFIRMADO"
            if result.get(
                "m5_confirmed",
                False
            )
            else "⚪ NEUTRAL"
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
                f"{result.get('m5_direction', 'NEUTRAL')}\n"
                f"M5: {m5_status}\n\n"
                f"{result.get('reason', '')}"
            ),
            f"trade_{pair}",
            True
        )

        return True

    except Exception as e:

        print(
            f"Error abriendo "
            f"{pair}: {e}"
        )

        return False


# ============================================================
# OPEN BEST TRADES
# ============================================================

def open_best_trades(
    results: List[Dict[str, Any]]
):

    if not results:
        return

    available_slots = (
        MAX_OPEN_TRADES
        - len(OPEN_TRADES)
    )

    if available_slots <= 0:
        return

    opened = 0

    used_pairs = set()

    for result in results:

        if opened >= available_slots:
            break

        pair = result["pair"]

        if pair in used_pairs:
            continue

        # ----------------------------------------------------
        # Solo aceptar señal fuerte.
        # ----------------------------------------------------

        if result["score"] < MIN_SCORE_TO_TRADE:
            continue

        # ----------------------------------------------------
        # Si M5 contradice, no entrar.
        # ----------------------------------------------------

        if (
            result["signal"] == "call"
            and
            result.get(
                "m5_direction"
            ) == "BEARISH"
        ):
            continue

        if (
            result["signal"] == "put"
            and
            result.get(
                "m5_direction"
            ) == "BULLISH"
        ):
            continue

        if open_trade(result):

            opened += 1

            used_pairs.add(
                pair
            )

            # Pequeña pausa entre órdenes.
            time.sleep(0.5)


# ============================================================
# SUMMARY
# ============================================================

def send_scan_summary(
    results: List[Dict[str, Any]]
):

    if not results:
        return

    # No mandar mensajes constantemente.
    # Solo se utiliza si posteriormente
    # quieres activar un reporte periódico.

    top = results[:5]

    lines = []

    for item in top:

        lines.append(
            (
                f"{item['pair']} "
                f"{item['signal'].upper()} "
                f"{item['score']}/100"
            )
        )

    message = (
        "🔎 MEJORES OTC\n\n"
        + "\n".join(lines)
    )

    # Actualmente desactivado para
    # evitar spam en Telegram.

    # send(
    #     message,
    #     "scan",
    #     False
    # )


# ============================================================
# MAIN
# ============================================================

def main():

    global BOT_RUNNING

    # ========================================================
    # CONECTAR
    # ========================================================

    connect()

    # ========================================================
    # TELEGRAM
    # ========================================================

    threading.Thread(
        target=telegram_worker,
        daemon=True
    ).start()

    # ========================================================
    # PRIMERA ACTUALIZACIÓN
    # ========================================================

    refresh_pairs()

    # ========================================================
    # LISTO
    # ========================================================

    send(
        (
            "🤖 BOT LISTO\n\n"
            "Sistema: M1 + M5\n"
            f"Máximo operaciones: "
            f"{MAX_OPEN_TRADES}\n"
            f"Expiración: "
            f"{EXPIRATION} minuto\n"
            f"Score mínimo: "
            f"{MIN_SCORE_TO_TRADE}\n"
            f"OTC encontrados: "
            f"{len(AVAILABLE_PAIRS)}\n\n"
            "Usa /START para comenzar."
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
            # ACTUALIZAR OTC
            # ------------------------------------------------

            refresh_pairs()

            # ------------------------------------------------
            # RESULTADOS
            # ------------------------------------------------

            check_results()

            # ------------------------------------------------
            # BOT APAGADO
            # ------------------------------------------------

            if not BOT_RUNNING:

                time.sleep(1)

                continue

            # ------------------------------------------------
            # LÍMITE DE OPERACIONES
            # ------------------------------------------------

            if len(OPEN_TRADES) >= MAX_OPEN_TRADES:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # ------------------------------------------------
            # NO HAY OTC
            # ------------------------------------------------

            if not AVAILABLE_PAIRS:

                refresh_pairs()

                time.sleep(2)

                continue

            # ------------------------------------------------
            # ANALIZAR TODOS
            # ------------------------------------------------

            results = analyze_all()

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

            print(
                "🛑 Bot detenido manualmente."
            )

            break

        except Exception as e:

            print(
                f"Error principal: {e}"
            )

            time.sleep(2)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
