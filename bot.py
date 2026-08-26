from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import pandas as pd
import requests

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_market

# ============================================================
# CONFIG
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = 1

# ============================================================
# 💰 GESTIÓN DE MONTO DINÁMICO
# ============================================================

BASE_AMOUNT = 556
CURRENT_AMOUNT = BASE_AMOUNT
MAX_AMOUNT = 200

WIN_STREAK = 0
LOSS_STREAK = 0

MODE = "compound"  # recomendado

# ============================================================
# ESTADO
# ============================================================

BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
LAST_UPDATE_ID = None

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}

# ============================================================
# 🚫 ANTI-SPAM
# ============================================================

LAST_MSG = {}
COOLDOWN = 10

def can_send(key):
    now = time.time()
    if key in LAST_MSG and now - LAST_MSG[key] < COOLDOWN:
        return False
    LAST_MSG[key] = now
    return True

# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(msg, key="msg"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not can_send(key):
        return False

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=3,
        )
        return True
    except:
        return False

# ============================================================
# 📊 ACTUALIZAR MONTO
# ============================================================

def update_amount(profit):
    global CURRENT_AMOUNT, WIN_STREAK, LOSS_STREAK

    if profit > 0:
        WIN_STREAK += 1
        LOSS_STREAK = 0

        CURRENT_AMOUNT = min(
            BASE_AMOUNT * (1 + WIN_STREAK * 0.5),
            MAX_AMOUNT
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
    global LAST_UPDATE_ID, BOT_RUNNING

    while True:
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": LAST_UPDATE_ID + 1 if LAST_UPDATE_ID else None},
                timeout=3,
            ).json()

            for u in res.get("result", []):
                LAST_UPDATE_ID = u["update_id"]

                msg = u.get("message", {})
                text = msg.get("text", "")

                if not isinstance(text, str):
                    continue

                text = text.lower().strip()
                chat_id = str(msg.get("chat", {}).get("id"))

                if str(chat_id) != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/start"):
                    BOT_RUNNING = True
                    telegram_send("🟢 BOT ACTIVADO", "start")

                elif text.startswith("/stop"):
                    BOT_RUNNING = False
                    telegram_send("🔴 BOT DETENIDO", "stop")

                elif text.startswith("/status"):
                    telegram_send(
                        f"Estado: {'ACTIVO' if BOT_RUNNING else 'DETENIDO'}\n"
                        f"Monto actual: ${CURRENT_AMOUNT}",
                        "status"
                    )

        except:
            pass

        time.sleep(1)

# ============================================================
# CONEXIÓN
# ============================================================

def connect():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    IQ.connect()
    telegram_send("🟢 Conectado a IQ Option", "connect")

# ============================================================
# RESULTADOS
# ============================================================

def check_results():
    now = int(time.time())

    for oid, t in list(OPEN_TRADES.items()):
        if now < t["expiry"]:
            continue

        try:
            result = IQ.check_win_v4(oid)

            if result is None:
                continue

            profit = float(result)
            update_amount(profit)

            outcome = "WIN 🟢" if profit > 0 else "LOSS 🔴"

            telegram_send(
                f"📊 RESULTADO\n\n"
                f"{t['pair']} {outcome}\n"
                f"💰 {profit}\n\n"
                f"📈 WIN: {WIN_STREAK}\n"
                f"📉 LOSS: {LOSS_STREAK}\n"
                f"💵 Próximo: ${CURRENT_AMOUNT}",
                f"res_{oid}"
            )

            del OPEN_TRADES[oid]

        except:
            pass

# ============================================================
# MAIN
# ============================================================

def main():
    connect()

    threading.Thread(
        target=telegram_worker,
        daemon=True
    ).start()

    telegram_send("🤖 BOT LISTO", "ready")

    while True:
        try:
            if not BOT_RUNNING:
                time.sleep(1)
                continue

            check_results()

            pair = "EURUSD-OTC"

            candles = IQ.get_candles(pair, 60, 60, time.time())
            df = pd.DataFrame(candles)

            result = analyze_market(
                df.iloc[-1],
                previous_m1=df
            )

            if result.get("valid"):
                signal = result["signal"]

                ok, oid = IQ.buy(
                    CURRENT_AMOUNT,
                    pair,
                    signal,
                    EXPIRATION
                )

                if ok:
                    telegram_send(
                        f"🚀 TRADE {pair} {signal.upper()}\n💵 ${CURRENT_AMOUNT}",
                        f"trade_{pair}"
                    )

                    OPEN_TRADES[oid] = {
                        "pair": pair,
                        "expiry": int(time.time()) + 60
                    }

            time.sleep(1)

        except Exception as e:
            logging.error(e)
            time.sleep(1)

# ============================================================

if __name__ == "__main__":
    main()
