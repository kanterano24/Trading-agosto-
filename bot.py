from __future__ import annotations

import os
import time
import threading
import requests
import pandas as pd
from collections import defaultdict

from iqoptionapi.stable_api import IQ_Option
from strategy import analyze_market

# =========================
# CONFIG
# =========================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = 1

BOT_RUNNING = False
IQ = None

# =========================
# STREAM SYSTEM
# =========================

STREAM_CACHE = defaultdict(pd.DataFrame)
STREAM_STARTED = set()
LOCK = threading.Lock()

# =========================
# TELEGRAM
# =========================

def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=3
        )
    except:
        pass

# =========================
# CONNECT
# =========================

def connect():
    global IQ
    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    IQ.connect()
    send("🟢 CONECTADO")

# =========================
# PAIRS FIX (5 ONLY)
# =========================

def get_pairs():
    data = IQ.get_all_init_v2()
    pairs = []

    for t in ["binary", "turbo"]:
        for a in data.get(t, {}).get("actives", {}).values():
            name = a.get("name", "")
            if "-OTC" in name:
                pairs.append(name)

    pairs = sorted(pairs)[:5]   # 🔥 SOLO 5 PARES
    return pairs

# =========================
# STREAMS ULTRA RÁPIDOS
# =========================

def start_stream(pair):
    if pair in STREAM_STARTED:
        return

    IQ.start_candles_stream(pair, TIMEFRAME, 60)
    STREAM_STARTED.add(pair)


def stream_worker(pair):
    while True:
        try:
            candles = IQ.get_realtime_candles(pair, TIMEFRAME)

            if candles:
                df = pd.DataFrame([
                    {
                        "from": int(t),
                        "open": c["open"],
                        "close": c["close"],
                        "high": c.get("max", c.get("high")),
                        "low": c.get("min", c.get("low")),
                    }
                    for t, c in candles.items()
                ])

                df.sort_values("from", inplace=True)
                df = df.tail(60)

                with LOCK:
                    STREAM_CACHE[pair] = df

        except:
            pass

        time.sleep(0.2)

# =========================
# START ALL STREAMS
# =========================

def start_all(pairs):
    for p in pairs:
        start_stream(p)

        t = threading.Thread(
            target=stream_worker,
            args=(p,),
            daemon=True
        )
        t.start()

# =========================
# ANALYSIS
# =========================

def analyze_all():
    results = []

    for pair in list(STREAM_CACHE.keys()):
        with LOCK:
            df = STREAM_CACHE.get(pair)

        if df is None or len(df) < 10:
            continue

        r = analyze_market(df.iloc[-1], previous_m1=df, pair=pair)

        if r.get("valid"):
            r["pair"] = pair
            results.append(r)

    return results


def best(results):
    return max(results, key=lambda x: x["score"]) if results else None

# =========================
# TELEGRAM CONTROL
# =========================

def telegram_worker():
    global BOT_RUNNING

    last = None

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": last + 1 if last else None},
                timeout=3
            ).json()

            for u in r.get("result", []):
                last = u["update_id"]
                text = u["message"]["text"].lower()

                if text == "/start":
                    BOT_RUNNING = True
                    send("🟢 BOT ON")

                elif text == "/stop":
                    BOT_RUNNING = False
                    send("🔴 BOT OFF")

        except:
            pass

        time.sleep(1)

# =========================
# MAIN
# =========================

def main():
    connect()

    pairs = get_pairs()   # 🔥 SOLO 5
    start_all(pairs)

    threading.Thread(target=telegram_worker, daemon=True).start()

    send(f"🤖 SNIPER READY | {len(pairs)} PAIRS")

    while True:
        try:

            if not BOT_RUNNING:
                time.sleep(1)
                continue

            results = analyze_all()
            b = best(results)

            if b:
                IQ.buy(10, b["pair"], b["signal"], EXPIRATION)
                send(f"🚀 {b['pair']} {b['signal']} | {b['score']}")

            time.sleep(0.3)

        except:
            time.sleep(1)


if __name__ == "__main__":
    main()
