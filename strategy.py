"""Secuencia de acción del precio para EURUSD-OTC M1. Solo velas cerradas."""
from statistics import mean

def normalize_candles(raw):
    out=[]
    for c in raw or []:
        try:
            t=int(c.get('timestamp',c.get('from',c.get('at',0))))
            o=float(c.get('open',c.get('o'))); cl=float(c.get('close',c.get('c')))
            h=float(c.get('max',c.get('high',c.get('h')))); l=float(c.get('min',c.get('low',c.get('l'))))
            if t>0 and h>=max(o,cl) and l<=min(o,cl): out.append({'timestamp':t,'open':o,'high':h,'low':l,'close':cl})
        except (TypeError,ValueError): pass
    return sorted({c['timestamp']:c for c in out}.values(),key=lambda x:x['timestamp'])

def anatomy(c):
    r=max(c['high']-c['low'],1e-12); b=abs(c['close']-c['open'])
    return {'range':r,'body':b,'body_ratio':b/r,'direction':(c['close']>c['open'])-(c['close']<c['open']),
            'upper_wick':(c['high']-max(c['open'],c['close']))/r,'lower_wick':(min(c['open'],c['close'])-c['low'])/r,
            'close_position':(c['close']-c['low'])/r}

def context(cs,n):
    w=cs[-n:]; net=w[-1]['close']-w[0]['open']; dirs=[anatomy(c)['direction'] for c in w]
    up=sum(x>0 for x in dirs); down=sum(x<0 for x in dirs)
    bias='CALL' if net>0 and up>=((n+1)//2) else 'PUT' if net<0 and down>=((n+1)//2) else 'NEUTRAL'
    return {'bias':bias,'net':net,'up':up,'down':down}

def _sequence(cs,side):
    s=1 if side=='CALL' else -1; a=[anatomy(c) for c in cs[-12:]]; w=cs[-12:]
    impulses=[]
    for i in range(7):
        baseline=mean([x['range'] for x in a[max(0,i-3):i]]) if i else a[i]['range']
        if a[i]['direction']==s and a[i]['body_ratio']>=.58 and a[i]['range']>=.9*baseline: impulses.append(i)
    if not impulses: return 'NO SIGNAL','sin impulso claro',{'impulso':False}
    i=impulses[-1]
    j=next((k for k in range(i+1,min(i+5,10)) if a[k]['direction']==-s),None)
    if j is None: return 'NO SIGNAL','falta respuesta/retroceso',{'impulso':True,'respuesta':False}
    decel=a[j]['body']<=a[i]['body']*.9
    if not decel: return 'NO SIGNAL','respuesta sin desaceleración',{'impulso':True,'respuesta':True,'desaceleracion':False}
    k=next((x for x in range(j+1,11) if a[x]['direction']==s and (w[x]['close']>w[j]['high'] if s==1 else w[x]['close']<w[j]['low'])),None)
    if k is None: return 'NO SIGNAL','sin recuperación',{'impulso':True,'respuesta':True,'desaceleracion':True,'recuperacion':False}
    last,prev=w[-1],w[-2]
    cont=a[-1]['direction']==s and (last['close']>prev['high'] if s==1 else last['close']<prev['low'])
    conf=a[-1]['body_ratio']>=.45 and (a[-1]['close_position']>=.70 if s==1 else a[-1]['close_position']<=.30)
    stages={'impulso':True,'respuesta':True,'desaceleracion':True,'recuperacion':True,'continuacion':cont,'confirmacion':conf}
    return (side,'secuencia completa confirmada',stages) if cont and conf else ('NO SIGNAL','falta continuación o confirmación',stages)

def analyze_market(candles):
    cs=normalize_candles(candles)
    if len(cs)<12: return {'signal':'NO SIGNAL','bias':'NEUTRAL','call_score':0,'put_score':0,'reason':'historial insuficiente','stages':{},'contexts':{}}
    ctx={n:context(cs,n) for n in (2,3,5,10)}
    nc=sum(v['bias']=='CALL' for v in ctx.values()); np=sum(v['bias']=='PUT' for v in ctx.values())
    side='CALL' if nc>=3 and ctx[5]['bias']=='CALL' or nc>=3 and ctx[10]['bias']=='CALL' else 'PUT' if np>=3 and (ctx[5]['bias']=='PUT' or ctx[10]['bias']=='PUT') else None
    signal,reason,stages=_sequence(cs,side) if side else ('NO SIGNAL','contextos 2/3/5/10 desalineados',{})
    return {'signal':signal,'bias':side or 'NEUTRAL','call_score':nc,'put_score':np,'reason':reason,'stages':stages,'contexts':ctx}

def describe_history(cs):
    cs=normalize_candles(cs); g=sum(c['close']>c['open'] for c in cs); r=sum(c['close']<c['open'] for c in cs)
    return {'count':len(cs),'greens':g,'reds':r,'dojis':len(cs)-g-r,'highest':max((c['high'] for c in cs),default=0),'lowest':min((c['low'] for c in cs),default=0),'net_change':cs[-1]['close']-cs[0]['open'] if cs else 0}

def format_candle(i,c):
    a=anatomy(c); color='VERDE' if a['direction']>0 else 'ROJA' if a['direction']<0 else 'DOJI'
    return f"{i:03d} | {color} | O:{c['open']:.5f} H:{c['high']:.5f} L:{c['low']:.5f} C:{c['close']:.5f} | cuerpo:{a['body_ratio']:.0%}"
