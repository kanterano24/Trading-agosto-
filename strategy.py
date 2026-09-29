from __future__ import annotations
from typing import Any, Dict, Optional, Tuple
import pandas as pd

MIN_BARS=30
SWING_LEFT=2
SWING_RIGHT=2
LOOKBACK=30
ZONE_TOLERANCE=0.0015
WICK_BODY_MIN=1.20
MODES={
    'M1_M1':('M1','M1',1),
    'M5_M1':('M5','M1',5),
    'M5_M5':('M5','M5',5),
    'M15_M5':('M15','M5',10),
}

def _empty(reason='sin señal',mode='M5_M1'):
    a,e,x=MODES.get(mode,('M5','M1',5))
    return {'signal':None,'direction':'range','trend':'range','reason':reason,'score':0,'blocked':True,'zone':'none','entry_type':f'PRICE_ACTION_{mode}','entry_quality':0,'mode':mode,'analysis_timeframe':a,'entry_timeframe':e,'target_expiration_minutes':x,'analysis':{}}

def _norm(df):
    if not isinstance(df,pd.DataFrame) or df.empty:return pd.DataFrame()
    d=df.copy().rename(columns={'max':'high','min':'low'})
    req=['open','high','low','close']
    if any(c not in d for c in req):return pd.DataFrame()
    for c in req:d[c]=pd.to_numeric(d[c],errors='coerce')
    if 'from' in d:
        d['from']=pd.to_numeric(d['from'],errors='coerce');d=d.sort_values('from')
    return d.dropna(subset=req).reset_index(drop=True)

def _dir(r):return 'bullish' if r.close>r.open else 'bearish' if r.close<r.open else 'neutral'

def _met(r):
    o,h,l,c=map(float,(r.open,r.high,r.low,r.close));rng=max(h-l,1e-12);body=abs(c-o);uw=h-max(o,c);lw=min(o,c)-l
    return {'high':h,'low':l,'close':c,'body_ratio':body/rng,'upper_body':uw/max(body,1e-12),'lower_body':lw/max(body,1e-12),'close_pos':(c-l)/rng}

def _structure(d):
    hs=[];ls=[];start=max(SWING_LEFT,len(d)-LOOKBACK-SWING_RIGHT);end=len(d)-SWING_RIGHT
    for i in range(start,end):
        h=float(d.iloc[i].high);l=float(d.iloc[i].low)
        if h>=float(d.iloc[i-SWING_LEFT:i].high.max()) and h>=float(d.iloc[i+1:i+1+SWING_RIGHT].high.max()):hs.append((i,h))
        if l<=float(d.iloc[i-SWING_LEFT:i].low.min()) and l<=float(d.iloc[i+1:i+1+SWING_RIGHT].low.min()):ls.append((i,l))
    s='range'
    if len(hs)>=2 and len(ls)>=2:
        if hs[-1][1]>hs[-2][1] and ls[-1][1]>ls[-2][1]:s='bullish'
        elif hs[-1][1]<hs[-2][1] and ls[-1][1]<ls[-2][1]:s='bearish'
    return {'structure':s,'last_high':hs[-1][1] if hs else None,'previous_high':hs[-2][1] if len(hs)>1 else None,'last_low':ls[-1][1] if ls else None,'previous_low':ls[-2][1] if len(ls)>1 else None}

def _candle(d,i):
    m=_met(d.iloc[i]);p=_met(d.iloc[i-1]) if i else m;dr=_dir(d.iloc[i]);pd_=_dir(d.iloc[i-1]) if i else 'neutral'
    doji=m['body_ratio']<=.10;ind=m['body_ratio']<=.25
    reversal=(dr=='bullish' and pd_=='bearish' and d.iloc[i].open<=d.iloc[i-1].close and d.iloc[i].close>=d.iloc[i-1].open) or (dr=='bearish' and pd_=='bullish' and d.iloc[i].open>=d.iloc[i-1].close and d.iloc[i].close<=d.iloc[i-1].open)
    mb=dr=='bullish' and m['body_ratio']>=.55 and m['close_pos']>=.70 and m['close']>p['high']
    ms=dr=='bearish' and m['body_ratio']>=.55 and m['close_pos']<=.30 and m['close']<p['low']
    cont=dr in ('bullish','bearish') and dr==pd_ and m['body_ratio']>=.35
    return {'direction':dr,'doji':doji,'indecision':ind,'reversal':reversal,'momentum_bull':mb,'momentum_bear':ms,'continuation':cont,'strength':m['body_ratio']>=.60,'rest':m['body_ratio']<=.35 and not doji,'shooting_star':m['upper_body']>=WICK_BODY_MIN and m['upper_body']>m['lower_body']*1.5 and m['close_pos']<=.55,'evening_star':False,'body_ratio':m['body_ratio'],'close_pos':m['close_pos']}

def _pullback(d,s):
    if s not in ('bullish','bearish') or len(d)<5:return False,0
    n=sum(1 for _,r in d.iloc[-5:-1].iterrows() if (_dir(r)=='bearish' if s=='bullish' else _dir(r)=='bullish'))
    return n>=1,n

def analyze_market(df:Optional[pd.DataFrame]=None,candle:Any=None,previous:Optional[pd.DataFrame]=None,pair:Optional[str]=None,mode='M5_M1',**kwargs):
    if mode not in MODES:mode='M5_M1'
    if df is not None:base=df.copy()
    elif previous is not None:
        base=previous.copy();
        if candle is not None:base=pd.concat([base,pd.DataFrame([candle])],ignore_index=True)
    else:base=pd.DataFrame()
    d=_norm(base);out=_empty(mode=mode)
    if len(d)<MIN_BARS:out['reason']=f'Historial insuficiente {len(d)}/{MIN_BARS}';return out
    i=len(d)-1;cur=d.iloc[i];st=_structure(d);s=st['structure'];p=_candle(d,i);pb,n=_pullback(d,s)
    support=st['last_low'] if st['last_low'] is not None else float(d.iloc[-20:].low.min());resistance=st['last_high'] if st['last_high'] is not None else float(d.iloc[-20:].high.max());m=_met(cur)
    ns=m['low']<=support*(1+ZONE_TOLERANCE) and m['close']>=support;nr=m['high']>=resistance*(1-ZONE_TOLERANCE) and m['close']<=resistance
    br=ns and m['lower_body']>=WICK_BODY_MIN and m['close_pos']>=.60;sr=nr and m['upper_body']>=WICK_BODY_MIN and m['close_pos']<=.40
    zone='support' if ns and not nr else 'resistance' if nr and not ns else 'ambiguous' if ns and nr else 'none'
    if zone=='ambiguous':return _blocked(d,st,p,zone,'Entrada bloqueada: S/R ambiguos',mode)
    if zone=='support' and p['direction']=='bearish':return _blocked(d,st,p,zone,'PUT bloqueada: precio en SOPORTE',mode)
    if zone=='resistance' and p['direction']=='bullish':return _blocked(d,st,p,zone,'CALL bloqueada: precio en RESISTENCIA',mode)
    call=(s=='bullish' and p['direction']=='bullish' and (br or (p['momentum_bull'] and pb)))
    put=(s=='bearish' and p['direction']=='bearish' and (sr or (p['momentum_bear'] and pb)))
    if p['doji'] or p['indecision']:call=put=False
    if call==put:return _blocked(d,st,p,zone,'sin señal: accion del precio ambigua',mode)
    score=min(100,(25 if s in ('bullish','bearish') else 0)+(15 if pb else 0)+(25 if br or sr else 0)+(20 if p['momentum_bull'] or p['momentum_bear'] else 0)+(10 if p['reversal'] or p['continuation'] else 0)+(5 if p['strength'] else 0))
    sig='call' if call else 'put';a,e,x=MODES[mode]
    return {'signal':sig,'direction':p['direction'],'trend':s,'reason':f"{sig.upper()} | estructura {s} | {'rechazo confirmado' if br or sr else 'momentum confirmado'} | {'pullback detectado' if pb else 'sin pullback claro'}",'score':score,'blocked':False,'zone':zone,'entry_type':f'PRICE_ACTION_{mode}','entry_quality':score,'mode':mode,'analysis_timeframe':a,'entry_timeframe':e,'target_expiration_minutes':x,'candle_timestamp':int(cur['from']) if 'from' in d and pd.notna(cur['from']) else None,'analysis':{'timeframe':a,'entry_timeframe':e,'expiration_minutes':x,'indicators_used':False,'structure':s,'support':support,'resistance':resistance,'zone':zone,'support_rejection':br,'resistance_rejection':sr,'pullback':pb,'reversal':p['reversal'],'continuation':p['continuation'],'strength':p['strength'],'call_momentum':p['momentum_bull'],'put_momentum':p['momentum_bear']}}

def _blocked(d,st,p,zone,reason,mode):
    a,e,x=MODES[mode];cur=d.iloc[-1]
    return {'signal':None,'direction':p['direction'],'trend':st['structure'],'reason':reason,'score':0,'blocked':True,'zone':zone,'entry_type':f'PRICE_ACTION_{mode}','entry_quality':0,'mode':mode,'analysis_timeframe':a,'entry_timeframe':e,'target_expiration_minutes':x,'candle_timestamp':int(cur['from']) if 'from' in d and pd.notna(cur['from']) else None,'analysis':{'timeframe':a,'entry_timeframe':e,'expiration_minutes':x,'indicators_used':False,'structure':st['structure'],'support':st['last_low'],'resistance':st['last_high']}}

def get_signal(df,mode='M5_M1'):return analyze_market(df=df,mode=mode).get('signal')
def signal(df,mode='M5_M1'):return get_signal(df,mode)
