"""
Bot M1 para hasta 50 pares BINARY OTC activos.
Analiza 200 velas cerradas por par con strategy.py.
Las órdenes, si se habilitan, se envían únicamente a PRACTICE.
"""
import logging, os, time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle, analyze_market

TIMEFRAME = 60
HISTORY_COUNT = 200
MAX_OTC_PAIRS = max(1, min(50, int(os.getenv("MAX_OTC_PAIRS", "50"))))
AMOUNT = float(os.getenv("AMOUNT", "1"))
EXPIRATION_MINUTES = 1
POLL_SECONDS = max(1.0, float(os.getenv("POLL_SECONDS", "1")))
PAIR_REFRESH_SECONDS = max(60.0, float(os.getenv("PAIR_REFRESH_SECONDS", "600")))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
SEND_HISTORY = os.getenv("SEND_HISTORY", "false").lower() in ("1","true","yes")
ENABLE_TRADES = os.getenv("ENABLE_TRADES", "true").lower() in ("1","true","yes")
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
    ok, reason=iq.connect()
    if not ok: raise RuntimeError(f"No conecta IQ Option: {reason}")
    iq.change_balance("PRACTICE")
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
        binary=data.get("binary") or {}
        for pair, info in binary.items():
            if "-OTC" in pair.upper() and isinstance(info,dict) and info.get("open") is True:
                found.append(pair)
    except Exception:
        logging.exception("No se pudo consultar el horario de activos binarios.")
    # Algunos builds no exponen correctamente el horario; usar códigos conocidos disponibles.
    if not found:
        try:
            codes=iq.get_all_ACTIVES_OPCODE() or {}
            found=[name for name in codes if "-OTC" in str(name).upper()]
        except Exception:
            logging.exception("No se pudo obtener la lista alternativa de activos.")
    pairs=sorted(set(found))[:MAX_OTC_PAIRS]
    if not pairs:
        raise RuntimeError("IQ Option no devolvió pares OTC binarios disponibles.")
    return pairs

def get_closed_candles(iq, pair, count=HISTORY_COUNT):
    now=server_time(iq)
    raw=iq.get_candles(pair,TIMEFRAME,count+30,int(now)) or []
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
    if not ENABLE_TRADES: return False,"ENABLE_TRADES=false"
    iq.change_balance("PRACTICE")
    return iq.buy(AMOUNT,pair,direction.lower(),EXPIRATION_MINUTES)

def main():
    iq=connect_iq()
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
            now_mono=time.monotonic()
            if not pairs or now_mono-last_refresh>=PAIR_REFRESH_SECONDS:
                pairs=discover_otc_pairs(iq)
                last_refresh=now_mono
                logging.info("Pares OTC detectados: %d/%d | %s",len(pairs),MAX_OTC_PAIRS,", ".join(pairs))
                telegram_send(f"🔄 Pares OTC activos detectados: {len(pairs)}\n"+", ".join(pairs))
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
                except Exception:
                    logging.exception("Error analizando %s; se continúa con el siguiente par",pair)
            time.sleep(POLL_SECONDS)
        except Exception as e:
            logging.exception("Error del ciclo general")
            telegram_send(f"⚠️ Error general: {type(e).__name__}: {e}")
            try:
                if not iq.check_connect():
                    logging.warning("Reconectando IQ Option...")
                    iq.connect()
                    iq.change_balance("PRACTICE")
            except Exception:
                logging.exception("No se pudo reconectar.")
            time.sleep(3)

if __name__=="__main__":
    main()
