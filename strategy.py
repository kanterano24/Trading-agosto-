"""QUANT MODE: filtros de precio, contexto y secuencia sobre velas M1 cerradas."""
from statistics import median

CONTEXTS=(2,3,5,10)
STAGES=('impulso','respuesta','desaceleracion','recuperacion','continuacion','confirmacion')

def normalize_candles(raw):
    out=[]
    for c in raw or []:
        try:
            ts=int(c.get('from',c.get('at',c.get('timestamp',0))))
            o=float(c['open']); cl=float(c['close']); lo=float(c.get('min',c.get('low',min(o,cl)))); hi=float(c.get('max',c.get('high',max(o,cl))))
            if ts>0 and hi>=max(o,cl) and lo<=min(o,cl): out.append({'timestamp':ts,'open':o,'close':cl,'min':lo,'max':hi,'high':hi,'low':lo,'color':'green' if cl>o else 'red' if cl<o else 'doji'})
        except (KeyError,TypeError,ValueError): continue
    dedup={c['timestamp']:c for c in sorted(out,key=lambda x:x['timestamp'])}
    return list(dedup.values())

def describe_history(raw):
    c=normalize_candles(raw); g=sum(x['color']=='green' for x in c); r=sum(x['color']=='red' for x in c)
    return {'count':len(c),'greens':g,'reds':r,'dojis':len(c)-g-r}

def format_candle(i,c):
    x=normalize_candles([c])
    if not x:return f'{i}: inválida'
    a=x[0]; return f"{i}: {'V' if a['color']=='green' else 'R' if a['color']=='red' else 'D'} O={a['open']} C={a['close']} H={a['high']} L={a['low']}"

def _bias(c):
    if len(c)<2:return 'NEUTRAL'
    net=c[-1]['close']-c[0]['open']; up=sum(max(x['close']-x['open'],0) for x in c); dn=sum(max(x['open']-x['close'],0) for x in c)
    if net>0 and up>dn*1.05:return 'CALL'
    if net<0 and dn>up*1.05:return 'PUT'
    return 'NEUTRAL'

def _stages(c,d):
    result={k:False for k in STAGES}
    if len(c)<10 or d not in ('CALL','PUT'):return result
    w=c[-10:]; sign=1 if d=='CALL' else -1
    m=[(x['close']-x['open'])*sign for x in w]; b=[abs(x['close']-x['open']) for x in w]
    # Secuencia interpretable: tramo impulsivo, retroceso, pérdida de fuerza y recuperación.
    impulse=sum(m[-8:-5])>0 and sum(v>0 for v in m[-8:-5])>=2
    response=sum(m[-5:-3])<0
    decel=b[-3] <= max(b[-5:-3])*1.20
    recovery=m[-2]>0
    continuation=m[-1]>0 and w[-1]['close']>w[-2]['close'] if d=='CALL' else m[-1]>0 and w[-1]['close']<w[-2]['close']
    # Confirmación adicional: cierre a favor y no doji.
    confirm=m[-1]>0 and b[-1]/max(w[-1]['high']-w[-1]['low'],1e-12)>=0.35
    vals=(impulse,response,decel,recovery,continuation,confirm)
    return dict(zip(STAGES,vals))

def analyze_market(raw):
    c=normalize_candles(raw); contexts={n:{'bias':_bias(c[-n:])} for n in CONTEXTS}
    votes=[contexts[n]['bias'] for n in CONTEXTS]
    direction=votes[0] if votes[0] in ('CALL','PUT') and all(v==votes[0] for v in votes) else 'NEUTRAL'
    stages=_stages(c,direction)
    # Filtros de mercado: evitar vela extrema, indecisión y entrada pegada al extremo contrario.
    reason=[]; valid=len(c)>=30
    if not valid: reason.append(f'historial insuficiente {len(c)}/30')
    if direction=='NEUTRAL': reason.append('contextos 2/3/5/10 sin alineación')
    if valid and direction in ('CALL','PUT'):
        last=c[-1]; rng=max(last['high']-last['low'],1e-12); body=abs(last['close']-last['open']); ratio=body/rng
        recent=[max(x['high']-x['low'],0) for x in c[-11:-1]]; typical=median(recent) if recent else 0
        if ratio<0.30: reason.append('vela de indecisión')
        if typical and rng>2.8*typical: reason.append('vela extendida: posible agotamiento')
        # Evita entrar si el precio está pegado al nivel reciente opuesto.
        levels=c[-21:-1]; opp=max(x['high'] for x in levels) if direction=='CALL' else min(x['low'] for x in levels)
        room=(opp-last['close']) if direction=='CALL' else (last['close']-opp)
        if typical and room<0.35*typical: reason.append('poco espacio hasta nivel contrario')
    complete=valid and direction in ('CALL','PUT') and all(stages.values()) and not reason
    signal=direction if complete else 'NO SIGNAL'
    if complete: why='secuencia completa confirmada; contextos alineados y filtros de mercado aprobados'
    else: why='; '.join(reason+[f'etapas pendientes: {", ".join(k for k,v in stages.items() if not v)}'] if reason or not all(stages.values()) else reason)
    return {'signal':signal,'contexts':contexts,'stages':stages,'bias':direction,'call_score':sum(v=='CALL' for v in votes)+sum(stages.values()) if direction=='CALL' else 0,'put_score':sum(v=='PUT' for v in votes)+sum(stages.values()) if direction=='PUT' else 0,'reason':why}
