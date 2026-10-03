"""
strategy.py — estructura y acción del precio para GBPUSD-OTC M1.
Usa exclusivamente velas cerradas OHLC; no calcula indicadores ni ejecuta órdenes.
"""
from datetime import datetime, timezone

TIMEFRAME_SECONDS=60
MIN_CANDLES=30
DEFAULT_CONTEXT=200
SWING_LEFT=2
SWING_RIGHT=2

def normalize_candles(candles):
    out=[]; seen=set()
    for raw in candles or []:
        try:
            ts=int(float(raw.get("from",raw.get("timestamp",raw.get("at",0)))))
            o=float(raw["open"]); h=float(raw.get("max",raw.get("high")))
            lo=float(raw.get("min",raw.get("low"))); c=float(raw["close"])
            if ts<=0 or not all(map(lambda x: x==x and abs(x)!=float("inf"),(o,h,lo,c))): continue
            if h<max(o,lo,c) or lo>min(o,h,c) or ts in seen: continue
            seen.add(ts); out.append({"timestamp":ts,"open":o,"high":h,"low":lo,"close":c})
        except (KeyError,TypeError,ValueError): continue
    return sorted(out,key=lambda x:x["timestamp"])

def candle_anatomy(c):
    o,h,lo,cl=(float(c[k]) for k in ("open","high","low","close"))
    span=max(0.0,h-lo); body=abs(cl-o)
    upper=max(0.0,h-max(o,cl)); lower=max(0.0,min(o,cl)-lo)
    color="VERDE" if cl>o else "ROJA" if cl<o else "DOJI"
    return {**c,"color":color,"range":span,"body":body,
            "body_pct":body/span*100 if span else 0.0,
            "upper_wick":upper,"lower_wick":lower,
            "upper_wick_pct":upper/span*100 if span else 0.0,
            "lower_wick_pct":lower/span*100 if span else 0.0,
            "close_position_pct":(cl-lo)/span*100 if span else 50.0}

def describe_history(candles):
    items=[candle_anatomy(c) for c in normalize_candles(candles)]
    greens=sum(x["color"]=="VERDE" for x in items); reds=sum(x["color"]=="ROJA" for x in items)
    return {"candles":items,"count":len(items),"greens":greens,"reds":reds,
            "dojis":len(items)-greens-reds,
            "sequence":" ".join("V" if x["color"]=="VERDE" else "R" if x["color"]=="ROJA" else "D" for x in items),
            "highest":max((x["high"] for x in items),default=None),
            "lowest":min((x["low"] for x in items),default=None),
            "net_change":items[-1]["close"]-items[0]["open"] if items else 0.0}

def format_candle(index,candle):
    x=candle_anatomy(candle)
    dt=datetime.fromtimestamp(int(x["timestamp"]),tz=timezone.utc)
    return (f'{index:03d} | {dt:%Y-%m-%d %H:%M:%S} UTC | {x["color"]}\n'
            f'Open: {x["open"]:.6f} | High: {x["high"]:.6f} | Low: {x["low"]:.6f} | Close: {x["close"]:.6f}\n'
            f'Rango: {x["range"]:.6f} | Cuerpo: {x["body"]:.6f} ({x["body_pct"]:.1f}%)\n'
            f'Mecha superior: {x["upper_wick"]:.6f} ({x["upper_wick_pct"]:.1f}%) | '
            f'Mecha inferior: {x["lower_wick"]:.6f} ({x["lower_wick_pct"]:.1f}%)\n'
            f'Posición del cierre: {x["close_position_pct"]:.1f}%')

def _median(values):
    vals=sorted(values); n=len(vals)
    return 0.0 if not n else vals[n//2] if n%2 else (vals[n//2-1]+vals[n//2])/2

def _swings(cs):
    highs=[]; lows=[]
    for i in range(SWING_LEFT,len(cs)-SWING_RIGHT):
        h=cs[i]["high"]; lo=cs[i]["low"]
        left=cs[i-SWING_LEFT:i]; right=cs[i+1:i+1+SWING_RIGHT]
        if all(h>x["high"] for x in left+right): highs.append((i,h))
        if all(lo<x["low"] for x in left+right): lows.append((i,lo))
    return highs,lows

def _bias(cs):
    hi,lo=_swings(cs[-80:])
    if len(hi)<2 or len(lo)<2: return "NEUTRAL","Pivotes insuficientes"
    hh=hi[-1][1]>hi[-2][1]; hl=lo[-1][1]>lo[-2][1]
    lh=hi[-1][1]<hi[-2][1]; ll=lo[-1][1]<lo[-2][1]
    if hh and hl: return "ALCISTA","Máximos y mínimos ascendentes"
    if lh and ll: return "BAJISTA","Máximos y mínimos descendentes"
    return "NEUTRAL","Estructura mixta/lateral"

def analyze_market(raw):
    cs=normalize_candles(raw)
    if len(cs)<MIN_CANDLES:
        return {"signal":"WAIT","reason":f"Faltan velas: {len(cs)}/{MIN_CANDLES}",
                "bias":"NEUTRAL","candles_used":len(cs),"call_score":0,"put_score":0}
    ctx=cs[-min(DEFAULT_CONTEXT,len(cs)):]
    bias,structure=_bias(ctx); last=candle_anatomy(ctx[-1]); prev=ctx[-2]
    ranges=[c["high"]-c["low"] for c in ctx[-11:-1]]
    typical=_median(ranges); tol=max(typical*.15,1e-10)
    recent=ctx[-40:]; resistance=max(c["high"] for c in recent); support=min(c["low"] for c in recent)
    call_break=last["color"]=="VERDE" and last["close"]>prev["high"]
    put_break=last["color"]=="ROJA" and last["close"]<prev["low"]
    call_reject=(last["low"]<=support+tol and last["lower_wick"]>=max(last["body"]*.8,tol)
                 and last["close_position_pct"]>=60)
    put_reject=(last["high"]>=resistance-tol and last["upper_wick"]>=max(last["body"]*.8,tol)
                and last["close_position_pct"]<=40)
    call_score=2 if bias=="ALCISTA" else 0; put_score=2 if bias=="BAJISTA" else 0
    cr=[structure] if bias=="ALCISTA" else []; pr=[structure] if bias=="BAJISTA" else []
    if call_break: call_score+=2; cr.append("cierre verde sobre el máximo previo")
    if put_break: put_score+=2; pr.append("cierre rojo bajo el mínimo previo")
    if call_reject: call_score+=1; cr.append("rechazo comprador en soporte")
    if put_reject: put_score+=1; pr.append("rechazo vendedor en resistencia")
    if last["color"]=="VERDE" and last["body_pct"]>=50 and last["close_position_pct"]>=70:
        call_score+=1; cr.append("vela verde amplia con cierre alto")
    if last["color"]=="ROJA" and last["body_pct"]>=50 and last["close_position_pct"]<=30:
        put_score+=1; pr.append("vela roja amplia con cierre bajo")
    signal="WAIT"; reasons=[]
    if bias=="ALCISTA" and call_score>=4 and (call_break or call_reject):
        signal="CALL"; reasons=cr
    elif bias=="BAJISTA" and put_score>=4 and (put_break or put_reject):
        signal="PUT"; reasons=pr
    else:
        if bias=="NEUTRAL": reasons.append("estructura neutral")
        if not (call_break or put_break or call_reject or put_reject):
            reasons.append("falta ruptura confirmada o rechazo válido")
        if not reasons: reasons.append("confirmaciones insuficientes")
    return {"signal":signal,"reason":"; ".join(reasons),"bias":bias,
            "structure_reason":structure,"candles_used":len(ctx),"last_candle":last,
            "support":support,"resistance":resistance,"call_score":call_score,"put_score":put_score,
            "confidence_score":max(call_score,put_score),"bullish_break":call_break,
            "bearish_break":put_break,"bullish_rejection":call_reject,
            "bearish_rejection":put_reject,"setup":signal if signal!="WAIT" else None,
            "note":"Puntaje de filtros; no representa probabilidad de acierto."}
