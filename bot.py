from __future__ import annotations
import logging,os,threading,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from typing import Any,Dict,Optional,Tuple
import pandas as pd,requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code
from strategy import analyze_market

def _binary_only_digital_underlying(self):return {'underlying':[]}
def _disabled_digital_open(self,*args,**kwargs):return None
setattr(IQ_Option,'get_digital_underlying_list_data',_binary_only_digital_underlying)
for n in ('_IQ_Option__get_digital_open','__get_digital_open','_get_digital_open'):
    if hasattr(IQ_Option,n):setattr(IQ_Option,n,_disabled_digital_open)

IQ_EMAIL=os.getenv('IQ_EMAIL');IQ_PASSWORD=os.getenv('IQ_PASSWORD');TELEGRAM_TOKEN=os.getenv('TELEGRAM_TOKEN');TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID')
M1=60;M5=300;M15=900
EXP={'M1_M1':1,'M5_M1':5,'M5_M5':5,'M15_M5':int(os.getenv('EXPIRATION_M15_M5','10'))}
MODES={'M1_M1':(M1,M1),'M5_M1':(M5,M1),'M5_M5':(M5,M5),'M15_M5':(M15,M5)}
LABEL={'M1_M1':'M1→M1','M5_M1':'M5→M1','M5_M5':'M5→M5','M15_M5':'M15→M5'}
AMOUNT=float(os.getenv('AMOUNT','500'));CANDLES=int(os.getenv('CANDLE_COUNT_M1','300'));WORKERS=8;REFRESH=600.;COOLDOWN=60.;MIN_SCORE=int(os.getenv('MIN_SCORE_TO_TRADE','60'))
PAIRS=[];LAST_REFRESH=0.;LAST_EVENT={};LAST_TRADE_ENTRY=-1;LAST_TRADE_TIME=0.;BOT_RUNNING=False;IQ:Optional[IQ_Option]=None
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s');logger=logging.getLogger(__name__)

def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    try:requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',data={'chat_id':TELEGRAM_CHAT_ID,'text':msg},timeout=3)
    except Exception:pass

def telegram_loop():
    global BOT_RUNNING
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    off=None;url=f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates'
    while True:
        try:
            p={'timeout':1};
            if off is not None:p['offset']=off+1
            data=requests.get(url,params=p,timeout=3).json()
            for u in data.get('result',[]):
                off=u.get('update_id',off);m=u.get('message') or {};cid=str((m.get('chat') or {}).get('id',''))
                if cid!=str(TELEGRAM_CHAT_ID):continue
                t=str(m.get('text','')).strip().lower()
                if t=='/start':BOT_RUNNING=True;tg('🟢 BOT ACTIVADO\n\nM1→M1 1m\nM5→M1 5m\nM5→M5 5m\nM15→M5 10m\n\nSolo una entrada por evento.')
                elif t=='/stop':BOT_RUNNING=False;tg('🔴 BOT DETENIDO')
                elif t=='/status':tg(f"📊 ESTADO\n\n{'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\nOTC: {len(PAIRS)}\nImporte: {AMOUNT:g}")
        except Exception:time.sleep(1)

def is_otc(x):
    n=str(x).upper();return n.endswith('-OTC') or n.endswith('_OTC') or 'OTC' in n

def refresh_pairs(force=False):
    global PAIRS,LAST_REFRESH
    if not IQ:return []
    now=time.time()
    if not force and now-LAST_REFRESH<REFRESH:return PAIRS
    try:data=IQ.get_all_init_v2();b=data.get('binary',{}) if isinstance(data,dict) else {};acts=b.get('actives',{}) if isinstance(b,dict) else {}
    except Exception as e:logger.warning('Catalogo OTC: %s',e);return PAIRS
    out=[]
    for aid,info in acts.items():
        if not isinstance(info,dict) or not isinstance(info.get('name'),str):continue
        name=info['name'].split('.',1)[-1].strip()
        if not is_otc(name) or info.get('enabled',True) is False or info.get('is_suspended',info.get('suspended',False)):continue
        try:OP_code.ACTIVES[name]=int(aid);out.append(name)
        except Exception:continue
    if out:PAIRS=sorted(set(out));LAST_REFRESH=now;logger.info('OTC disponibles: %d',len(PAIRS))
    return PAIRS

def server_ts():
    try:return float(IQ.get_server_timestamp()) if IQ else time.time()
    except Exception:return time.time()
def floor(ts,tf):return int(ts//tf)*tf

def connect():
    global IQ
    IQ=IQ_Option(IQ_EMAIL,IQ_PASSWORD);ok,reason=IQ.connect()
    if not ok:raise ConnectionError(reason)
    refresh_pairs(True);logger.info('IQ conectado | server=%.3f',server_ts());tg('🟢 IQ OPTION CONECTADO\n\nMulti-temporalidad activa.')

def ensure():
    if IQ is None:return False
    try:
        if IQ.check_connect():return True
    except Exception:pass
    try:return bool(IQ.connect()[0])
    except Exception:return False

def get_m1(pair):
    try:
        c=IQ.get_candles(pair,M1,CANDLES,server_ts());d=pd.DataFrame(c).rename(columns={'max':'high','min':'low'});req=['from','open','high','low','close']
        if d.empty or any(x not in d for x in req):return None
        for x in req:d[x]=pd.to_numeric(d[x],errors='coerce')
        return d.dropna(subset=req).drop_duplicates('from').sort_values('from').reset_index(drop=True)
    except Exception:return None

def aggregate(m1,tf,closed):
    if m1 is None or m1.empty:return pd.DataFrame()
    d=m1.copy();d['from']=d['from'].astype(int);d=d[d['from']<=closed+tf-M1];d['block']=(d['from']//tf)*tf;out=[];n=tf//M1
    for b,g in d.groupby('block',sort=True):
        g=g.sort_values('from');expected=[int(b)+i*M1 for i in range(n)]
        if g['from'].astype(int).tolist()!=expected:continue
        out.append({'from':int(b),'open':float(g.iloc[0].open),'high':float(g.high.max()),'low':float(g.low.min()),'close':float(g.iloc[-1].close)})
    return pd.DataFrame(out).query('from<=@closed').sort_values('from').reset_index(drop=True) if out else pd.DataFrame()

def analyze_mode(pair,mode,event,m1):
    atf,etf=MODES[mode];closed=event-atf
    if atf==M1:d=m1[m1['from']<=closed].copy()
    else:d=aggregate(m1,atf,closed)
    if d.empty or d[d['from']==closed].empty:return None
    r=analyze_market(df=d,mode=mode,pair=pair);sig=r.get('signal');score=int(r.get('score',0))
    if sig not in ('call','put') or score<MIN_SCORE:return None
    return {'pair':pair,'mode':mode,'signal':sig,'score':score,'analysis_ts':int(closed),'entry_tf':etf,'entry_ts':int(event),'expiration':EXP[mode],'reason':r.get('reason',''),'analysis':r.get('analysis',{})}

def analyze_event(mode_event,event):
    modes=[m for m,(atf,_) in MODES.items() if atf==({'M1':M1,'M5':M5,'M15':M15}[mode_event])]
    candidates=[]
    def worker(pair):
        m1=get_m1(pair)
        if m1 is None:return []
        ans=[]
        for mode in modes:
            try:
                c=analyze_mode(pair,mode,event,m1)
                if c:ans.append(c)
            except Exception:logger.exception('Error %s %s',pair,mode)
        return ans
    with ThreadPoolExecutor(max_workers=max(1,min(WORKERS,len(PAIRS)))) as ex:
        fs={ex.submit(worker,p):p for p in PAIRS}
        for f in as_completed(fs):
            try:candidates+=f.result()
            except Exception:pass
    if not candidates:return None
    for c in candidates:c['confluence']=sum(1 for x in candidates if x['pair']==c['pair'] and x['signal']==c['signal'])-1
    return max(candidates,key=lambda c:(c['score'],c['confluence'],1 if c['analysis'].get('support_rejection') or c['analysis'].get('resistance_rejection') else 0,1 if c['analysis'].get('pullback') else 0))

def buy(c):
    try:return IQ.buy(AMOUNT,c['pair'],c['signal'],int(c['expiration']))
    except Exception as e:logger.error('buy: %s',e);return False,None

def execute(c):
    global LAST_TRADE_ENTRY,LAST_TRADE_TIME
    now=server_ts();entry=floor(now,c['entry_tf'])
    if entry<c['entry_ts'] or entry==LAST_TRADE_ENTRY or time.time()-LAST_TRADE_TIME<COOLDOWN:return False
    res=buy(c);ok=bool(res[0]) if isinstance(res,tuple) else res not in (False,None,'error',-1);oid=res[1] if isinstance(res,tuple) and len(res)>1 else res
    if not ok:tg(f"❌ ORDEN RECHAZADA\n\n{c['pair']} | {LABEL[c['mode']]} | {c['signal'].upper()} | {c['expiration']} min");return False
    LAST_TRADE_ENTRY=entry;LAST_TRADE_TIME=time.time();delay=max(0,now-c['entry_ts']);tg(f"⚡ ENTRADA EJECUTADA\n\nPar: {c['pair']}\nModo: {LABEL[c['mode']]}\nDirección: {c['signal'].upper()}\nScore: {c['score']}/100\nExpiración: {c['expiration']} min\nRetraso: {delay:.2f}s\nID: {oid}");return True

def process():
    refresh_pairs()
    if not PAIRS:return
    now=server_ts();m1open=floor(now,M1);events=[('M1',m1open)]
    if m1open%M5==0:events.append(('M5',m1open))
    if m1open%M15==0:events.append(('M15',m1open))
    for tf,ts in events:
        if LAST_EVENT.get(tf)==ts:continue
        LAST_EVENT[tf]=ts;start=time.time();c=analyze_event(tf,ts);elapsed=time.time()-start
        if not c:continue
        cur=server_ts();entry=floor(cur,c['entry_tf'])
        if entry>c['entry_ts']:tg(f"⚠️ ANÁLISIS TERMINÓ TARDE\n\nPar: {c['pair']}\nModo: {LABEL[c['mode']]}\nTiempo: {elapsed:.2f}s\nSe ejecutará en la vela actual.")
        execute(c)

def main():
    global BOT_RUNNING
    if not all((IQ_EMAIL,IQ_PASSWORD,TELEGRAM_TOKEN,TELEGRAM_CHAT_ID)):
        logger.error('Faltan variables IQ_EMAIL/IQ_PASSWORD/TELEGRAM_TOKEN/TELEGRAM_CHAT_ID');return
    threading.Thread(target=telegram_loop,daemon=True).start()
    try:connect()
    except Exception as e:logger.exception('No se pudo iniciar IQ Option');tg(f'❌ ERROR DE CONEXIÓN\n\n{e}');return
    tg('🤖 BOT LISTO\n\nM1→M1 1m\nM5→M1 5m\nM5→M5 5m\nM15→M5 10m\n\nUsa /start para activar.')
    while True:
        try:
            if not BOT_RUNNING:time.sleep(.25);continue
            if not ensure():time.sleep(1);continue
            process();time.sleep(.05)
        except KeyboardInterrupt:BOT_RUNNING=False;break
        except Exception as e:logger.exception('Error principal: %s',e);time.sleep(1)
if __name__=='__main__':main()
