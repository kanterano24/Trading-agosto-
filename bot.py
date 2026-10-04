"""
Bot M1 para hasta 59 pares BINARY OTC activos.
Analiza 200 velas cerradas por par con strategy.py.
Las órdenes, si se habilitan, se envían únicamente a PRACTICE.
"""
import logging, os, time, threading, json
import requests
from iqoptionapi.stable_api import IQ_Option

# Este bot solo usa binarias. Algunas versiones de iqoptionapi inician un hilo
# digital que falla cuando el servidor devuelve None o no incluye "underlying".
# Desactivamos únicamente ese recolector digital ANTES de crear/conectar IQ_Option.
def _disable_unused_digital_worker():
    method_name = "_IQ_Option__get_digital_open"
    original = getattr(IQ_Option, method_name, None)
    if callable(original):
        def _digital_worker_disabled(self, *args, **kwargs):
            logging.info("Recolector digital desactivado: el bot opera solo binarias.")
            return None
        setattr(IQ_Option, method_name, _digital_worker_disabled)

    # Protección adicional para variantes que llaman directamente al getter.
    getter = getattr(IQ_Option, "get_digital_underlying_list_data", None)
    if callable(getter):
        def _safe_digital_underlying_list_data(self, *args, **kwargs):
            try:
                data = getter(self, *args, **kwargs)
                if not isinstance(data, dict):
                    return {"underlying": []}
                if not isinstance(data.get("underlying"), list):
                    data["underlying"] = []
                return data
            except Exception as exc:
                logging.warning("Datos digitales omitidos (mercado no utilizado): %s", exc)
                return {"underlying": []}
        setattr(IQ_Option, "get_digital_underlying_list_data", _safe_digital_underlying_list_data)

_disable_unused_digital_worker()

from strategy import normalize_candles, describe_history, format_candle, analyze_market

TIMEFRAME = 60
HISTORY_COUNT = 200
MAX_OTC_PAIRS = max(1, min(59, int(os.getenv("MAX_OTC_PAIRS", "59"))))
AMOUNT = float(os.getenv("AMOUNT", "1"))
EXPIRATION_MINUTES = 1
POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "1")))
PAIR_REFRESH_SECONDS = max(60.0, float(os.getenv("PAIR_REFRESH_SECONDS", "600")))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
SEND_HISTORY = os.getenv("SEND_HISTORY", "false").lower() in ("1","true","yes")
ENABLE_TRADES = os.getenv("ENABLE_TRADES", "false").lower() in ("1","true","yes")
MIN_ENTRY_SECOND = float(os.getenv("MIN_ENTRY_SECOND", "1"))
MAX_ENTRY_SECOND = float(os.getenv("MAX_ENTRY_SECOND", "20"))
MAX_TRADES_PER_HOUR = max(1, int(os.getenv("MAX_TRADES_PER_HOUR", "6")))
TG_MIN_INTERVAL = max(1.2, float(os.getenv("TG_MIN_INTERVAL", "2.0")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
session = requests.Session()
last_tg_sent = 0.0

def telegram_send(message, retries=5):
    global last_tg_sent
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram no configurado; mensaje omitido.")
        return False
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(retries):
        pause=TG_MIN_INTERVAL-(time.monotonic()-last_tg_sent)
        if pause>0: time.sleep(pause)
        try:
            r=session.post(url, data={"chat_id":TELEGRAM_CHAT_ID,"text":message,
                                      "disable_web_page_preview":True}, timeout=25)
            if r.status_code==429:
                try: delay=float(r.json().get("parameters",{}).get("retry_after",5))
                except (ValueError,TypeError): delay=5
                logging.warning("Telegram 429; pausa %.1fs",delay+1)
                time.sleep(delay+1); continue
            r.raise_for_status()
            if not r.json().get("ok"): raise RuntimeError(f"Telegram: {r.text}")
            last_tg_sent=time.monotonic()
            return True
        except requests.RequestException as e:
            logging.warning("Telegram intento %d/%d: %s",attempt+1,retries,e)
            time.sleep(min(2**attempt,20))
    logging.error("Telegram no aceptó el mensaje.")
    return False

def split_messages(lines, max_chars=3000):
    chunks=[]; current=""
    for line in lines:
        addition=("\n\n" if current else "")+line
        if current and len(current)+len(addition)>max_chars:
            chunks.append(current); current=line
        else: current+=addition
    if current: chunks.append(current)
    return chunks

def connect_iq():
    if not IQ_EMAIL or not IQ_PASSWORD:
        raise RuntimeError("Faltan IQ_EMAIL/IQ_PASSWORD.")
    iq=IQ_Option(IQ_EMAIL,IQ_PASSWORD)
    result = iq.connect()
    if not isinstance(result, (tuple, list)) or len(result) < 1 or not result[0]:
        reason = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else result
        raise RuntimeError(f"No conecta IQ Option: {reason}")
    try:
        iq.change_balance("PRACTICE")
    except Exception as exc:
        logging.warning("No se pudo confirmar PRACTICE en este intento: %s", exc)
        raise
    time.sleep(1)
    logging.info("Conectado a IQ Option; PRACTICE solicitado.")
    return iq

def server_time(iq):
    try:
        value=iq.get_server_timestamp()
        return float(value) if value else time.time()
    except Exception:
        return time.time()

def discover_otc_pairs(iq):
    """Obtiene activos OTC binarios abiertos; si el endpoint falla, usa lista de activos."""
    found=[]
    try:
        data=iq.get_all_open_time() or {}
        binary = data.get("binary") or data.get("turbo") or {}
        if isinstance(binary, dict):
            for pair, info in binary.items():
                if (isinstance(pair, str) and "-OTC" in pair.upper()
                        and isinstance(info, dict) and info.get("open") is True):
                    found.append(pair)
    except Exception:
        logging.exception("No se pudo consultar el horario de activos binarios.")
    # Algunos builds no exponen correctamente el horario; usar códigos conocidos disponibles.
    if not found:
        # En varias versiones, get_all_ACTIVES_OPCODE devuelve {SIMBOLO: codigo};
        # en otras, el formato puede no ser utilizable. Nunca tratar IDs numéricos como pares.
        try:
            codes=iq.get_all_ACTIVES_OPCODE() or {}
            if isinstance(codes, dict):
                found=[str(name) for name in codes.keys()
                       if isinstance(name, str) and "-OTC" in name.upper()]
            logging.info("Fallback opcode: %d símbolos OTC candidatos", len(found))
        except Exception:
            logging.exception("No se pudo obtener la lista alternativa de activos.")
    pairs=sorted(set(found))[:MAX_OTC_PAIRS]
    if not pairs:
        raise RuntimeError("IQ Option no devolvió pares OTC binarios disponibles.")
    return pairs

def get_closed_candles(iq, pair, count=HISTORY_COUNT):
    now=server_time(iq)
    raw=iq.get_candles(pair,TIMEFRAME,count+30,int(now)) or []
    if not isinstance(raw, (list, tuple)):
        logging.warning("%s: respuesta de velas no válida (%s)", pair, type(raw).__name__)
        return []
    current_start=int(now//TIMEFRAME)*TIMEFRAME
    closed=[]
    for c in raw:
        try:
            ts=int(c.get("from",c.get("at",0)))
            if ts>0 and ts+TIMEFRAME<=current_start:
                closed.append(c)
        except (TypeError,ValueError):
            continue
    return normalize_candles(closed)[-count:]

def send_history(pair,candles):
    report=describe_history(candles)
    telegram_send(f"📊 HISTORIAL M1 | {pair}\nVelas: {report['count']}/{HISTORY_COUNT}\n"
                  f"Verdes: {report['greens']} | Rojas: {report['reds']} | Doji: {report['dojis']}\n"
                  f"Máximo: {report['highest']} | Mínimo: {report['lowest']}\n"
                  f"Cambio neto: {report['net_change']:+.6f}")
    for i,chunk in enumerate(split_messages([format_candle(n,c) for n,c in enumerate(candles,1)]),1):
        if not telegram_send(f"🕯️ {pair} | Bloque {i}\n\n{chunk}"):
            logging.error("Falló historial de %s bloque %d",pair,i)
            return False
    return True

def place_binary(iq,pair,direction):
    if not ENABLE_TRADES:
        return False, "ENABLE_TRADES=false; solo análisis"
    if direction not in ("CALL", "PUT"):
        return False, "Dirección inválida"
    if AMOUNT <= 0:
        return False, "AMOUNT debe ser mayor que cero"
    # Forzar cuenta de práctica inmediatamente antes de enviar la orden.
    iq.change_balance("PRACTICE")
    response = iq.buy(AMOUNT, pair, direction.lower(), EXPIRATION_MINUTES)
    if not isinstance(response, (tuple, list)) or len(response) < 2:
        return False, f"Respuesta inesperada de compra: {response!r}"
    return bool(response[0]), response[1]

def connect_with_retry():
    delay=5
    while True:
        try:
            logging.info("Iniciando conexión con IQ Option; cuenta PRACTICE.")
            return connect_iq()
        except Exception as exc:
            logging.exception("Conexión inicial fallida; nuevo intento en %ss", delay)
            telegram_send(f"⚠️ No conecta IQ Option: {type(exc).__name__}: {exc}. Reintento en {delay}s")
            time.sleep(delay)
            delay=min(delay*2,60)



bot_running = True
tg_offset = 0

def telegram_controls(iq, pairs):
    """Lee comandos de Telegram (/start, /stop, /status) y botones inline."""
    global tg_offset, bot_running
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r=session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                      params={"offset":tg_offset,"timeout":1,"allowed_updates":["message","callback_query"]},
                      timeout=5)
        r.raise_for_status()
        payload=r.json()
        if not payload.get("ok"):
            return
        for upd in payload.get("result",[]):
            tg_offset=max(tg_offset,int(upd.get("update_id",0))+1)
            msg=upd.get("message") or {}
            cb=upd.get("callback_query")
            chat_id=str((msg.get("chat") or {}).get("id",""))
            text_cmd=(msg.get("text") or "").strip().lower()
            if cb:
                chat_id=str((cb.get("message",{}).get("chat") or {}).get("id",""))
                text_cmd=(cb.get("data") or "").lower()
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue
            if text_cmd in ("/start","arrancar","start","bot_start"):
                bot_running=True
                answer="▶️ Análisis y operaciones habilitados (PRACTICE)."
            elif text_cmd in ("/stop","detener","stop","bot_stop"):
                bot_running=False
                answer="⏸ Bot detenido por comando. Railway seguirá ejecutando el proceso."
            elif text_cmd in ("/status","estado","status","bot_status"):
                answer=f"📡 Estado: {'ACTIVO' if bot_running else 'DETENIDO'}\\nPares cargados: {len(pairs)}\\nCuenta: PRACTICE"
            else:
                continue
            if cb:
                try:
                    session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                                 data={"callback_query_id":cb.get("id"),"text":answer[:180]},timeout=5)
                except requests.RequestException:
                    pass
            keyboard={"inline_keyboard":[[{"text":"▶️ Arrancar","callback_data":"bot_start"},
                                          {"text":"⏸ Detener","callback_data":"bot_stop"}],
                                         [{"text":"📊 Estado","callback_data":"bot_status"}]]}
            session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         data={"chat_id":TELEGRAM_CHAT_ID,"text":answer,
                               "reply_markup":__import__("json").dumps(keyboard)},timeout=8)
    except requests.RequestException as exc:
        logging.warning("No se pudieron consultar comandos Telegram: %s", exc)
    except (ValueError, TypeError, KeyError) as exc:
        logging.warning("Respuesta de Telegram no válida: %s", exc)

def main():
    iq=connect_with_retry()
    pairs=[]
    last_refresh=0.0
    last_seen={}
    last_direction={}
    history_sent=set()
    trades=[]
    telegram_send("🟢 Bot M1 iniciado\nMercado: OTC binario\nObjetivo: hasta 50 pares activos\n"
                  "Análisis: 200 velas cerradas M1 por par\nCuenta solicitada: PRACTICE")
    while True:
        try:
            telegram_controls(iq,pairs)
            if not bot_running:
                time.sleep(POLL_SECONDS)
                continue
            now_mono=time.monotonic()
            if not pairs or now_mono-last_refresh>=PAIR_REFRESH_SECONDS:
                pairs=discover_otc_pairs(iq)
                last_refresh=now_mono
                logging.info("Pares OTC detectados: %d/%d | %s",len(pairs),MAX_OTC_PAIRS,", ".join(pairs))
                telegram_send(f"🔄 Pares OTC activos detectados: {len(pairs)}\n"+", ".join(pairs))
            if not pairs:
                telegram_send("⚠️ Sin pares OTC detectados. Revisa disponibilidad BINARY OTC y versión de iqoptionapi.")
                time.sleep(POLL_SECONDS)
                continue
            for pair in pairs:
                try:
                    candles=get_closed_candles(iq,pair)
                    if len(candles)<HISTORY_COUNT:
                        logging.info("%s: velas %d/%d",pair,len(candles),HISTORY_COUNT)
                        continue
                    if SEND_HISTORY and pair not in history_sent:
                        if send_history(pair,candles): history_sent.add(pair)
                    candle_ts=candles[-1]["timestamp"]
                    if last_seen.get(pair)==candle_ts:
                        continue
                    last_seen[pair]=candle_ts
                    result=analyze_market(candles)
                    signal=result.get("signal","WAIT")
                    logging.info("%s señal=%s sesgo=%s CALL=%s PUT=%s: %s",
                                 pair,signal,result.get("bias"),result.get("call_score"),
                                 result.get("put_score"),result.get("reason"))
                    if signal not in ("CALL","PUT"):
                        continue
                    telegram_send(f"🔎 M1 {pair}\nSeñal: {signal}\nSesgo: {result.get('bias')}\n"
                                  f"Puntaje CALL/PUT: {result.get('call_score',0)}/{result.get('put_score',0)}\n"
                                  f"Motivo: {result.get('reason')}")
                    if signal==last_direction.get(pair):
                        logging.info("%s señal repetida omitida: %s",pair,signal)
                        continue
                    now=server_time(iq)
                    elapsed=now-(candle_ts+TIMEFRAME)
                    if elapsed<MIN_ENTRY_SECOND or elapsed>MAX_ENTRY_SECOND:
                        logging.info("%s señal omitida fuera de ventana (segundo %.1f)",pair,elapsed)
                        continue
                    cutoff=time.time()-3600
                    trades=[t for t in trades if t>cutoff]
                    if len(trades)>=MAX_TRADES_PER_HOUR:
                        logging.info("Límite horario alcanzado; señal %s %s omitida",pair,signal)
                        continue
                    ok,order_id=place_binary(iq,pair,signal)
                    if ok:
                        trades.append(time.time())
                        last_direction[pair]=signal
                        telegram_send(f"🧪 Orden demo aceptada\n{pair} | {signal}\nExpiración: 1 min\nID: {order_id}")
                        logging.info("Orden aceptada: %s %s id=%s",pair,signal,order_id)
                    else:
                        telegram_send(f"⚠️ Orden rechazada: {pair} {signal} | {order_id}")
                        logging.warning("Orden rechazada %s: %s",pair,order_id)
                except Exception as pair_exc:
                    logging.exception("Error analizando %s; se continúa con el siguiente par", pair)
                    if not iq.check_connect():
                        logging.warning("Conexión caída durante %s; se reconectará.", pair)
                        raise ConnectionError(f"Conexión IQ caída al consultar {pair}") from pair_exc
            time.sleep(POLL_SECONDS)
        except Exception as e:
            logging.exception("Error del ciclo general")
            telegram_send(f"⚠️ Error general: {type(e).__name__}: {e}")
            try:
                connected = False
                try:
                    connected = bool(iq.check_connect())
                except Exception:
                    connected = False
                if not connected:
                    logging.warning("Conexión IQ perdida; creando una sesión nueva.")
                    try:
                        iq.api.close()
                    except Exception:
                        pass
                    iq = connect_with_retry()
                    pairs = []
                    last_refresh = 0.0
            except Exception:
                logging.exception("Fallo durante la recuperación de conexión.")
            time.sleep(3)

if __name__=="__main__":
    main()
