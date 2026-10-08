"""Estrategia: estructura + RSI + agotamiento/reversion.
Solo velas M1 cerradas. Sin S/R ni rechazo.
PUT: estructura alcista + RSI sobrecomprado girando abajo + patron.
CALL: estructura bajista + RSI sobrevendido girando arriba + patron.
Objetivo de expiracion: 2 minutos.
"""

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
EXPIRATION = 2


def normalize_candles(raw):
    out = []
    for c in raw or []:
        try:
            ts = int(c.get("from", c.get("at", c.get("timestamp", 0))))
            o = float(c["open"]); cl = float(c["close"])
            lo = float(c.get("min", c.get("low", min(o, cl))))
            hi = float(c.get("max", c.get("high", max(o, cl))))
            if ts <= 0 or hi < max(o, cl) or lo > min(o, cl):
                continue
            body = abs(cl - o); rng = max(hi - lo, 0.0)
            out.append({
                "timestamp": ts, "open": o, "close": cl,
                "min": lo, "max": hi, "high": hi, "low": lo,
                "body": body, "range": rng,
                "lower_wick": min(o, cl) - lo,
                "upper_wick": hi - max(o, cl),
                "color": "green" if cl > o else "red" if cl < o else "doji",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return list({x["timestamp"]: x for x in sorted(out, key=lambda z: z["timestamp"])}.values())


def describe_history(raw):
    c = normalize_candles(raw)
    return {"count": len(c), "greens": sum(x["color"] == "green" for x in c),
            "reds": sum(x["color"] == "red" for x in c),
            "dojis": sum(x["color"] == "doji" for x in c)}


def format_candle(i, c):
    x = normalize_candles([c])
    if not x: return f"{i}: inválida"
    a = x[0]; color = "V" if a["color"] == "green" else "R" if a["color"] == "red" else "D"
    return (f"{i}: {color} O={a['open']} C={a['close']} H={a['high']} L={a['low']} "
            f"Rango={a['range']} Cuerpo={a['body']} MechaInf={a['lower_wick']} MechaSup={a['upper_wick']}")


def _rsi_values(candles, period=RSI_PERIOD):
    closes = [float(x["close"]) for x in candles]
    if len(closes) < period + 1: return []
    gains=[]; losses=[]
    for i in range(1, len(closes)):
        d=closes[i]-closes[i-1]; gains.append(max(d,0.0)); losses.append(max(-d,0.0))
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    values=[None]*period
    values.append(100.0 if al == 0 else 100.0-(100.0/(1.0+ag/al)))
    for i in range(period, len(gains)):
        ag=((ag*(period-1))+gains[i])/period
        al=((al*(period-1))+losses[i])/period
        values.append(100.0 if al == 0 else 100.0-(100.0/(1.0+ag/al)))
    return values


def _is_clear(c):
    return c["color"] != "doji" and c["range"] > 0 and c["body"] / c["range"] >= 0.50


def _bullish_structure(a,b,c):
    return b["high"]>a["high"] and c["high"]>b["high"] and b["low"]>a["low"] and c["low"]>b["low"]


def _bearish_structure(a,b,c):
    return b["high"]<a["high"] and c["high"]<b["high"] and b["low"]<a["low"] and c["low"]<b["low"]


def _three_green(a,b,c):
    return (a["color"]==b["color"]==c["color"]=="green" and _is_clear(a) and _is_clear(b) and _is_clear(c)
            and b["close"]>a["close"] and c["close"]>b["close"])


def _three_red(a,b,c):
    return (a["color"]==b["color"]==c["color"]=="red" and _is_clear(a) and _is_clear(b) and _is_clear(c)
            and b["close"]<a["close"] and c["close"]<b["close"])


def _bullish_exhaustion(second, third):
    return (third["range"]>0 and second["body"]>0 and third["body"]<second["body"]
            and third["upper_wick"]/third["range"]>=0.20)


def _bearish_exhaustion(second, third):
    return (third["range"]>0 and second["body"]>0 and third["body"]<second["body"]
            and third["lower_wick"]/third["range"]>=0.20)


def _rsi_confirmation(candles, direction):
    v=_rsi_values(candles)
    if len(v)<len(candles): return False,None,None
    previous,current=v[-2],v[-1]
    if previous is None or current is None: return False,current,previous
    if direction=="PUT": return previous>=RSI_OVERBOUGHT and current<previous,current,previous
    return previous<=RSI_OVERSOLD and current>previous,current,previous


def _signal(c):
    if len(c)<RSI_PERIOD+2: return "NEUTRAL",None,None
    a,b,t,r=c[-4],c[-3],c[-2],c[-1]
    put_rsi,rc,rp=_rsi_confirmation(c,"PUT")
    if (_bullish_structure(a,b,t) and _three_green(a,b,t)
        and _bullish_exhaustion(b,t) and r["color"]=="red" and _is_clear(r)
        and r["close"]<t["open"] and put_rsi):
        return "PUT",rc,rp
    call_rsi,rc,rp=_rsi_confirmation(c,"CALL")
    if (_bearish_structure(a,b,t) and _three_red(a,b,t)
        and _bearish_exhaustion(b,t) and r["color"]=="green" and _is_clear(r)
        and r["close"]>t["open"] and call_rsi):
        return "CALL",rc,rp
    return "NEUTRAL",rc,rp


def analyze_market(raw):
    c=normalize_candles(raw)
    if len(c)<RSI_PERIOD+2:
        return {"signal":"NO SIGNAL","bias":"NEUTRAL","call_score":0,"put_score":0,
                "expiration":EXPIRATION,"reason":f"historial insuficiente: {len(c)} velas"}
    signal,rc,rp=_signal(c)
    if signal=="PUT":
        reason=(f"PUT | estructura alcista + agotamiento/reversion + RSI sobrecompra girando abajo "
                f"({rp:.2f}->{rc:.2f}) | expiracion objetivo: {EXPIRATION} minutos")
    elif signal=="CALL":
        reason=(f"CALL | estructura bajista + agotamiento/reversion + RSI sobreventa girando arriba "
                f"({rp:.2f}->{rc:.2f}) | expiracion objetivo: {EXPIRATION} minutos")
    else:
        rsi="RSI no disponible" if rc is None else f"RSI {rp:.2f}->{rc:.2f}" if rp is not None else f"RSI actual {rc:.2f}"
        reason=("sin patron confirmado: se requiere estructura, agotamiento/reversion y RSI en zona extrema "
                f"mostrando cambio de direccion | {rsi}")
    return {"signal":signal if signal in ("CALL","PUT") else "NO SIGNAL",
            "bias":signal if signal in ("CALL","PUT") else "NEUTRAL",
            "call_score":1 if signal=="CALL" else 0,
            "put_score":1 if signal=="PUT" else 0,
            "expiration":EXPIRATION,"reason":reason}
