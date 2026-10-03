"""
GBPUSD-OTC M1 bot — historial de 200 velas + análisis + entradas en PRACTICE.
Variables requeridas: IQ_EMAIL, IQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
No opera en REAL: el código fuerza PRACTICE antes de enviar órdenes.
"""
import logging, os, time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle, analyze_market

PAIR = os.getenv("ANALYSIS_PAIR", "GBPUSD-OTC").strip().upper()
TIMEFRAME = 60
HISTORY_COUNT = 200
AMOUNT = float(os.getenv("AMOUNT", "1"))
EXPIRATION_MINUTES = 1
POLL_SECONDS = max(0.5, float(os.getenv("POLL_SECONDS", "1")))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IQ_EMAIL, IQ_PASSWORD = os.getenv("IQ_EMAIL", ""), os.getenv("IQ_PASSWORD", "")
SEND_HISTORY = os.getenv("SEND_HISTORY", "true").lower() in ("1","true","yes")
# Activo por defecto para demo. La cuenta se fuerza a PRACTICE.
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
                time.sleep(delay+1)
                continue
            r.raise_for_status()
            payload=r.json()
            if not payload.get("ok"): raise RuntimeError(f"Telegram: {payload}")
            last_tg_sent=time.monotonic()
            return True
        except requests.RequestException as e:
            logging.warning("Telegram intento %d/%d: %s",attempt+1,retries,e)
            time.sleep(min(2**attempt,20))
    logging.error("Telegram no aceptó el mensaje.")
    return False

def split_messages(lines, max_chars=3000):
    result=[]; current=""
    for line in lines:
        add=("\n\n" if current else "")+line
        if current and len(current)+len(add)>max_chars:
            result.append(current); current=line
        else: current+=add
    if current: result.append(current)
    return result

def connect_iq():
    if not IQ_EMAIL or not IQ_PASSWORD: raise RuntimeError("Faltan IQ_EMAIL/IQ_PASSWORD.")
    iq=IQ_Option(IQ_EMAIL,IQ_PASSWORD)
    ok, reason=iq.connect()
    if not ok: raise RuntimeError(f"No conecta IQ Option: {reason}")
    iq.change_balance("PRACTICE")
    time.sleep(1)
    logging.info("Conectado a IQ Option; balance PRACTICE solicitado.")
    return iq

def server_time(iq):
    try:
        value=iq.get_server_timestamp()
        return float(value) if value else time.time()
    except Exception: return time.time()

def get_closed_candles(iq,count=HISTORY_COUNT):
    now=server_time(iq)
    raw=iq.get_candles(PAIR,TIMEFRAME,count+30,int(now)) or []
    # Solo incluir velas cuyo minuto completo terminó según reloj del servidor.
    current_start=int(now//TIMEFRAME)*TIMEFRAME
    closed=[]
    for c in raw:
        try:
            ts=int(c.get("from",c.get("at",0)))
            if ts>0 and ts+TIMEFRAME<=current_start:
                closed.append(c)
        except (TypeError,ValueError): pass
    return normalize_candles(closed)[-count:]

def send_history(candles):
    report=describe_history(candles)
    telegram_send(f"📊 HISTORIAL M1 | {PAIR}\nVelas cerradas: {report['count']}/{HISTORY_COUNT}\n"
                  f"Verdes: {report['greens']} | Rojas: {report['reds']} | Doji: {report['dojis']}\n"
                  f"Máximo: {report['highest']} | Mínimo: {report['lowest']}\n"
                  f"Cambio neto: {report['net_change']:+.6f}")
    details=[format_candle(i,c) for i,c in enumerate(candles,1)]
    chunks=split_messages(details)
    for i,chunk in enumerate(chunks,1):
        ok=telegram_send(f"🕯️ {PAIR} | Bloque {i}/{len(chunks)}\n\n{chunk}")
        if not ok: logging.error("Falló bloque %d/%d del historial",i,len(chunks))
    return True

def place_binary(iq,direction):
    if not ENABLE_TRADES: return False,"ENABLE_TRADES=false"
    iq.change_balance("PRACTICE")
    # iqoptionapi espera 'call' o 'put', monto, activo y expiración en minutos.
    return iq.buy(AMOUNT,PAIR,direction.lower(),EXPIRATION_MINUTES)

def main():
    iq=connect_iq()
    telegram_send(f"🟢 Bot M1 iniciado\nPar: {PAIR}\nCuenta: PRACTICE\n"
                  f"Velas de análisis: {HISTORY_COUNT}\nEntradas demo: {'ON' if ENABLE_TRADES else 'OFF'}")
    history_sent=False; last_candle=None; last_direction=None; trades=[]
    while True:
        try:
            candles=get_closed_candles(iq)
            if len(candles)<HISTORY_COUNT:
                logging.info("Velas disponibles %d/%d",len(candles),HISTORY_COUNT)
                time.sleep(POLL_SECONDS); continue
            if SEND_HISTORY and not history_sent:
                send_history(candles); history_sent=True
            closed_ts=candles[-1]["timestamp"]
            if closed_ts==last_candle:
                time.sleep(POLL_SECONDS); continue
            last_candle=closed_ts
            result=analyze_market(candles)
            signal=result.get("signal","WAIT")
            logging.info("Análisis %s señal=%s sesgo=%s CALL=%s PUT=%s: %s",
                         PAIR,signal,result.get("bias"),result.get("call_score"),
                         result.get("put_score"),result.get("reason"))
            telegram_send(f"🔎 Análisis M1 {PAIR}\nSeñal: {signal}\nSesgo: {result.get('bias')}\n"
                          f"Puntaje CALL/PUT: {result.get('call_score',0)}/{result.get('put_score',0)}\n"
                          f"Motivo: {result.get('reason')}")
            if signal not in ("CALL","PUT"): continue
            # Evita repetición inmediata de dirección en el mismo par.
            if signal==last_direction:
                logging.info("Señal repetida omitida: %s",signal); continue
            now=server_time(iq)
            # La vela señalada acaba de cerrar; solo entrar durante los primeros segundos
            # de la vela siguiente. Si la consulta llegó tarde, se omite explícitamente.
            elapsed=now-(closed_ts+TIMEFRAME)
            if elapsed<MIN_ENTRY_SECOND or elapsed>MAX_ENTRY_SECOND:
                msg=f"Señal {signal} omitida: fuera de ventana (segundo {elapsed:.1f})."
                logging.info(msg); telegram_send(msg); continue
            cutoff=time.time()-3600
            trades=[t for t in trades if t>cutoff]
            if len(trades)>=MAX_TRADES_PER_HOUR:
                telegram_send("Límite horario de operaciones demo alcanzado."); continue
            ok, order_id=place_binary(iq,signal)
            if ok:
                trades.append(time.time()); last_direction=signal
                telegram_send(f"🧪 Orden aceptada en PRACTICE\n{PAIR} | {signal}\nExpiración: 1 min\nID: {order_id}")
                logging.info("Orden aceptada: %s %s id=%s",PAIR,signal,order_id)
            else:
                telegram_send(f"⚠️ Orden rechazada: {signal} | respuesta: {order_id}")
                logging.warning("Orden rechazada: %s",order_id)
        except Exception as e:
            logging.exception("Error en ciclo")
            telegram_send(f"⚠️ Error: {type(e).__name__}: {e}")
            try:
                if not iq.check_connect():
                    logging.warning("Reconectando IQ Option...")
                    iq.connect(); iq.change_balance("PRACTICE")
            except Exception: pass
            time.sleep(3)
        time.sleep(POLL_SECONDS)

if __name__=="__main__": main()
