from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

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

TIMEFRAME = 60
EXPIRATION = 1

AMOUNT = float(os.getenv("AMOUNT", "120"))
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "60"))

# 🔥 SOLO 3 PARES
MAX_OTC_PAIRS = 3

PAIR_REFRESH_SECONDS = 60.0
TRADE_COOLDOWN = 60.0

# ============================================================
# ESTADO
# ============================================================

BOT_RUNNING = False
IQ: Optional[IQ_Option] = None

PAIRS: list[str] = []
LAST_PAIR_REFRESH = 0.0

LIVE_STATE: Dict[str, Dict[str, Any]] = {}
PENDING_ENTRY: Dict[str, Dict[str, Any]] = {}

LAST_TRADE_TIME: Dict[str, float] = {}
LAST_TRADE_CANDLE: Dict[str, int] = {}

STATE_LOCK = threading.RLock()

# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    def worker():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=3,
            )
        except:
            pass

    threading.Thread(target=worker, daemon=True).start()

# ============================================================
# CONEXIÓN
# ============================================================

def connect_iq() -> bool:
    global IQ

    IQ = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = IQ.connect()

    if not connected:
        raise Exception(f"No conecta: {reason}")

    refresh_pairs(force=True)
    return True

def ensure_connection():
    if IQ is None:
        return connect_iq()
    if not IQ.check_connect():
        return connect_iq()
    return True

# ============================================================
# PARES
# ============================================================

def refresh_pairs(force=False):
    global PAIRS, LAST_PAIR_REFRESH

    now = time.time()

    if not force and now - LAST_PAIR_REFRESH < PAIR_REFRESH_SECONDS:
        return PAIRS

    data = IQ.get_all_init_v2()
    binary = data.get("binary", {}).get("actives", {})

    pairs = []

    for _, info in binary.items():
        name = info.get("name", "")
        if "OTC" in name and info.get("enabled", True):
            pair = name.split(".")[-1]
            pairs.append(pair)

    # 🔥 SOLO 3
    PAIRS = sorted(set(pairs))[:MAX_OTC_PAIRS]

    LAST_PAIR_REFRESH = now
    return PAIRS

# ============================================================
# DATA
# ============================================================

def get_candles(pair):
    candles = IQ.get_candles(pair, TIMEFRAME, CANDLE_COUNT, time.time())

    df = pd.DataFrame(candles)
    df.rename(columns={"max": "high", "min": "low"}, inplace=True)

    return df

# ============================================================
# ANÁLISIS
# ============================================================

def analyze_pair(pair, closed_ts):

    df = get_candles(pair)

    df = df[df["from"] <= closed_ts].copy()
    df.sort_values("from", inplace=True)

    if len(df) < 25:
        return

    last_candle = df.iloc[-1].to_dict()
    history = df.iloc[:-1].copy()

    result = analyze_market(last_candle, history, pair)

    signal = result.get("signal")

    if signal not in ("call", "put"):
        return

    execution_ts = closed_ts + TIMEFRAME

    PENDING_ENTRY[pair] = {
        "signal": signal,
        "execution_ts": execution_ts,
    }

    telegram_send(f"🎯 {pair} {signal.upper()} preparada")

# ============================================================
# EJECUCIÓN
# ============================================================

def execute(pair):

    pending = PENDING_ENTRY.get(pair)
    if not pending:
        return

    now = int(time.time())
    current_ts = now - (now % TIMEFRAME)

    if current_ts != pending["execution_ts"]:
        return

    signal = pending["signal"]

    ok, _ = IQ.buy(AMOUNT, pair, signal, EXPIRATION)

    if ok:
        telegram_send(f"✅ {pair} {signal.upper()} ejecutado")

    PENDING_ENTRY.pop(pair, None)

# ============================================================
# LOOP PRINCIPAL (🔥 SOLO POR CIERRE)
# ============================================================

def main():

    global BOT_RUNNING

    connect_iq()

    BOT_RUNNING = True
    last_ts = None

    while True:

        if not ensure_connection():
            time.sleep(1)
            continue

        now = int(time.time())
        current_ts = now - (now % TIMEFRAME)

        # 🔥 SOLO CUANDO CIERRA VELA
        if current_ts != last_ts:

            last_ts = current_ts
            closed_ts = current_ts - TIMEFRAME

            refresh_pairs()

            for pair in PAIRS:
                analyze_pair(pair, closed_ts)

        # ejecución sniper
        for pair in list(PAIRS):
            execute(pair)

        time.sleep(0.1)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
