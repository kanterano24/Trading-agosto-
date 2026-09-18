"""
bot.py
Bot IQ Option + Telegram.

Funciones:
- Analiza pares buscando rechazos de soporte/resistencia mediante strategy.py.
- Envía señales CALL/PUT a Telegram.
- Comandos de Telegram: /start, /stop, /status, /scan.
- Por seguridad, DRY_RUN=True y cuenta PRACTICE por defecto.

Variables de entorno necesarias:
    IQ_EMAIL
    IQ_PASSWORD
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Instalación:
    pip install requests
    pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
"""

from __future__ import annotations

import os
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from strategy import analyze_rejection


# =========================
# CONFIGURACIÓN
# =========================

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ACCOUNT_TYPE = os.getenv("IQ_ACCOUNT_TYPE", "PRACTICE")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 120
EXPIRATION_MINUTES = 1
STAKE = 1.0
MIN_SCORE = 80

SCAN_INTERVAL_SECONDS = 5
TELEGRAM_POLL_INTERVAL = 2

# Añade aquí tus 50 pares si deseas ampliar el escaneo.
ASSETS: List[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
]


# =========================
# ESTADO DEL BOT
# =========================

bot_running = True
last_signal_key: Optional[str] = None
last_scan_time: Optional[str] = None
signals_sent = 0
lock = threading.Lock()


# =========================
# TELEGRAM
# =========================

def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram(message: str, chat_id: Optional[str] = None) -> bool:
    """Envía un mensaje a Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        print("[TELEGRAM] Falta TELEGRAM_BOT_TOKEN.")
        return False

    destination = chat_id or TELEGRAM_CHAT_ID
    if not destination:
        print("[TELEGRAM] Falta TELEGRAM_CHAT_ID.")
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            data={
                "chat_id": destination,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return bool(data.get("ok"))
    except requests.RequestException as exc:
        print(f"[TELEGRAM] Error enviando mensaje: {exc}")
        return False


def get_telegram_updates(offset: Optional[int] = None) -> List[Dict[str, Any]]:
    """Obtiene mensajes nuevos mediante long polling."""
    if not TELEGRAM_BOT_TOKEN:
        return []

    params = {
        "timeout": 10,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(
            telegram_url("getUpdates"),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("result", []) if data.get("ok") else []
    except requests.RequestException as exc:
        print(f"[TELEGRAM] Error leyendo comandos: {exc}")
        return []


def authorized_chat(chat_id: str) -> bool:
    """Solo acepta comandos del chat configurado."""
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


def telegram_command_loop():
    """Procesa /start, /stop, /status y /scan."""
    global bot_running

    offset = None

    while True:
        updates = get_telegram_updates(offset)

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", ""))
            text = (message.get("text") or "").strip().lower()

            if not authorized_chat(chat_id):
                continue

            if text.startswith("/start"):
                with lock:
                    bot_running = True
                send_telegram("✅ Bot activado. El análisis continuará en modo DEMO/DRY_RUN.")

            elif text.startswith("/stop"):
                with lock:
                    bot_running = False
                send_telegram("🛑 Bot detenido. No se analizarán nuevas señales hasta usar /start.")

            elif text.startswith("/status"):
                with lock:
                    current_state = "ACTIVO" if bot_running else "DETENIDO"
                    current_last_scan = last_scan_time or "sin escaneos"
                send_telegram(
                    "📊 <b>Estado del bot</b>\n"
                    f"Estado: <b>{current_state}</b>\n"
                    f"Cuenta: <b>{ACCOUNT_TYPE}</b>\n"
                    f"DRY_RUN: <b>{DRY_RUN}</b>\n"
                    f"Último escaneo: <b>{current_last_scan}</b>\n"
                    f"Señales enviadas: <b>{signals_sent}</b>"
                )

            elif text.startswith("/scan"):
                send_telegram("🔎 Se realizará un escaneo en el siguiente ciclo.")

            elif text.startswith("/help"):
                send_telegram(
                    "<b>Comandos disponibles</b>\n"
                    "/start - Activar análisis\n"
                    "/stop - Detener análisis\n"
                    "/status - Ver estado\n"
                    "/scan - Solicitar escaneo\n"
                    "/help - Mostrar ayuda"
                )

        time.sleep(TELEGRAM_POLL_INTERVAL)


# =========================
# IQ OPTION
# =========================

def connect_iqoption():
    try:
        from iqoptionapi.stable_api import IQ_Option
    except ImportError as exc:
        raise RuntimeError(
            "Falta iqoptionapi. Instala la dependencia indicada en el encabezado."
        ) from exc

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD.")

    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = api.connect()

    if not connected:
        raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")

    api.change_balance(ACCOUNT_TYPE)
    return api


def get_closed_candles(api, asset: str):
    candles = api.get_candles(
        asset,
        TIMEFRAME_SECONDS,
        CANDLE_COUNT + 1,
        time.time(),
    )

    if not candles:
        return []

    candles = sorted(candles, key=lambda item: item.get("from", 0))

    # Excluye la vela que todavía podría estar formándose.
    return candles[:-1]


def execute_signal(api, asset: str, action: str):
    """
    Por defecto no ejecuta operaciones.
    DRY_RUN debe permanecer True durante las pruebas.
    """
    if DRY_RUN:
        print(
            f"[DRY_RUN] {asset} -> {action.upper()} "
            f"expiración={EXPIRATION_MINUTES}m monto={STAKE}"
        )
        return False, None

    return api.buy(STAKE, asset, action, EXPIRATION_MINUTES)


def scan_once(api):
    global last_signal_key, last_scan_time, signals_sent

    with lock:
        if not bot_running:
            return

    candidates = []

    for asset in ASSETS:
        try:
            candles = get_closed_candles(api, asset)
            signal = analyze_rejection(candles, min_score=MIN_SCORE)

            if signal.action != "none":
                candidates.append((signal.score, asset, signal))

        except Exception as exc:
            print(f"[{asset}] Error de análisis: {exc}")

    with lock:
        last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not candidates:
        print("Sin rechazos válidos.")
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, asset, signal = candidates[0]

    candle_key = (
        f"{asset}:{signal.action}:"
        f"{signal.support}:{signal.resistance}:"
        f"{last_scan_time}"
    )

    # Evita repetir exactamente la misma señal dentro del ciclo.
    if candle_key == last_signal_key:
        return

    last_signal_key = candle_key

    direction = "CALL 🟢" if signal.action == "call" else "PUT 🔴"
    level_name = "soporte" if signal.action == "call" else "resistencia"
    level_value = signal.support if signal.action == "call" else signal.resistance

    message = (
        "🚨 <b>SEÑAL DE RECHAZO</b>\n\n"
        f"📌 Par: <b>{asset}</b>\n"
        f"📈 Dirección: <b>{direction}</b>\n"
        f"🎯 Score: <b>{score}/100</b>\n"
        f"📍 Zona: <b>{level_name}</b>\n"
        f"💵 Nivel: <b>{level_value}</b>\n"
        f"⏱️ Expiración: <b>{EXPIRATION_MINUTES} minuto(s)</b>\n"
        f"🧪 Cuenta: <b>{ACCOUNT_TYPE}</b>\n"
        f"🔒 DRY_RUN: <b>{DRY_RUN}</b>\n"
        f"📝 Motivo: <b>{signal.reason}</b>\n\n"
        "⚠️ Señal experimental. Verifica el gráfico antes de operar."
    )

    if send_telegram(message):
        with lock:
            signals_sent += 1

    execute_signal(api, asset, signal.action)


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID antes de iniciar."
        )

    send_telegram(
        "🤖 <b>Bot IQ Option iniciado</b>\n"
        f"Cuenta: <b>{ACCOUNT_TYPE}</b>\n"
        f"DRY_RUN: <b>{DRY_RUN}</b>\n"
        "Usa /help para ver los comandos."
    )

    telegram_thread = threading.Thread(
        target=telegram_command_loop,
        daemon=True,
    )
    telegram_thread.start()

    api = connect_iqoption()
    print(f"Conectado a IQ Option. Cuenta: {ACCOUNT_TYPE}. DRY_RUN={DRY_RUN}")

    while True:
        try:
            with lock:
                active = bot_running

            if active:
                scan_once(api)

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            send_telegram("🛑 Bot detenido manualmente.")
            break

        except Exception as exc:
            print(f"[MAIN] Error: {exc}")
            send_telegram(f"⚠️ Error del bot: {exc}")
            time.sleep(10)


if __name__ == "__main__":
    main()
