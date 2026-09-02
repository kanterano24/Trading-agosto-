from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# COMPATIBILIDAD IQOPTIONAPI - BINARY OTC
# ============================================================
# Algunas versiones de iqoptionapi lanzan durante connect() un
# hilo interno llamado __get_digital_open. Ese hilo consulta
# get_digital_underlying_list_data() y, cuando IQ Option devuelve
# None, termina con:
# TypeError: 'NoneType' object is not subscriptable
#
# Este bot SOLO utiliza BINARY OTC, por lo que no necesita ese
# proceso DIGITAL. Se desactiva a nivel de clase ANTES de crear
# la instancia IQ_Option, evitando que el hilo se inicie.
_ORIGINAL_GET_DIGITAL_UNDERLYING = IQ_Option.get_digital_underlying_list_data


def _safe_get_digital_underlying_list_data(self):
    """Evita el TypeError cuando iqoptionapi devuelve None."""
    try:
        result = _ORIGINAL_GET_DIGITAL_UNDERLYING(self)
        if not isinstance(result, dict):
            return {"underlying": []}
        underlying = result.get("underlying")
        if not isinstance(underlying, list):
            result["underlying"] = []
        return result
    except Exception:
        return {"underlying": []}


def _disabled_digital_open(self) -> None:
    return None

# Correccion robusta: si la libreria intenta consultar DIGITAL y la
# respuesta es None, nunca se propaga None al codigo que hace
# ["underlying"]. Ademas, el worker DIGITAL queda desactivado.
setattr(
    IQ_Option,
    "get_digital_underlying_list_data",
    _safe_get_digital_underlying_list_data,
)
setattr(
    IQ_Option,
    "_IQ_Option__get_digital_open",
    _disabled_digital_open,
)


from strategy import analyze_market


# ============================================================
# BOT OTC - DIVERGENCIA RSI ESTRUCTURAL / SNIPER 5 MINUTOS
# ============================================================
#
# FLUJO:
#
# 1. Descubre todos los pares OTC Binary disponibles.
# 2. Cada par se analiza en M1.
# 3. Se espera el cierre completo de N.
# 4. Se analiza:
#       - OHLC
#       - cuerpo
#       - mechas
#       - posición del cierre
#       - estructura
#       - máximos
#       - mínimos
#       - RSI
#       - divergencia
#       - descanso
#       - rechazo
#       - recuperación
#       - dominancia
# 5. Si existe señal confirmada:
#       N = análisis
#       N+1 = ejecución
# 6. N+1 NO participa en la decisión.
# 7. Expiración = 5 minutos.
# ============================================================


# ============================================================
# CONFIGURACIÓN
# ============================================================

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME = 60
EXPIRATION = 5

AMOUNT = float(
    os.getenv(
        "AMOUNT",
        "10",
    )
)

CANDLE_COUNT = int(
    os.getenv(
        "CANDLE_COUNT",
        "60",
    )
)

MAX_OTC_PAIRS = int(
    os.getenv(
        "MAX_OTC_PAIRS",
        "50",
    )
)

PAIR_REFRESH_SECONDS = 60.0
SNIPER_POLL = 0.02
REALTIME_MAXDICT = 100
TRADE_COOLDOWN = 60.0


# ============================================================
# ESTADO GLOBAL
# ============================================================

BOT_RUNNING = False

IQ: Optional[IQ_Option] = None

PAIRS: list[str] = []

LAST_PAIR_REFRESH = 0.0

LIVE_STATE: Dict[str, Dict[str, Any]] = {}

PENDING_ENTRY: Dict[str, Dict[str, Any]] = {}

LAST_TRADE_TIME: Dict[str, float] = {}

LAST_TRADE_CANDLE: Dict[str, int] = {}

LAST_DIVERGENCE_NOTICE: Dict[str, int] = {}

STREAM_STARTED: Dict[str, bool] = {}

STATE_LOCK = threading.RLock()


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================

def _telegram_post(
    endpoint: str,
    data: Dict[str, Any],
    timeout: float = 3.0,
) -> bool:

    if not TELEGRAM_TOKEN:
        return False

    try:
        response = requests.post(
            (
                "https://api.telegram.org/"
                f"bot{TELEGRAM_TOKEN}/"
                f"{endpoint}"
            ),
            data=data,
            timeout=timeout,
        )

        return response.status_code == 200

    except Exception as exc:
        logger.warning(
            "Telegram %s: %s",
            endpoint,
            exc,
        )
        return False


def telegram_send(message: str) -> None:

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return

    def worker() -> None:
        _telegram_post(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=3.0,
        )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


def telegram_command_loop() -> None:

    global BOT_RUNNING

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        return

    last_update_id: Optional[int] = None

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/getUpdates"
    )

    while True:
        try:
            params: Dict[str, Any] = {
                "timeout": 1,
            }

            if last_update_id is not None:
                params["offset"] = (
                    last_update_id + 1
                )

            response = requests.get(
                url,
                params=params,
                timeout=3,
            )

            data = response.json()

            if not data.get("ok"):
                time.sleep(0.5)
                continue

            for update in data.get(
                "result",
                [],
            ):
                uid = update.get("update_id")

                if uid is not None:
                    last_update_id = int(uid)

                message = (
                    update.get("message")
                    or {}
                )

                text = str(
                    message.get(
                        "text",
                        "",
                    )
                ).strip().lower()

                chat_id = str(
                    (
                        message.get("chat")
                        or {}
                    ).get(
                        "id",
                        "",
                    )
                )

                if (
                    chat_id
                    != str(TELEGRAM_CHAT_ID)
                ):
                    continue

                if text == "/start":
                    BOT_RUNNING = True

                    telegram_send(
                        "🟢 BOT ACTIVADO\n\n"
                        "⚡ SNIPER OTC\n"
                        "🧠 DIVERGENCIA RSI "
                        "ESTRUCTURAL\n\n"
                        f"OTC analizados: "
                        f"hasta {MAX_OTC_PAIRS}\n"
                        "⏱ Análisis: M1\n"
                        "🎯 Entrada: N+1\n"
                        "⏳ Expiración: "
                        "5 minutos\n"
                        f"💵 Importe: "
                        f"{AMOUNT:g}"
                    )

                elif text == "/stop":
                    BOT_RUNNING = False

                    telegram_send(
                        "🔴 BOT DETENIDO\n\n"
                        "No se abrirán "
                        "nuevas operaciones."
                    )

                elif text == "/status":
                    status = (
                        "🟢 ACTIVO"
                        if BOT_RUNNING
                        else "🔴 DETENIDO"
                    )

                    telegram_send(
                        "📊 ESTADO\n\n"
                        f"Estado: {status}\n"
                        "Modo: SNIPER\n"
                        "Mercado: BINARY OTC\n"
                        "Estrategia: "
                        "DIVERGENCIA RSI\n"
                        "Temporalidad: 1 minuto\n"
                        "Entrada: N+1\n"
                        "Expiración: 5 minutos\n"
                        f"Importe: {AMOUNT:g}\n"
                        f"Pares OTC: "
                        f"{len(PAIRS)}"
                    )

        except Exception as exc:
            logger.warning(
                "Telegram commands: %s",
                exc,
            )
            time.sleep(1)


# ============================================================
# OTC
# ============================================================

def _is_otc_pair(value: Any) -> bool:

    try:
        name = str(value).strip().upper()
    except Exception:
        return False

    return (
        name.endswith("-OTC")
        or name.endswith("_OTC")
        or "OTC" in name
    )


def _binary_open_pairs_from_open_time() -> list[str]:

    if IQ is None:
        return []

    try:
        data = IQ.get_all_open_time()

        binary = (
            data.get("binary", {})
            if isinstance(data, dict)
            else {}
        )

        if not isinstance(binary, dict):
            return []

        result: list[str] = []

        for asset, info in binary.items():

            if (
                not _is_otc_pair(asset)
                or not isinstance(info, dict)
            ):
                continue

            is_open = bool(
                info.get("open")
                or info.get("enabled")
                or (
                    isinstance(
                        info.get("binary"),
                        dict,
                    )
                    and info["binary"].get("open")
                )
            )

            if is_open:
                result.append(str(asset))

        return sorted(set(result))

    except Exception as exc:
        logger.warning(
            "OTC discovery error: %s",
            exc,
        )
        return []


def _binary_assets_from_init_v2() -> list[str]:

    if (
        IQ is None
        or not hasattr(
            IQ,
            "get_all_init_v2",
        )
    ):
        return []

    try:
        data = IQ.get_all_init_v2()
    except Exception:
        return []

    found: set[str] = set()

    def walk(obj: Any) -> None:

        if isinstance(obj, dict):

            for key in (
                "name",
                "symbol",
                "asset",
                "active",
                "pair",
            ):
                value = obj.get(key)

                if (
                    isinstance(value, str)
                    and _is_otc_pair(value)
                ):
                    suspended = bool(
                        obj.get("is_suspended")
                        or obj.get("suspended")
                    )

                    enabled = obj.get(
                        "enabled",
                        True,
                    )

                    if (
                        not suspended
                        and enabled is not False
                    ):
                        found.add(value)

            for key, value in obj.items():

                if (
                    isinstance(key, str)
                    and _is_otc_pair(key)
                    and isinstance(value, dict)
                ):
                    suspended = bool(
                        value.get("is_suspended")
                        or value.get("suspended")
                    )

                    enabled = value.get(
                        "enabled",
                        True,
                    )

                    opened = value.get(
                        "open",
                        True,
                    )

                    if (
                        not suspended
                        and enabled is not False
                        and opened is not False
                    ):
                        found.add(key)

                walk(value)

        elif isinstance(obj, (list, tuple)):

            for item in obj:
                walk(item)

    walk(data)

    return sorted(found)


def discover_binary_otc_pairs() -> list[str]:

    # No usar get_all_open_time(): en varias versiones de
    # iqoptionapi esa función también consulta DIGITAL y puede
    # provocar el mismo error de NoneType.
    pairs = _binary_assets_from_init_v2()

    return sorted(
        set(
            p
            for p in pairs
            if _is_otc_pair(p)
        )
    )


def refresh_binary_otc_pairs(
    force: bool = False,
) -> list[str]:

    global PAIRS
    global LAST_PAIR_REFRESH

    now = time.time()

    if (
        not force
        and now - LAST_PAIR_REFRESH
        < PAIR_REFRESH_SECONDS
    ):
        return list(PAIRS)

    discovered = discover_binary_otc_pairs()

    selected = discovered[:MAX_OTC_PAIRS]

    previous = set(PAIRS)
    current = set(selected)

    with STATE_LOCK:

        PAIRS = list(selected)

        LAST_PAIR_REFRESH = now

        for pair in previous - current:

            PENDING_ENTRY.pop(
                pair,
                None,
            )

            LIVE_STATE.pop(
                pair,
                None,
            )

            LAST_TRADE_CANDLE.pop(
                pair,
                None,
            )

            LAST_DIVERGENCE_NOTICE.pop(
                pair,
                None,
            )

            STREAM_STARTED.pop(
                pair,
                None,
            )

    if set(selected) != previous:

        # CORRECCIÓN PRINCIPAL DEL ERROR:
        # La expresión condicional debe estar COMPLETA
        # dentro del f-string.
        selected_text = (
            ", ".join(selected)
            if selected
            else "NINGUNO"
        )

        msg = (
            "🔄 UNIVERSO OTC "
            "ACTUALIZADO\n\n"
            f"Pares disponibles: "
            f"{len(selected)}/{MAX_OTC_PAIRS}\n\n"
            f"{selected_text}"
        )

        logger.info(
            msg.replace("\n", " | ")
        )

        telegram_send(msg)

    start_realtime_streams()

    return list(PAIRS)


# ============================================================
# RELOJ IQ
# ============================================================

def get_iq_server_timestamp() -> float:

    if IQ is None:
        return time.time()

    try:
        value = float(
            IQ.get_server_timestamp()
        )

        if value > 0:
            return value

    except Exception:
        pass

    return time.time()


def floor_candle_timestamp(
    timestamp: float,
) -> int:

    return (
        int(timestamp // TIMEFRAME)
        * TIMEFRAME
    )


# ============================================================
# CONEXIÓN
# ============================================================

def connect_iq() -> bool:

    global IQ

    if (
        not IQ_EMAIL
        or not IQ_PASSWORD
    ):
        raise ValueError(
            "Faltan IQ_EMAIL/IQ_PASSWORD"
        )

    logger.info(
        "Conectando a IQ Option..."
    )

    IQ = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    connected, reason = IQ.connect()

    if not connected:
        raise ConnectionError(
            "No se pudo conectar a "
            f"IQ Option: {reason}"
        )

    refresh_binary_otc_pairs(force=True)

    start_realtime_streams()

    server_ts = get_iq_server_timestamp()

    logger.info(
        "IQ conectado | server=%.3f",
        server_ts,
    )

    telegram_send(
        "🟢 IQ OPTION CONECTADO\n\n"
        "⚡ MODO SNIPER\n"
        "🧠 DIVERGENCIA RSI "
        "ESTRUCTURAL\n"
        "⏱ M1 cerrada → N+1\n"
        "⏳ Expiración: 5 minutos"
    )

    return True


def ensure_connection() -> bool:

    global IQ

    try:

        if IQ is None:
            return connect_iq()

        if IQ.check_connect():
            return True

        logger.warning(
            "Conexión perdida. "
            "Reconectando..."
        )

        connected, reason = IQ.connect()

        if not connected:

            logger.error(
                "No se pudo reconectar: %s",
                reason,
            )

            return False

        STREAM_STARTED.clear()

        refresh_binary_otc_pairs(
            force=True
        )

        start_realtime_streams()

        telegram_send(
            "🟢 IQ OPTION "
            "RECONECTADO"
        )

        return True

    except Exception as exc:

        logger.error(
            "Error conexión: %s",
            exc,
        )

        return False


# ============================================================
# STREAM M1
# ============================================================

def start_realtime_streams() -> None:

    if IQ is None:
        return

    for pair in list(PAIRS):

        try:

            if STREAM_STARTED.get(pair):
                continue

            IQ.start_candles_stream(
                pair,
                TIMEFRAME,
                REALTIME_MAXDICT,
            )

            STREAM_STARTED[pair] = True

            logger.info(
                "%s | stream M1 iniciado",
                pair,
            )

        except Exception as exc:

            STREAM_STARTED[pair] = False

            logger.warning(
                "%s | stream error: %s",
                pair,
                exc,
            )


def realtime_dataframe(
    pair: str,
) -> pd.DataFrame:

    if IQ is None:
        return pd.DataFrame()

    try:
        candles = IQ.get_realtime_candles(
            pair,
            TIMEFRAME,
        )
    except Exception:
        return pd.DataFrame()

    if (
        not isinstance(candles, dict)
        or not candles
    ):
        return pd.DataFrame()

    rows = []

    for key, candle in candles.items():

        if not isinstance(candle, dict):
            continue

        try:

            ts = int(
                float(
                    candle.get(
                        "from",
                        key,
                    )
                )
            )

            rows.append(
                {
                    "from": ts,
                    "open": float(candle["open"]),
                    "high": float(
                        candle.get(
                            "max",
                            candle.get("high"),
                        )
                    ),
                    "low": float(
                        candle.get(
                            "min",
                            candle.get("low"),
                        )
                    ),
                    "close": float(candle["close"]),
                }
            )

        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = (
        pd.DataFrame(rows)
        .drop_duplicates(
            "from",
            keep="last",
        )
        .sort_values("from")
    )

    return (
        df
        .tail(CANDLE_COUNT)
        .reset_index(drop=True)
    )


def get_closed_candles(
    pair: str,
) -> Optional[pd.DataFrame]:

    if IQ is None:
        return None

    try:

        candles = IQ.get_candles(
            pair,
            TIMEFRAME,
            CANDLE_COUNT,
            get_iq_server_timestamp(),
        )

        if not candles:
            return None

        df = pd.DataFrame(candles).rename(
            columns={
                "max": "high",
                "min": "low",
            }
        )

        required = [
            "from",
            "open",
            "high",
            "low",
            "close",
        ]

        if not all(
            c in df.columns
            for c in required
        ):
            return None

        for col in required:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            )

        df.dropna(
            subset=required,
            inplace=True,
        )

        df["from"] = df["from"].astype(int)

        return (
            df
            .drop_duplicates(
                "from",
                keep="last",
            )
            .sort_values("from")
            .tail(CANDLE_COUNT)
            .reset_index(drop=True)
        )

    except Exception as exc:

        logger.debug(
            "%s | historial: %s",
            pair,
            exc,
        )

        return None


def get_row_by_ts(
    df: pd.DataFrame,
    ts: int,
) -> Optional[pd.Series]:

    if (
        df is None
        or df.empty
        or "from" not in df.columns
    ):
        return None

    rows = df[
        df["from"].astype(int) == int(ts)
    ]

    if rows.empty:
        return None

    return rows.iloc[-1]


def candle_values(
    row: pd.Series,
) -> Dict[str, float]:

    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    }


# ============================================================
# MENSAJE DIVERGENCIA
# ============================================================

def divergence_message(
    pair: str,
    ts: int,
    result: Dict[str, Any],
) -> str:

    div = (
        result
        .get("analysis", {})
        .get("divergence", {})
    )

    bull = bool(div.get("bullish"))
    bear = bool(div.get("bearish"))

    if bull:

        side = "🟢 DIVERGENCIA ALCISTA"

        p1 = div.get("previous_low")
        p2 = div.get("current_low")
        r1 = div.get("previous_low_rsi")
        r2 = div.get("current_low_rsi")

        score = div.get(
            "bullish_score",
            0,
        )

        details = (
            f"Precio: {p2} < {p1}\n"
            f"RSI: {r2:.2f} > {r1:.2f}"
        )

        rsi_difference = div.get(
            "bull_rsi_difference",
            0,
        )

    elif bear:

        side = "🔴 DIVERGENCIA BAJISTA"

        p1 = div.get("previous_high")
        p2 = div.get("current_high")
        r1 = div.get("previous_high_rsi")
        r2 = div.get("current_high_rsi")

        score = div.get(
            "bearish_score",
            0,
        )

        details = (
            f"Precio: {p2} > {p1}\n"
            f"RSI: {r2:.2f} < {r1:.2f}"
        )

        rsi_difference = div.get(
            "bear_rsi_difference",
            0,
        )

    else:

        return (
            "⚠️ Divergencia sin datos "
            "completos"
        )

    c = (
        result
        .get("analysis", {})
        .get("candle", {})
    )

    cond = (
        result
        .get("analysis", {})
        .get("conditions", {})
    )

    return (
        f"{side}\n\n"
        f"Par: {pair}\n"
        f"Vela N: {ts}\n"
        f"Score: {score}/100\n\n"
        f"{details}\n\n"
        f"RSI diferencia: "
        f"{rsi_difference:.2f}\n\n"
        f"Apertura N: {c.get('open')}\n"
        f"Máximo N: {c.get('high')}\n"
        f"Mínimo N: {c.get('low')}\n"
        f"Cierre N: {c.get('close')}\n"
        f"Cuerpo: "
        f"{c.get('body_percent', 0):.1f}%\n"
        f"Posición cierre: "
        f"{c.get('close_position', 0):.1f}%\n\n"
        f"Vela anterior fuerte: "
        f"{'✅' if cond.get('previous_strong') else '❌'}\n"
        f"Vela descanso: "
        f"{'✅' if cond.get('current_rest') else '❌'}\n"
        f"Recuperación alcista: "
        f"{'✅' if cond.get('bull_recovery') else '❌'}\n"
        f"Recuperación bajista: "
        f"{'✅' if cond.get('bear_recovery') else '❌'}\n"
        f"Dominancia alcista: "
        f"{'✅' if cond.get('bull_dominance') else '❌'}\n"
        f"Dominancia bajista: "
        f"{'✅' if cond.get('bear_dominance') else '❌'}\n\n"
        f"Resultado:\n"
        f"{result.get('reason')}"
    )


# ============================================================
# ANALIZAR VELA CERRADA
# ============================================================

def analyze_closed_candle(
    pair: str,
    expected_closed_ts: int,
) -> bool:

    realtime = realtime_dataframe(pair)

    closed_row = get_row_by_ts(
        realtime,
        expected_closed_ts,
    )

    df = realtime

    if closed_row is None:

        df = get_closed_candles(pair)

        if df is not None:
            closed_row = get_row_by_ts(
                df,
                expected_closed_ts,
            )

    if (
        closed_row is None
        or df is None
        or len(df) < 25
    ):
        return False

    # Solo hasta N.
    df = df[
        df["from"].astype(int)
        <= expected_closed_ts
    ].copy()

    df = (
        df
        .sort_values("from")
        .reset_index(drop=True)
    )

    if len(df) < 22:
        return False

    # Evitar volver a analizar N.
    current_state = LIVE_STATE.get(pair)

    if (
        current_state is not None
        and int(
            current_state.get(
                "analyzed_ts",
                -1,
            )
        )
        == expected_closed_ts
    ):
        return True

    # ANÁLISIS
    result = analyze_market(
        candle_1m=closed_row.to_dict(),
        previous_m1=df.iloc[:-1].copy(),
        pair=pair,
    )

    # MARCAR COMO ANALIZADA
    with STATE_LOCK:

        LIVE_STATE[pair] = {
            "analyzed_ts": int(expected_closed_ts),
            "signal": result.get("signal"),
            "score": int(
                result.get(
                    "score",
                    0,
                )
            ),
            "reason": result.get(
                "reason",
                "",
            ),
            "analysis": result.get(
                "analysis",
                {},
            ),
            "created_at": time.time(),
        }

    # DIVERGENCIA
    div = (
        result
        .get("analysis", {})
        .get("divergence", {})
    )

    has_divergence = bool(
        div.get("bullish")
        or div.get("bearish")
    )

    if (
        has_divergence
        and LAST_DIVERGENCE_NOTICE.get(pair)
        != expected_closed_ts
    ):

        LAST_DIVERGENCE_NOTICE[pair] = (
            expected_closed_ts
        )

        msg = divergence_message(
            pair,
            expected_closed_ts,
            result,
        )

        logger.info(
            "%s",
            msg.replace("\n", " | "),
        )

        telegram_send(msg)

    # LOG
    signal = result.get("signal")

    score = int(
        result.get(
            "score",
            0,
        )
    )

    values = candle_values(closed_row)

    logger.info(
        "%s | N CERRADA | "
        "signal=%s | score=%s | %s",
        pair,
        signal,
        score,
        result.get("reason"),
    )

    # SIN SEÑAL
    if signal not in ("call", "put"):
        return True

    # ARMAR N+1
    execution_ts = int(
        expected_closed_ts + TIMEFRAME
    )

    with STATE_LOCK:

        PENDING_ENTRY[pair] = {
            "signal": signal,
            "score": score,
            "continuity_ts": int(
                expected_closed_ts
            ),
            "execution_ts": execution_ts,
            "open": values["open"],
            "high": values["high"],
            "low": values["low"],
            "close": values["close"],
            "reason": result.get(
                "reason",
                "",
            ),
            "analysis": result.get(
                "analysis",
                {},
            ),
            "created_at": time.time(),
        }

    side = (
        "CALL 🟢"
        if signal == "call"
        else "PUT 🔴"
    )

    telegram_send(
        "🎯 SEÑAL COMPLETA — "
        "ENTRADA ARMADA\n\n"
        f"Par: {pair}\n"
        f"Dirección: {side}\n"
        f"Score: {score}/100\n\n"
        f"Apertura N: {values['open']}\n"
        f"Máximo N: {values['high']}\n"
        f"Mínimo N: {values['low']}\n"
        f"Cierre N: {values['close']}\n\n"
        f"N cierre: {expected_closed_ts}\n"
        f"N+1: {execution_ts}\n\n"
        "🚫 N no se opera.\n"
        "⚡ SNIPER: ejecutar "
        "al abrir N+1.\n"
        "⏳ Expiración: 5 minutos\n\n"
        f"{result.get('reason', '')}"
    )

    return True


# ============================================================
# COOLDOWN
# ============================================================

def cooldown_active(pair: str) -> bool:

    return (
        time.time()
        - LAST_TRADE_TIME.get(pair, 0.0)
        < TRADE_COOLDOWN
    )


# ============================================================
# COMPRA
# ============================================================

def buy_binary(
    pair: str,
    signal: str,
) -> Tuple[bool, Optional[Any]]:

    if (
        IQ is None
        or signal not in ("call", "put")
    ):
        return False, None

    try:

        result = IQ.buy(
            AMOUNT,
            pair,
            signal,
            EXPIRATION,
        )

        if isinstance(result, tuple):

            return (
                bool(result[0]),
                (
                    result[1]
                    if len(result) > 1
                    else None
                ),
            )

        if result not in (
            None,
            False,
            "error",
            -1,
        ):
            return True, result

        return False, result

    except Exception as exc:

        logger.error(
            "%s | buy error: %s",
            pair,
            exc,
        )

        return False, None


# ============================================================
# SNIPER
# ============================================================

def execute_sniper(
    pair: str,
    pending: Dict[str, Any],
) -> bool:

    execution_ts = int(
        pending["execution_ts"]
    )

    signal = str(
        pending["signal"]
    )

    # RELOJ IQ
    now = get_iq_server_timestamp()

    current_ts = floor_candle_timestamp(now)

    # Todavía no llegó N+1.
    if current_ts < execution_ts:
        return False

    # Ya pasó N+1.
    if current_ts > execution_ts:

        with STATE_LOCK:
            PENDING_ENTRY.pop(
                pair,
                None,
            )

        telegram_send(
            "⚠️ SNIPER DESCARTADO\n\n"
            f"Par: {pair}\n"
            f"N+1 objetivo: {execution_ts}\n"
            f"Minuto actual: {current_ts}\n\n"
            "La señal no se "
            "trasladó a otra vela."
        )

        return False

    # PROTECCIONES
    if (
        LAST_TRADE_CANDLE.get(pair)
        == execution_ts
    ):
        return False

    if cooldown_active(pair):
        return False

    # VERIFICACIÓN FINAL
    now2 = get_iq_server_timestamp()

    if (
        floor_candle_timestamp(now2)
        != execution_ts
    ):
        return False

    telegram_send(
        "⚡ SNIPER EJECUTANDO\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n\n"
        f"N cierre: {pending['close']}\n"
        f"N+1 timestamp: {execution_ts}\n"
        f"Reloj IQ: {now2:.3f}\n\n"
        "⏳ Expiración: 5 minutos"
    )

    sent_at = get_iq_server_timestamp()

    # ORDEN
    ok, order_id = buy_binary(
        pair,
        signal,
    )

    if not ok:

        with STATE_LOCK:
            PENDING_ENTRY.pop(
                pair,
                None,
            )

        telegram_send(
            "❌ ORDEN BINARY "
            "RECHAZADA\n\n"
            f"Par: {pair}\n"
            f"Dirección: {signal.upper()}\n"
            f"N+1: {execution_ts}\n"
            f"Reloj IQ: {sent_at:.3f}\n\n"
            "La señal no se "
            "trasladará a otra vela."
        )

        return False

    # REGISTRAR OPERACIÓN
    LAST_TRADE_TIME[pair] = time.time()
    LAST_TRADE_CANDLE[pair] = execution_ts

    with STATE_LOCK:
        PENDING_ENTRY.pop(
            pair,
            None,
        )

    telegram_send(
        "✅ SNIPER EJECUTADO\n\n"
        f"Par: {pair}\n"
        f"Dirección: {signal.upper()}\n"
        f"N cierre: {pending['close']}\n"
        f"N+1: {execution_ts}\n"
        f"Reloj envío IQ: {sent_at:.3f}\n"
        f"ID: {order_id}\n\n"
        "⚡ Entrada inmediata N+1\n"
        "⏳ Expiración: 5 minutos"
    )

    logger.info(
        "%s | SNIPER EJECUTADO | "
        "%s | N=%s | N+1=%s | ID=%s",
        pair,
        signal.upper(),
        pending["continuity_ts"],
        execution_ts,
        order_id,
    )

    return True


# ============================================================
# MOTOR POR PAR
# ============================================================

def process_pair(pair: str) -> None:

    if IQ is None:
        return

    server_now = get_iq_server_timestamp()

    current_ts = floor_candle_timestamp(
        server_now
    )

    closed_ts = current_ts - TIMEFRAME

    # ANALIZAR N UNA SOLA VEZ
    state = LIVE_STATE.get(pair)

    analyzed_ts = -1

    if state is not None:

        try:
            analyzed_ts = int(
                state.get(
                    "analyzed_ts",
                    -1,
                )
            )
        except Exception:
            analyzed_ts = -1

    if analyzed_ts != closed_ts:

        analyze_closed_candle(
            pair,
            closed_ts,
        )

    # ENTRADA PENDIENTE
    pending = PENDING_ENTRY.get(pair)

    if pending is None:
        return

    if int(
        pending["execution_ts"]
    ) != current_ts:
        return

    execute_sniper(
        pair,
        pending,
    )


# ============================================================
# ANALIZAR TODOS LOS OTC
# ============================================================

def analyze_all_pairs() -> None:

    if not BOT_RUNNING:
        return

    refresh_binary_otc_pairs()

    for pair in list(PAIRS):

        if not BOT_RUNNING:
            return

        try:
            process_pair(pair)

        except Exception:
            logger.exception(
                "Error procesando %s",
                pair,
            )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    global BOT_RUNNING

    logger.info(
        "========================================"
    )
    logger.info("BOT BINARY OTC")
    logger.info(
        "DIVERGENCIA RSI ESTRUCTURAL"
    )
    logger.info(
        "MODO SNIPER - EXPIRACION 5 MINUTOS"
    )
    logger.info("TIMEFRAME M1")
    logger.info(
        "MAX OTC: %d",
        MAX_OTC_PAIRS,
    )
    logger.info(
        "AMOUNT: %s",
        AMOUNT,
    )
    logger.info(
        "========================================"
    )

    # VARIABLES
    required = {
        "IQ_EMAIL": IQ_EMAIL,
        "IQ_PASSWORD": IQ_PASSWORD,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [
        key
        for key, value in required.items()
        if not value
    ]

    if missing:

        logger.error(
            "Faltan variables: %s",
            ", ".join(missing),
        )

        return

    # TELEGRAM
    threading.Thread(
        target=telegram_command_loop,
        daemon=True,
    ).start()

    # IQ
    try:
        connect_iq()

    except Exception as exc:

        logger.exception(
            "No se pudo iniciar "
            "IQ Option"
        )

        telegram_send(
            "❌ ERROR DE CONEXIÓN\n\n"
            f"{exc}"
        )

        return

    # ARRANCA DETENIDO
    BOT_RUNNING = False

    telegram_send(
        "🤖 BOT LISTO\n\n"
        "🧠 DIVERGENCIA RSI "
        "ESTRUCTURAL\n"
        "🔎 Analiza todos los "
        "OTC BINARY disponibles.\n\n"
        "📌 La señal se decide "
        "al cierre de N.\n"
        "🚫 N nunca se opera.\n"
        "⚡ N+1 se ejecuta en "
        "modo SNIPER.\n"
        "⏳ Expiración: "
        "5 minutos.\n\n"
        "Usa /start para activar."
    )

    # LOOP
    while True:

        try:

            if not BOT_RUNNING:

                time.sleep(0.25)
                continue

            if not ensure_connection():

                time.sleep(1)
                continue

            analyze_all_pairs()

            time.sleep(SNIPER_POLL)

        except KeyboardInterrupt:

            BOT_RUNNING = False

            telegram_send(
                "🔴 BOT DETENIDO "
                "MANUALMENTE"
            )

            break

        except Exception as exc:

            logger.exception(
                "Error principal"
            )

            telegram_send(
                "⚠️ ERROR EN BOT\n\n"
                f"{exc}"
            )

            time.sleep(1)


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    main()
