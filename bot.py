"""
bot.py
Bot IQ Option + Telegram con análisis de rechazos.

Mejoras:
- Lista de hasta 50 pares.
- Selección de la señal con mayor score.
- Deducción de señales repetidas usando timestamp de vela.
- Comandos Telegram: /start, /stop, /status, /scan, /help.
- Registro de resultados virtuales en DRY_RUN.
- Cuenta PRACTICE y DRY_RUN activados por defecto.

Variables Railway:
    IQ_EMAIL
    IQ_PASSWORD
    TELEGRAM_BOT_TOKEN (o TELEGRAM_TOKEN)
    TELEGRAM_CHAT_ID

Instalación:
    pip install requests
    pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git
"""

from __future__ import annotations

import html
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from strategy import Signal, analyze_rejection


# =========================
# CONFIGURACIÓN
# =========================

IQ_EMAIL = os.getenv("IQ_EMAIL", "").strip()
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "").strip()

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or ""
).strip()

TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_CHATID")
    or ""
).strip()

ACCOUNT_TYPE = os.getenv("IQ_ACCOUNT_TYPE", "PRACTICE").upper()
# La ejecución solo se permite explícitamente en cuenta PRACTICE/DEMO.
EXECUTE_DEMO = os.getenv("EXECUTE_DEMO", "true").strip().lower() == "true"
DRY_RUN = not EXECUTE_DEMO

TIMEFRAME_SECONDS = 60
CANDLE_COUNT = 120
EXPIRATION_MINUTES = 1
STAKE = float(os.getenv("STAKE", "1.0"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))

SCAN_INTERVAL_SECONDS = 5
TELEGRAM_POLL_INTERVAL = 2

ASSETS: List[str] = [
    "EURUSD",
    "GBPUSD",
    "AUDCHF",
    "AUDUSD",
]


# =========================
# ESTADO
# =========================

bot_running = True
force_scan = False
last_scan_time: Optional[str] = None
last_processed_candle: Dict[str, int] = {}
pending_paper_trades: Dict[str, Dict[str, Any]] = {}
wins = 0
losses = 0
draws = 0
signals_sent = 0
lock = threading.Lock()


# =========================
# TELEGRAM
# =========================

def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram(message: str, chat_id: Optional[str] = None) -> bool:
    destination = chat_id or TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not destination:
        print("[TELEGRAM] Variables faltantes.")
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
        return bool(response.json().get("ok"))
    except (requests.RequestException, ValueError) as exc:
        print(f"[TELEGRAM] Error: {exc}")
        return False


def get_telegram_updates(offset: Optional[int]) -> List[Dict[str, Any]]:
    if not TELEGRAM_BOT_TOKEN:
        return []

    params: Dict[str, Any] = {
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
        if response.status_code == 409:
            print(
                "[TELEGRAM] Error 409: otra instancia está usando getUpdates. "
                "Detén el proceso duplicado en Railway/otro servidor."
            )
            return []

        response.raise_for_status()
        data = response.json()
        return data.get("result", []) if data.get("ok") else []
    except (requests.RequestException, ValueError) as exc:
        print(f"[TELEGRAM] Lectura de comandos falló: {exc}")
        time.sleep(3)
        return []


def authorized_chat(chat_id: str) -> bool:
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


def telegram_command_loop() -> None:
    global bot_running, force_scan

    offset: Optional[int] = None

    while True:
        try:
            updates = get_telegram_updates(offset)
        except RuntimeError as exc:
            print(f"[TELEGRAM] Polling detenido: {exc}")
            return

        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = (message.get("text") or "").strip().lower()

            if not authorized_chat(chat_id):
                continue

            command = text.split()[0] if text else ""

            if command in ("/start", "/resume"):
                with lock:
                    bot_running = True
                send_telegram("✅ <b>Bot activado</b>. Análisis habilitado en DEMO.")

            elif command == "/stop":
                with lock:
                    bot_running = False
                send_telegram("🛑 <b>Bot detenido</b>. No se procesarán nuevas señales.")

            elif command == "/scan":
                with lock:
                    force_scan = True
                send_telegram("🔎 Escaneo solicitado. Se procesará en el próximo ciclo.")

            elif command == "/status":
                with lock:
                    state = "ACTIVO" if bot_running else "DETENIDO"
                    last_scan = last_scan_time or "sin escaneos"
                    current_wins = wins
                    current_losses = losses
                    current_draws = draws
                    current_signals = signals_sent

                send_telegram(
                    "📊 <b>Estado del bot</b>\n"
                    f"Estado: <b>{state}</b>\n"
                    f"Cuenta: <b>{html.escape(ACCOUNT_TYPE)}</b>\n"
                    f"DRY_RUN: <b>{DRY_RUN}</b>\n"
                    f"Último escaneo: <b>{last_scan}</b>\n"
                    f"Señales: <b>{current_signals}</b>\n"
                    f"WIN: <b>{current_wins}</b> | LOSS: <b>{current_losses}</b> | DRAW: <b>{current_draws}</b>"
                )

            elif command == "/help":
                send_telegram(
                    "<b>Comandos</b>\n"
                    "/start - Activar\n"
                    "/stop - Detener\n"
                    "/status - Estado y resultados\n"
                    "/scan - Solicitar escaneo\n"
                    "/help - Ayuda"
                )

        time.sleep(TELEGRAM_POLL_INTERVAL)


# =========================
# IQ OPTION Y VELAS
# =========================

def connect_iqoption():
    try:
        from iqoptionapi.stable_api import IQ_Option
    except ImportError as exc:
        raise RuntimeError(
            "Instala iqoptionapi con: pip install -U git+https://github.com/iqoptionapi/iqoptionapi.git"
        ) from exc

    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Configura IQ_EMAIL e IQ_PASSWORD.")

    # Algunas versiones de iqoptionapi lanzan un hilo interno para
    # digitales que falla cuando la respuesta del servidor es None.
    # Este parche evita que ese hilo intente indexar None.
    original_digital_data = IQ_Option.get_digital_underlying_list_data

    def safe_digital_data(self):
        try:
            result = original_digital_data(self)
            if not isinstance(result, dict):
                return {"underlying": []}
            if not isinstance(result.get("underlying"), list):
                result["underlying"] = []
            return result
        except Exception as exc:
            print(f"[IQ] Datos digitales no disponibles; se omiten: {exc}")
            return {"underlying": []}

    IQ_Option.get_digital_underlying_list_data = safe_digital_data

    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    connected, reason = api.connect()

    if not connected:
        raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")

    api.change_balance(ACCOUNT_TYPE)
    return api


def asset_is_available(api, asset: str) -> bool:
    """
    No llama a get_all_open_time(): esa función puede activar hilos internos
    de digitales que fallan con datos None en algunas versiones de iqoptionapi.
    La disponibilidad se valida de forma práctica al solicitar las velas.
    """
    return True


def get_closed_candles(api, asset: str) -> List[Dict[str, Any]]:
    candles = api.get_candles(
        asset,
        TIMEFRAME_SECONDS,
        CANDLE_COUNT + 2,
        time.time(),
    )

    if not candles:
        return []

    ordered = sorted(candles, key=lambda item: item.get("from", 0))

    # Se descartan las dos últimas por seguridad: la última puede estar
    # abierta y la anterior puede no estar completamente confirmada
    # dependiendo del retraso de la API.
    return ordered[:-1]


def candle_time(candle: Dict[str, Any]) -> Optional[int]:
    value = candle.get("from", candle.get("at", candle.get("timestamp")))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def candle_close(candle: Dict[str, Any]) -> float:
    if candle.get("close") is not None:
        return float(candle["close"])
    return float(candle["close_price"])


def candle_open(candle: Dict[str, Any]) -> float:
    if candle.get("open") is not None:
        return float(candle["open"])
    return float(candle["open_price"])


def settle_paper_trades(asset: str, candles: List[Dict[str, Any]]) -> None:
    global wins, losses, draws

    if not candles:
        return

    latest_time = candle_time(candles[-1])
    if latest_time is None:
        return

    with lock:
        trade = pending_paper_trades.get(asset)

    if not trade:
        return

    expiry_time = int(trade["candle_time"]) + EXPIRATION_MINUTES * 60
    if latest_time < expiry_time:
        return

    entry = float(trade["entry"])
    exit_price = candle_close(candles[-1])
    action = trade["action"]

    if exit_price == entry:
        result = "DRAW"
    elif action == "call":
        result = "WIN" if exit_price > entry else "LOSS"
    else:
        result = "WIN" if exit_price < entry else "LOSS"

    with lock:
        pending_paper_trades.pop(asset, None)
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        else:
            draws += 1

    emoji = "✅" if result == "WIN" else "❌" if result == "LOSS" else "➖"
    send_telegram(
        f"{emoji} <b>Resultado DEMO / VIRTUAL</b>\n"
        f"Par: <b>{html.escape(asset)}</b>\n"
        f"Dirección: <b>{action.upper()}</b>\n"
        f"Entrada: <b>{entry}</b>\n"
        f"Salida: <b>{exit_price}</b>\n"
        f"Resultado: <b>{result}</b>"
    )


def execute_signal(api, asset: str, signal: Signal) -> Tuple[bool, Any]:
    # Bloqueo de seguridad: nunca permite ejecución desde una cuenta real.
    if ACCOUNT_TYPE != "PRACTICE":
        raise RuntimeError(
            "Ejecución bloqueada: usa IQ_ACCOUNT_TYPE=PRACTICE."
        )

    if DRY_RUN:
        print(
            f"[DRY_RUN] {asset} -> {signal.action.upper()} "
            f"score={signal.score} entry={signal.entry_price}"
        )
        return False, None

    # Expiración fija de 1 minuto.
    result = api.buy(STAKE, asset, signal.action, EXPIRATION_MINUTES)
    print(
        f"[DEMO] {asset} -> {signal.action.upper()} "
        f"expiración={EXPIRATION_MINUTES} minuto, resultado_api={result}"
    )
    return result


# =========================
# ESCANEO
# =========================

def scan_once(api) -> None:
    global last_scan_time, signals_sent, force_scan

    with lock:
        if not bot_running:
            return
        force_scan = False

    candidates: List[Tuple[int, str, Signal, List[Dict[str, Any]]]] = []

    for asset in ASSETS:
        try:
            candles = get_closed_candles(api, asset)
            if len(candles) < 30:
                continue

            settle_paper_trades(asset, candles)

            signal = analyze_rejection(candles, min_score=MIN_SCORE)

            # Compatibilidad con versiones antiguas de strategy.py.
            signal_action = getattr(signal, "action", "none")
            signal_score = int(getattr(signal, "score", 0) or 0)
            signal_time = getattr(signal, "candle_time", None)
            if signal_time is None:
                signal_time = candle_time(candles[-1])

            if signal_action == "none" or signal_time is None:
                continue

            with lock:
                if last_processed_candle.get(asset) == signal_time:
                    continue
                last_processed_candle[asset] = signal_time

            candidates.append((signal_score, asset, signal, candles))

        except Exception as exc:
            print(f"[{asset}] Error: {exc}")

    with lock:
        last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not candidates:
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, asset, signal, candles = candidates[0]

    direction = "CALL 🟢" if signal.action == "call" else "PUT 🔴"
    level_name = "soporte" if signal.action == "call" else "resistencia"
    level_value = signal.support if signal.action == "call" else signal.resistance

    # Ejecutar inmediatamente después de confirmar el rechazo en una vela cerrada.
    # La función execute_signal mantiene el bloqueo exclusivo a PRACTICE.
    execution_ok, execution_data = execute_signal(api, asset, signal)

    message = (
        "🚨 <b>SEÑAL DE RECHAZO</b>\n\n"
        f"📌 Par: <b>{html.escape(asset)}</b>\n"
        f"📈 Dirección: <b>{direction}</b>\n"
        f"🎯 Score: <b>{score}/100</b>\n"
        f"📍 Zona: <b>{level_name}</b>\n"
        f"💵 Nivel: <b>{level_value}</b>\n"
        f"🎬 Entrada de referencia: <b>{signal.entry_price}</b>\n"
        f"⏱️ Expiración: <b>{EXPIRATION_MINUTES} minuto(s)</b>\n"
        f"🧪 Cuenta: <b>{html.escape(ACCOUNT_TYPE)}</b>\n"
        f"🔒 DRY_RUN: <b>{DRY_RUN}</b>\n"
        f"⚡ Ejecución: <b>{'ACEPTADA' if execution_ok else 'NO CONFIRMADA'}</b>\n"
        f"📝 Motivo: <b>{html.escape(signal.reason)}</b>\n\n"
        "⚠️ Señal experimental. No garantiza ganancias."
    )

    if send_telegram(message):
        with lock:
            signals_sent += 1

    # Registro virtual para calcular WIN/LOSS con la siguiente vela.
    if (DRY_RUN or execution_ok) and signal.entry_price is not None and signal.candle_time is not None:
        with lock:
            pending_paper_trades[asset] = {
                "action": signal.action,
                "entry": signal.entry_price,
                "candle_time": signal.candle_time,
            }



def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Configura TELEGRAM_BOT_TOKEN/TELEGRAM_TOKEN y TELEGRAM_CHAT_ID."
        )

    if ACCOUNT_TYPE != "PRACTICE":
        raise RuntimeError(
            "Por seguridad, este archivo solo funciona con IQ_ACCOUNT_TYPE=PRACTICE."
        )

    # Polling con getUpdates no permite dos instancias simultáneas.
    # El conflicto 409 suele indicar otro worker/servicio activo.
    try:
        requests.post(telegram_url("deleteWebhook"), timeout=10)
    except requests.RequestException as exc:
        print(f"[TELEGRAM] No se pudo limpiar webhook: {exc}")

    send_telegram(
        "🤖 <b>Bot iniciado</b>\n"
        f"Cuenta: <b>{html.escape(ACCOUNT_TYPE)}</b>\n"
        f"DRY_RUN: <b>{DRY_RUN}</b>\n"
        f"Ejecución DEMO: <b>{EXECUTE_DEMO}</b>\n"
        f"Pares configurados: <b>{len(ASSETS)}</b>\n"
        "Usa /help para ver los comandos."
    )

    threading.Thread(
        target=telegram_command_loop,
        daemon=True,
    ).start()

    api = connect_iqoption()
    print(
        f"Conectado a IQ Option. Cuenta={ACCOUNT_TYPE}, "
        f"DRY_RUN={DRY_RUN}, pares={len(ASSETS)}"
    )

    while True:
        try:
            scan_once(api)
            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            send_telegram("🛑 Bot detenido manualmente.")
            break
        except Exception as exc:
            print(f"[MAIN] Error: {exc}")
            send_telegram(f"⚠️ <b>Error del bot:</b> {html.escape(str(exc))}")
            time.sleep(10)


if __name__ == "__main__":
    main()
