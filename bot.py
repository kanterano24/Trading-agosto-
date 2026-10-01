from __future__ import annotations
"""Recolector M1 de UN SOLO PAR OTC. No opera y no usa indicadores/SR/rechazo."""
import logging,os,threading,time
from typing import Optional
import pandas as pd,requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code
from strategy import M1,WINDOW,normalize,candle_data,compare_window_to_next

IQ_EMAIL=os.getenv('IQ_EMAIL'); IQ_PASSWORD=os.getenv('IQ_PASSWORD'); TELEGRAM_TOKEN=os.getenv('TELEGRAM_TOKEN'); TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID')
ANALYSIS_PAIR=os.getenv('ANALYSIS_PAIR','').strip().upper()
CANDLE_COUNT_M1=int(os.getenv('CANDLE_COUNT_M1','120')); LOOP_SLEEP=float(os.getenv('LOOP_SLEEP','0.05')); PAIR_REFRESH_SECONDS=float(os.getenv('PAIR_REFRESH_SECONDS','600'))
IQ:Optional[IQ_Option]=None; PAIR:Optional[str]=None; STREAM_STARTED=False; STREAM_CACHE=pd.DataFrame(); LAST_REPORTED_CANDLE=0; PREVIOUS_10=[]; BOT_RUNNING=True
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s'); logger=logging.getLogger(__name__)

def _binary_only_digital_underlying(self):return {'underlying':[]}
def _disabled_digital_open(self,*args,**kwargs):return None
setattr(IQ_Option,'get_digital_underlying_list_data',_binary_only_digital_underlying)
for n in ('_IQ_Option__get_digital_open','__get_digital_open','_get_digital_open'):
    if hasattr(IQ_Option,n):setattr(IQ_Option,n,_disabled_digital_open)

def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:logger.info('TELEGRAM:\n%s',msg);return
    try:requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',data={'chat_id':TELEGRAM_CHAT_ID,'text':msg},timeout=5)
    except Exception as e:logger.warning('Telegram: %s',e)

def telegram_loop():
    global BOT_RUNNING
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    offset=None; url=f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates'
    while True:
        try:
            p={'timeout':1};
            if offset is not None:p['offset']=offset+1
            data=requests.get(url,params=p,timeout=3).json()
            for u in data.get('result',[]):
                offset=u.get('update_id',offset); m=u.get('message') or {}; chat=str((m.get('chat') or {}).get('id',''))
                if chat!=str(TELEGRAM_CHAT_ID):continue
                cmd=str(m.get('text','')).strip().lower()
                if cmd=='/start':BOT_RUNNING=True;tg(f'🟢 RECOLECTOR ACTIVADO\n\nPar: {PAIR}\nM1 | 10 velas\nSin indicadores\nSin S/R\nSin rechazo\nOperaciones DESACTIVADAS')
                elif cmd=='/stop':BOT_RUNNING=False;tg('🔴 RECOLECTOR DETENIDO')
                elif cmd=='/status':tg(f"📊 ESTADO\n\n{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\nPar: {PAIR}\nM1 | ventana 10\nOperaciones DESACTIVADAS")
        except Exception:time.sleep(1)

def is_otc(n):
    n=str(n).upper();return n.endswith('-OTC') or n.endswith('_OTC') or 'OTC' in n

def discover_otc_pairs():
    if IQ is None:return []
    try:
        x=IQ.get_all_init_v2(); b=x.get('binary',{}) if isinstance(x,dict) else {}; a=b.get('actives',{}) if isinstance(b,dict) else {}
    except Exception as e:logger.warning('Catalogo OTC: %s',e);return []
    found=[]
    for aid,info in a.items():
        if not isinstance(info,dict):continue
        name=info.get('name');
        if not isinstance(name,str):continue
        name=name.split('.',1)[-1].strip()
        if not is_otc(name) or info.get('enabled',True) is False or info.get('is_suspended',info.get('suspended',False)):continue
        try:OP_code.ACTIVES[name]=int(aid);found.append(name)
        except Exception:pass
    return sorted(set(found))

def select_pair():
    global PAIR
    otc=discover_otc_pairs()
    if not otc:return None
    if ANALYSIS_PAIR:
        if ANALYSIS_PAIR in otc:PAIR=ANALYSIS_PAIR;return PAIR
        logger.warning('ANALYSIS_PAIR=%s no disponible. OTC: %s',ANALYSIS_PAIR,', '.join(otc[:20]));return None
    PAIR=otc[0];return PAIR

def server_ts():
    try:return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:return time.time()
def floor_m1(ts):return int(ts//M1)*M1

def normalize_stream(raw):
    if not isinstance(raw,dict) or not raw:return pd.DataFrame()
    rows=[]
    for k,v in raw.items():
        if not isinstance(v,dict):continue
        x=dict(v)
        try:x['from']=int(float(x.get('from',k)))
        except Exception:continue
        rows.append(x)
    return normalize(pd.DataFrame(rows)) if rows else pd.DataFrame()

def update_stream_cache():
    global STREAM_CACHE
    if IQ is None or not PAIR:return STREAM_CACHE
    try:
        d=normalize_stream(IQ.get_realtime_candles(PAIR,M1))
        if not d.empty:STREAM_CACHE=d
    except Exception as e:logger.warning('Stream %s: %s',PAIR,e)
    return STREAM_CACHE

def ensure_stream():
    global STREAM_STARTED
    if IQ is None or not PAIR:return False
    if STREAM_STARTED:return True
    try:IQ.start_candles_stream(PAIR,M1,CANDLE_COUNT_M1);STREAM_STARTED=True;logger.info('Stream M1 iniciado: %s',PAIR);return True
    except Exception as e:logger.warning('No se pudo iniciar stream %s: %s',PAIR,e);return False

def fmt_candle(c):
    return (f"🕯️ VELA M1 CERRADA\n\nPar: {PAIR}\nTimestamp: {c['timestamp']}\nColor: {c['color']}\n\nApertura: {c['open']:.8f}\nMecha inferior: {c['lower_wick']:.8f}\nCierre: {c['close']:.8f}\nMecha superior: {c['upper_wick']:.8f}\n\nSolo precio. Sin indicadores, S/R ni rechazo.\nOperaciones DESACTIVADAS.")

def fmt_window(w):
    seq=' '.join('G' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D' for c in w)
    s=[f'📊 ULTIMAS 10 VELAS\n\nPar: {PAIR}\nSecuencia: {seq}\n']
    for i,c in enumerate(w,1):s.append(f"{i:02d} {c['color'][0]} | O={c['open']:.8f} | MI={c['lower_wick']:.8f} | C={c['close']:.8f} | MS={c['upper_wick']:.8f}")
    return '\n'.join(s)

def fmt_next(r):
    return (f"🔎 VENTANA 10 → SIGUIENTE VELA\n\nPar: {PAIR}\n10 velas: {r['sequence']}\nSiguiente color real: {r['next_color']}\n\nApertura: {r['next_open']:.8f}\nMecha inferior: {r['next_lower_wick']:.8f}\nCierre: {r['next_close']:.8f}\nMecha superior: {r['next_upper_wick']:.8f}\n\nDato observado; todavia no se usa para operar.")

def process_closed_candles():
    global LAST_REPORTED_CANDLE,PREVIOUS_10
    if not BOT_RUNNING or STREAM_CACHE.empty:return
    latest=floor_m1(server_ts())-M1
    closed=STREAM_CACHE[STREAM_CACHE['from'].astype(int)<=latest].drop_duplicates('from').sort_values('from').reset_index(drop=True)
    if closed.empty:return
    if LAST_REPORTED_CANDLE==0:
        LAST_REPORTED_CANDLE=int(closed.iloc[-1]['from'])
        h=closed.tail(WINDOW)
        if len(h)==WINDOW:PREVIOUS_10=[candle_data(r) for _,r in h.iterrows()]
        return
    pending=closed[closed['from'].astype(int)>LAST_REPORTED_CANDLE]
    for _,row in pending.iterrows():
        c=candle_data(row)
        if len(PREVIOUS_10)==WINDOW:
            r=compare_window_to_next(PREVIOUS_10,c)
            if r.get('ready'):tg(fmt_next(r))
        tg(fmt_candle(c))
        h=closed[closed['from'].astype(int)<=c['timestamp']].tail(WINDOW)
        w=[candle_data(r) for _,r in h.iterrows()]
        if len(w)==WINDOW:PREVIOUS_10=w;tg(fmt_window(w))
        LAST_REPORTED_CANDLE=c['timestamp']
        logger.info('Cierre %s | %s | O=%.8f MI=%.8f C=%.8f MS=%.8f',PAIR,c['color'],c['open'],c['lower_wick'],c['close'],c['upper_wick'])

def connect():
    global IQ
    IQ=IQ_Option(IQ_EMAIL,IQ_PASSWORD);ok,reason=IQ.connect()
    if not ok:raise ConnectionError(reason)
    if not select_pair():raise RuntimeError('No se pudo seleccionar el par OTC.')
    tg(f'🟢 RECOLECTOR CONECTADO\n\nPar unico: {PAIR}\nM1 | ventana 10\nSin indicadores\nSin S/R\nSin rechazo\nOperaciones DESACTIVADAS\n\nSe enviara cada vela cerrada con apertura, mecha inferior, cierre y mecha superior.')

def ensure_connection():
    global STREAM_STARTED,STREAM_CACHE
    if IQ is None:return False
    try:
        if IQ.check_connect():return True
    except Exception:pass
    try:
        ok=bool(IQ.connect()[0])
        if ok:STREAM_STARTED=False;STREAM_CACHE=pd.DataFrame()
        return ok
    except Exception:return False

def main():
    global PAIR,STREAM_STARTED,STREAM_CACHE
    if not IQ_EMAIL or not IQ_PASSWORD:raise RuntimeError('Faltan IQ_EMAIL / IQ_PASSWORD.')
    connect()
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:threading.Thread(target=telegram_loop,daemon=True).start()
    last_refresh=time.time()
    while True:
        try:
            if not ensure_connection():time.sleep(2);continue
            if PAIR is None or time.time()-last_refresh>=PAIR_REFRESH_SECONDS:
                old=PAIR;new=select_pair()
                if new!=old:STREAM_STARTED=False;STREAM_CACHE=pd.DataFrame()
                last_refresh=time.time()
            if not PAIR:time.sleep(2);continue
            if not ensure_stream():time.sleep(1);continue
            update_stream_cache();process_closed_candles();time.sleep(LOOP_SLEEP)
        except KeyboardInterrupt:break
        except Exception:logger.exception('Error en ciclo principal');time.sleep(1)

if __name__=='__main__':main()
