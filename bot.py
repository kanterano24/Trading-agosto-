"""Bot EURUSD-OTC M1: contexto 2/3/5/10 y secuencia de precio. PRACTICE solamente."""
import logging, os, time, requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, describe_history, format_candle, analyze_market
PAIRS = [
    "EURUSD-OTC",
    "EURJPY-OTC",
    "EURGBP-OTC",
    "GBPAUD-OTC",
    "GBPUSD-OTC",
    "USDCHF-OTC",
]
TF=60; COUNT=200; EXPIRATION=1
AMOUNT=float(os.getenv('AMOUNT','1000')); ENABLE_TRADES=os.getenv('ENABLE_TRADES','true').lower() in ('1','true','yes','si')
EMAIL=os.getenv('IQ_EMAIL',''); PASSWORD=os.getenv('IQ_PASSWORD',''); TOKEN=os.getenv('TELEGRAM_TOKEN',''); CHAT=os.getenv('TELEGRAM_CHAT_ID','')
POLL=max(1,float(os.getenv('POLL_SECONDS','2'))); logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
sess=requests.Session(); tg_last=0
# Evita el hilo digital que en algunas versiones falla al indexar None.
def safe_underlying(self): return {'underlying':[]}
def no_digital_open(self): return None
IQ_Option.get_digital_underlying_list_data=safe_underlying
if hasattr(IQ_Option,'_get_digital_open'): IQ_Option._get_digital_open=no_digital_open

def tg(msg):
    global tg_last
    if not TOKEN or not CHAT: return False
    try:
        wait=1.2-(time.monotonic()-tg_last)
        if wait>0: time.sleep(wait)
        r=sess.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',data={'chat_id':CHAT,'text':msg},timeout=15); r.raise_for_status()
        if not r.json().get('ok'): raise RuntimeError(str(r.json()))
        tg_last=time.monotonic(); return True
    except Exception: logging.exception('Error Telegram'); return False

def connect():
    if not EMAIL or not PASSWORD: raise RuntimeError('Faltan IQ_EMAIL/IQ_PASSWORD en Railway')
    iq=IQ_Option(EMAIL,PASSWORD); ok,why=iq.connect()
    if not ok: raise RuntimeError(f'Conexión fallida: {why}')
    iq.change_balance('PRACTICE'); logging.info('Conectado a PRACTICE'); return iq

def connect_retry():
    delay=5
    while True:
        try: return connect()
        except Exception as e:
            logging.exception('No conectó; reintento en %ss',delay); tg(f'⚠️ Conexión fallida ({type(e).__name__}); reintento en {delay}s')
            time.sleep(delay); delay=min(delay*2,60)

def discover_pairs(iq):
    """Usa únicamente los seis pares OTC solicitados."""
    return PAIRS.copy()

def get_candles(iq, pair):
    try: now=int(iq.get_server_timestamp())
    except Exception: now=int(time.time())
    raw=iq.get_candles(pair,TF,COUNT+20,now) or []; minute=now-now%TF
    closed=[]
    for c in raw:
        try:
            start=int(c.get('from',c.get('at',0)))
            if start>0 and start+TF<=minute: closed.append(c)
        except (TypeError,ValueError): pass
    return normalize_candles(closed)[-COUNT:],now

def main():
    iq=connect_retry(); pairs=[]; last_candle={}; history_done=set(); last_error=0; refresh=0
    tg('🟢 Bot iniciado\nPares configurados: EURUSD-OTC, EURJPY-OTC, EURGBP-OTC, GBPAUD-OTC, GBPUSD-OTC, USDCHF-OTC\nM1; expiración 1 min\nCuenta PRACTICE\nOperaciones: '+('habilitadas' if ENABLE_TRADES else 'solo análisis'))
    while True:
        try:
            if not iq.check_connect():
                iq=connect_retry(); pairs=[]; refresh=0
            mono=time.monotonic()
            if not pairs or mono-refresh>=60:
                new=discover_pairs(iq)
                if new:
                    if new!=pairs:
                        pairs=new
                        logging.info('Pares OTC cargados=%d | %s',len(pairs),', '.join(pairs))
                        tg(f'🔄 Pares OTC cargados ({len(pairs)}):\n'+', '.join(pairs))
                    refresh=mono
                else:
                    logging.warning('No se detectaron pares OTC abiertos; se reintentará.')
                    time.sleep(POLL); continue
            for pair in pairs:
                try:
                    cs,now=get_candles(iq,pair)
                    if len(cs)<COUNT:
                        logging.info('%s esperando historial %d/%d',pair,len(cs),COUNT); continue
                    if pair not in history_done:
                        st=describe_history(cs)
                        tg(f'📊 {pair} historial M1: {st["count"]}/{COUNT}\nVerdes {st["greens"]} | Rojas {st["reds"]} | Doji {st["dojis"]}')
                        for x in range(0,len(cs),35):
                            tg(f'🕯️ {pair} velas {x+1}-{min(x+35,len(cs))}\n'+'\n'.join(format_candle(i+1,c) for i,c in enumerate(cs[x:x+35],x)))
                        history_done.add(pair)
                    stamp=cs[-1]['timestamp']
                    if stamp==last_candle.get(pair): continue
                    last_candle[pair]=stamp
                    res=analyze_market(cs); ctx=res['contexts']
                    stages=', '.join(k for k,v in res['stages'].items() if v) or 'ninguna'
                    ctxs=' | '.join(f'{n}:{ctx[n]["bias"]}' for n in (2,3,5,10))
                    logging.info('%s señal=%s sesgo=%s CALL=%s PUT=%s | %s | etapas=%s',pair,res['signal'],res['bias'],res['call_score'],res['put_score'],res['reason'],stages)
                    # Ejecutar inmediatamente al detectar la señal; sin esperar una ventana de segundos.
                    if res['signal'] in ('CALL','PUT') and ENABLE_TRADES:
                        iq.change_balance('PRACTICE')
                        inverse_signal = 'PUT' if res['signal'] == 'CALL' else 'CALL'
                        ok,oid=iq.buy(AMOUNT,pair,inverse_signal.lower(),EXPIRATION)
                        logging.info('%s señal=%s | orden invertida=%s | ok=%s | respuesta=%s',pair,res['signal'],inverse_signal,ok,oid)
                        tg(f'{"🧪 Orden enviada" if ok else "⚠️ Orden rechazada"}\n{pair} señal {res["signal"]} → orden {inverse_signal}\nID/respuesta: {oid}')
                    tg(f'📈 {pair} M1\nSeñal: {res["signal"]}\nContexto: {ctxs}\nEtapas: {stages}\nMotivo: {res["reason"]}')
                except Exception as e:
                    logging.exception('Error analizando %s',pair)
                    if time.monotonic()-last_error>60:
                        tg(f'⚠️ Error en {pair}: {type(e).__name__}: {e}'); last_error=time.monotonic()
        except Exception as e:
            logging.exception('Error de ciclo')
            if time.monotonic()-last_error>60:
                tg(f'⚠️ Error: {type(e).__name__}: {e}'); last_error=time.monotonic()
            try:
                if not iq.check_connect(): iq=connect_retry()
            except Exception: iq=connect_retry()
        time.sleep(POLL)

if __name__=='__main__':
    while True:
        try: main()
        except KeyboardInterrupt: break
        except Exception: logging.exception('Fallo principal; reinicio en 10s'); time.sleep(10)
