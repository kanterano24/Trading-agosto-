from __future__ import annotations

import os
import time
import threading
import requests
import pandas as pd
from typing import Dict, List, Optional, Any

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
# 💰 MONEY MANAGEMENT
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
REFRESH_INTERVAL = 900  # 15 min

OPEN_TRADES: Dict[int, Dict[str, Any]] = {}

# ============================================================
# TELEGRAM
# ============================================================

def send(msg, key="msg", force=False):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=3
        )
    except:
        pass


# ============================================================
# OTC LIST
# ============================================================

def get_otc_pairs():
    try:
        data = IQ.get_all_init_v2()
        pairs = set()

        for t in ["binary", "turbo"]:
            for a in data.get(t, {}).get("actives", {}).values():
                name = a.get("name", "")
                if "-OTC" in name:
                    pairs.add(name)

        return list(pairs)
    except:
        return []


def refresh_pairs():
    global AVAILABLE_PAIRS, LAST_REFRESH

    if time.time() - LAST_REFRESH > REFRESH_INTERVAL:
        AVAILABLE_PAIRS = get_otc_pairs()
        LAST_REFRESH = time.time()

        send(f"🔄 OTC actualizados: {len(AVAILABLE_PAIRS)}", "refresh", True)


# ============================================================
# TELEGRAM CONTROL
# ============================================================

LAST_UPDATE = None

def telegram_worker():
    global BOT_RUNNING, LAST_UPDATE

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": LAST_UPDATE + 1 if LAST_UPDATE else None},
                timeout=3
            ).json()

            for u in r.get("result", []):
                LAST_UPDATE = u["update_id"]

                msg = u.get("message", {})
                text = msg.get("text", "").lower()
                chat = str(msg.get("chat", {}).get("id"))

                if chat != str(TELEGRAM_CHAT_ID):
                    continue

                if text == "/start":
                    BOT_RUNNING = True
                    send("🟢 BOT ON", "start", True)

                elif text == "/stop":
                    BOT_RUNNING = False
                    send("🔴 BOT OFF", "stop", True)

                elif text == "/status":
                    send(
                        f"Estado: {BOT_RUNNING}\nOTC: {len(AVAILABLE_PAIRS)}\nWIN:{WIN} LOSS:{LOSS}",
                        "status",
                        True
                    )

        except:
            pass

        time.sleep(1)


# ============================================================
# IQ
# ============================================================

def connect():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    IQ.connect()
    send("🟢 Conectado IQ", "connect", True)


# ============================================================
# RESULTS
# ============================================================

def check_results():
    global WIN, LOSS, CURRENT_AMOUNT

    for oid, t in list(OPEN_TRADES.items()):
        try:
            if time.time() < t["expiry"]:
                continue

            res = IQ.check_win_v4(oid)

            if res is None:
                continue

            profit = float(res)

            if profit > 0:
                WIN += 1
                CURRENT_AMOUNT = min(BASE_AMOUNT * (1 + WIN * 0.5), MAX_AMOUNT)
            else:
                LOSS += 1
                CURRENT_AMOUNT = BASE_AMOUNT

            send(
                f"📊 RESULT\n{t['pair']}\n{'WIN' if profit>0 else 'LOSS'}\n{profit}",
                f"r_{oid}",
                True
            )

            del OPEN_TRADES[oid]

        except:
            pass


# ============================================================
# ANALYZE ALL OTC
# ============================================================

def select_best(results):
    if not results:
        return None
    return sorted(results, key=lambda x: x["score"], reverse=True)[0]


def analyze_all():
    results = []

    for pair in AVAILABLE_PAIRS:
        try:
            candles = IQ.get_candles(pair, TIMEFRAME, 60, time.time())
            df = pd.DataFrame(candles)

            if len(df) < 10:
                continue

            r = analyze_market(df.iloc[-1], previous_m1=df)

            if r.get("valid"):
                r["pair"] = pair
                results.append(r)

        except:
            continue

    return results


# ============================================================
# MAIN
# ============================================================

def main():
    connect()

    threading.Thread(target=telegram_worker, daemon=True).start()

    send("🤖 BOT LISTO", "ready", True)

    while True:
        try:

            if not BOT_RUNNING:
                time.sleep(1)
                continue

            refresh_pairs()
            check_results()

            results = analyze_all()
            best = select_best(results)

            if not best:
                time.sleep(1)
                continue

            pair = best["pair"]
            signal = best["signal"]

            ok, oid = IQ.buy(CURRENT_AMOUNT, pair, signal, EXPIRATION)

            if ok:
                OPEN_TRADES[oid] = {
                    "pair": pair,
                    "expiry": time.time() + 60
                }

                send(
                    f"🚀 TRADE\n{pair}\n{signal.upper()}\n${CURRENT_AMOUNT}\nScore:{best['score']}",
                    f"t_{pair}",
                    True
                )

            time.sleep(1)

        except Exception as e:
            print(e)
            time.sleep(1)


if __name__ == "__main__":
    main()
