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
# 💰 GESTIÓN DINÁMICA
# ============================================================

BASE_AMOUNT = 10
CURRENT_AMOUNT = BASE_AMOUNT
MAX_AMOUNT = 200

WIN_STREAK = 0
LOSS_STREAK = 0

# ============================================================
# ESTADO GENERAL
# ============================================================

BOT_RUNNING = False
IQ: Optional[IQ_Option] = None
LAST_UPDATE_ID = None

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}

# ============================================================
# 🧠 IA SIMPLE QUE APRENDE
# ============================================================

AI_MEMORY = {
    "total": 0,
    "wins": 0,
    "losses": 0,
    "weights": {
        "score": 1.0,
        "continuity": 1.0,
        "trend": 1.0,
    }
}

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
# TELEGRAM SEND
# ============================================================

def telegram_send(msg: str, key: str = "msg", force: bool = False):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not force and not can_send(key):
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
# IA UPDATE
# ============================================================

def update_ai(profit: float):

    AI_MEMORY["total"] += 1

    if profit > 0:
        AI_MEMORY["wins"] += 1
        AI_MEMORY["weights"]["score"] *= 1.01
        AI_MEMORY["weights"]["continuity"] *= 1.02
        AI_MEMORY["weights"]["trend"] *= 1.01
    else:
        AI_MEMORY["losses"] += 1
        AI_MEMORY["weights"]["score"] *= 0.99
        AI_MEMORY["weights"]["continuity"] *= 0.98
        AI_MEMORY["weights"]["trend"] *= 0.99

# ============================================================
# MONTO DINÁMICO
# ============================================================

def update_amount(profit: float):

    global CURRENT_AMOUNT, WIN_STREAK, LOSS_STREAK

    if profit > 0:
        WIN_STREAK += 1
        LOSS_STREAK = 0

        CURRENT_AMOUNT = min(
            BASE_AMOUNT * (1 + WIN_STREAK * 0.5),
            MAX_AMOUNT
        )

    else:
        LOSS_STREAK += 1
        WIN_STREAK = 0
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
                text = str(msg.get("text", "")).lower()
                chat_id = str(msg.get("chat", {}).get("id"))

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/start"):
                    BOT_RUNNING = True
                    telegram_send("🟢 BOT ACTIVADO", "start", force=True)

                elif text.startswith("/stop"):
                    BOT_RUNNING = False
                    telegram_send("🔴 BOT DETENIDO", "stop", force=True)

                elif text.startswith("/status"):
                    telegram_send(
                        f"Estado: {'ACTIVO' if BOT_RUNNING else 'DETENIDO'}\n"
                        f"Monto: ${CURRENT_AMOUNT}\n"
                        f"WIN: {WIN_STREAK} | LOSS: {LOSS_STREAK}",
                        "status",
                        force=True
                    )

                elif text.startswith("/ai"):
                    winrate = (
                        AI_MEMORY["wins"] / AI_MEMORY["total"] * 100
                        if AI_MEMORY["total"] > 0 else 0
                    )

                    telegram_send(
                        "🧠 IA STATUS\n\n"
                        f"Total: {AI_MEMORY['total']}\n"
                        f"Wins: {AI_MEMORY['wins']}\n"
                        f"Losses: {AI_MEMORY['losses']}\n"
                        f"Winrate: {winrate:.2f}%\n\n"
                        f"Pesos: {AI_MEMORY['weights']}",
                        "ai",
                        force=True
                    )

        except:
            pass

        time.sleep(1)

# ============================================================
# CONEXIÓN IQ
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

    for oid, trade in list(OPEN_TRADES.items()):
        if now < trade["expiry"]:
            continue

        try:
            result = IQ.check_win_v4(oid)

            if result is None:
                continue

            profit = float(result)

            update_amount(profit)
            update_ai(profit)

            outcome = "WIN 🟢" if profit > 0 else "LOSS 🔴"

            telegram_send(
                f"📊 RESULTADO\n\n"
                f"{trade['pair']} {outcome}\n"
                f"💰 {profit}\n\n"
                f"WIN: {WIN_STREAK} | LOSS: {LOSS_STREAK}\n"
                f"💵 Monto: ${CURRENT_AMOUNT}",
                f"result_{oid}"
            )

            del OPEN_TRADES[oid]

        except:
            pass

# ============================================================
# MAIN LOOP
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

            result = analyze_market(df.iloc[-1], previous_m1=df)

            if result.get("valid"):

                signal = result["signal"]

                ok, oid = IQ.buy(
                    CURRENT_AMOUNT,
                    pair,
                    signal,
                    EXPIRATION
                )

                if ok:
                    OPEN_TRADES[oid] = {
                        "pair": pair,
                        "expiry": int(time.time()) + 60
                    }

                    telegram_send(
                        f"🚀 TRADE {pair} {signal.upper()}\n💵 ${CURRENT_AMOUNT}",
                        f"trade_{pair}"
                    )

            time.sleep(1)

        except Exception as e:
            logging.error(e)
            time.sleep(1)

# ============================================================

if __name__ == "__main__":
    main()
