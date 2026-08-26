from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_live_candle, analyze_market

# ============================================================
# CONFIG
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
CANDLE_COUNT = 62

AMOUNT = 552
EXPIRATION = 1

POLL_INTERVAL = 0.05
MAX_ENTRY_DELAY = 5

MIN_MARKET_SCORE = 82

# ============================================================
# 🔕 FILTRO ANTI-SPAM
# ============================================================

LAST_TELEGRAM_MESSAGE = ""
LAST_TELEGRAM_TIME = 0.0
TELEGRAM_MIN_INTERVAL = 10


def is_important_message(message: str) -> bool:
    keywords = [
        "OPERACIÓN ABIERTA",
        "RESULTADO",
        "MEJOR MERCADO",
        "N+1",
    ]
    return any(k in message for k in keywords)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message: str) -> bool:
    global LAST_TELEGRAM_MESSAGE, LAST_TELEGRAM_TIME

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not is_important_message(message):
        return False

    now = time.time()

    if message == LAST_TELEGRAM_MESSAGE:
        return False

    if now - LAST_TELEGRAM_TIME < TELEGRAM_MIN_INTERVAL:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=3,
        )

        if response.status_code != 200:
            logging.warning("Telegram error: %s", response.text)
            return False

        LAST_TELEGRAM_MESSAGE = message
        LAST_TELEGRAM_TIME = now
        return True

    except Exception as e:
        logging.warning("Telegram fallo: %s", e)
        return False


# ============================================================
# RESULTADOS
# ============================================================

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}

def check_trade_results():
    if IQ is None:
        return

    now = int(time.time())

    for order_id, trade in list(OPEN_TRADES.items()):
        if trade["checked"]:
            continue

        if now < trade["expiry"]:
            continue

        try:
            result = IQ.check_win_v4(order_id)

            if result is None:
                continue

            trade["checked"] = True

            profit = float(result)

            outcome = "WIN 🟢" if profit > 0 else "LOSS 🔴" if profit < 0 else "EMPATE ⚪"

            telegram_send(
                f"📊 RESULTADO\n\n"
                f"Par: {trade['pair']}\n"
                f"Dirección: {trade['signal'].upper()}\n"
                f"Resultado: {outcome}\n"
                f"Ganancia: {profit}"
            )

        except Exception as e:
            logging.warning("Error resultado: %s", e)


# ============================================================
# IQ
# ============================================================

IQ: Optional[IQ_Option] = None

def connect_iq():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    check, reason = IQ.connect()
    if not check:
        raise Exception(reason)


# ============================================================
# STREAM
# ============================================================

def get_candles(pair):
    candles = IQ.get_candles(pair, 60, 100, time.time())
    return pd.DataFrame(candles)


# ============================================================
# TRADING
# ============================================================

LAST_TRADE = 0

def trade(pair, signal):
    global LAST_TRADE

    ok, order_id = IQ.buy(AMOUNT, pair, signal, EXPIRATION)

    if ok:
        telegram_send(
            f"✅ OPERACIÓN ABIERTA\n\n"
            f"{pair}\n{signal.upper()}"
        )

        OPEN_TRADES[order_id] = {
            "pair": pair,
            "signal": signal,
            "expiry": int(time.time()) + (EXPIRATION * 60),
            "checked": False
        }

        LAST_TRADE = time.time()


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    connect_iq()

    pair = "EURUSD-OTC"

    while True:
        try:
            check_trade_results()

            df = get_candles(pair)

            result = analyze_market(
                df.iloc[-1],
                previous_m1=df
            )

            if result["valid"] and time.time() - LAST_TRADE > 240:
                telegram_send(
                    f"🏆 MEJOR MERCADO\n\n{pair}\n{result['signal'].upper()}"
                )

                trade(pair, result["signal"])

            time.sleep(1)

        except Exception as e:
            logging.error(e)
            time.sleep(2)


if __name__ == "__main__":
    main()
