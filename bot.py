"""
bot.py
Bot de análisis y ejecución para IQ Option.

SEGURIDAD:
- Inicia en PRACTICE.
- DRY_RUN=True no ejecuta operaciones.
- Cambia DRY_RUN=False únicamente después de probar ampliamente en DEMO.
- La API comunitaria puede cambiar y no es oficial.
"""

from __future__ import annotations
import os
import time
from datetime import datetime
from typing import List

from strategy import analyze_rejection

try:
    from iqoptionapi.stable_api import IQ_Option
except ImportError:
    IQ_Option = None


EMAIL = os.getenv("IQ_EMAIL", "")
PASSWORD = os.getenv("IQ_PASSWORD", "")
ACCOUNT_TYPE = "PRACTICE"
DRY_RUN = True

TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 120
EXPIRATION_MINUTES = 1
STAKE = 1.0
SCAN_INTERVAL_SECONDS = 5
MIN_SCORE = 80

# Puedes reemplazar esta lista por tus 50 pares.
ASSETS: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "EURGBP", "EURJPY", "GBPJPY", "AUDJPY",
]


def connect():
    if IQ_Option is None:
        raise RuntimeError(
            "Instala la API: pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git"
        )
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD como variables de entorno.")

    api = IQ_Option(EMAIL, PASSWORD)
    ok, reason = api.connect()
    if not ok:
        raise RuntimeError(f"No se pudo conectar: {reason}")

    api.change_balance(ACCOUNT_TYPE)
    return api


def get_closed_candles(api, asset: str):
    candles = api.get_candles(
        asset,
        TIMEFRAME_SECONDS,
        CANDLE_COUNT + 1,
        time.time()
    )
    if not candles:
        return []

    # La última vela devuelta puede estar en formación.
    # Usamos la vela anterior como última vela cerrada.
    candles = sorted(candles, key=lambda x: x.get("from", 0))
    return candles[:-1]


def execute_signal(api, asset: str, action: str):
    if DRY_RUN:
        print(f"[DRY_RUN] {asset} -> {action.upper()} | expiración={EXPIRATION_MINUTES}m")
        return False, None

    # No se ejecuta por defecto. Revisa primero la API y realiza pruebas en DEMO.
    return api.buy(STAKE, asset, action, EXPIRATION_MINUTES)


def scan_once(api):
    candidates = []

    for asset in ASSETS:
        try:
            candles = get_closed_candles(api, asset)
            signal = analyze_rejection(candles, min_score=MIN_SCORE)

            if signal.action != "none":
                candidates.append((signal.score, asset, signal))

        except Exception as exc:
            print(f"[{asset}] error: {exc}")

    # Solo se selecciona la señal con mayor puntuación; no se opera todo a la vez.
    if not candidates:
        print("Sin rechazos válidos.")
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, asset, signal = candidates[0]

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"{asset} {signal.action.upper()} score={score} reason={signal.reason}"
    )
    execute_signal(api, asset, signal.action)


def main():
    api = connect()
    print("Conectado a IQ Option en modo:", ACCOUNT_TYPE)
    print("DRY_RUN =", DRY_RUN)

    while True:
        try:
            scan_once(api)
            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("Bot detenido por el usuario.")
            break
        except Exception as exc:
            print("Error principal:", exc)
            time.sleep(10)


if __name__ == "__main__":
    main()
