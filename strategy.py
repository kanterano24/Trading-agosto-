"""
strategy.py
Binary M1: estructura + divergencia + zona estructural + 4.º/5.º toque
+ vela N de reversión. No ejecuta operaciones ni gestiona expiración.
"""

from __future__ import annotations
from typing import Any, Dict, Optional, Tuple
import math
import numpy as np
import pandas as pd

MIN_BARS = 35
MAX_CANDLES = 80
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
RSI_PERIOD, ATR_PERIOD = 14, 14

PIVOT_LEFT, PIVOT_RIGHT = 2, 2
SWING_LOOKBACK = 35
ZONE_ATR = 0.28
MAX_ENTRY_DISTANCE_ATR = 0.55
MIN_ROOM_TO_OPPOSITE_ATR = 0.70

MIN_BODY_RATIO = 0.25
MIN_REJECTION_WICK_RATIO = 0.35
MIN_WICK_BODY_RATIO = 1.15
MIN_CLOSE_POSITION_CALL = 0.62
MAX_CLOSE_POSITION_PUT = 0.38
MIN_BODY_ATR, MAX_BODY_ATR = 0.12, 1.35

# Divergencia / toques
DIVERGENCE_LOOKBACK = 25
DIVERGENCE_MIN_SEPARATION = 3
MIN_TOUCHES = 4
MAX_TOUCHES = 5
TOUCH_ZONE_ATR = 0.35
TOUCH_MIN_SEPARATION = 2

MIN_STRUCTURE_SCORE = 3
MIN_ENTRY_SCORE = 70
EPS = 1e-12


def _empty_result(reason="sin señal") -> Dict[str, Any]:
    return {
        "signal": None, "direction": "range", "trend": "range",
        "reason": reason, "score": 0, "continuity": False,
        "blocked": True, "zone": None, "entry_type": None,
        "entry_quality": 0, "last_swing_high": None,
        "last_swing_low": None, "support": None, "resistance": None,
        "rsi": 50.0, "atr": 0.0, "candle_timestamp": None,
        "analysis": {},
    }


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _normalize(df):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    ren = {}
    if "max" in out.columns and "high" not in out.columns: ren["max"] = "high"
    if "min" in out.columns and "low" not in out.columns: ren["min"] = "low"
    ren.update({"Open":"open","High":"high","Low":"low","Close":"close"})
    out.rename(columns=ren, inplace=True)
    req = ["open","high","low","close"]
    if any(c not in out.columns for c in req): return pd.DataFrame()
    for c in req: out[c] = pd.to_numeric(out[c], errors="coerce")
    if "from" in out.columns:
        out["from"] = pd.to_numeric(out["from"], errors="coerce")
        out.dropna(subset=["from"], inplace=True)
        out["from"] = out["from"].astype(int)
        out.sort_values("from", inplace=True)
        out.drop_duplicates("from", keep="last", inplace=True)
    out.dropna(subset=req, inplace=True)
    out.reset_index(drop=True, inplace=True)
    return out.tail(MAX_CANDLES).reset_index(drop=True)


def _build_analysis_dataframe(candle_1m=None, previous_m1=None, df=None):
    if df is not None:
        return _normalize(df)
    history = _normalize(previous_m1 if isinstance(previous_m1, pd.DataFrame) else pd.DataFrame())
    if isinstance(candle_1m, pd.Series): current = candle_1m.to_dict()
    elif isinstance(candle_1m, dict): current = dict(candle_1m)
    else: current = None
    if current is None: return history
    cur = _normalize(pd.DataFrame([current]))
    if cur.empty: return history
    return _normalize(pd.concat([history, cur], ignore_index=True))


def add_indicators(df):
    out = _normalize(df)
    if out.empty: return out
    close, high, low = out["close"], out["high"], out["low"]
    out["ema9"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    out["ema21"] = close.ewm(span=EMA_MID, adjust=False).mean()
    out["ema50"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    pc = close.shift(1)
    out["tr"] = pd.concat([(high-low),(high-pc).abs(),(low-pc).abs()], axis=1).max(axis=1)
    out["atr"] = out["tr"].rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    d = close.diff()
    gain, loss = d.clip(lower=0), -d.clip(upper=0)
    ag = gain.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    al = loss.ewm(alpha=1/RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    rs = ag / al.replace(0, np.nan)
    out["rsi"] = 100 - (100/(1+rs))
    out.loc[(al==0)&(ag>0),"rsi"] = 100
    out.loc[(ag==0)&(al>0),"rsi"] = 0
    return out


def _atr(df):
    if df is None or df.empty: return 0.0
    if "tr" in df.columns: v = df["tr"].tail(ATR_PERIOD).mean()
    else: v = (df["high"]-df["low"]).tail(ATR_PERIOD).mean()
    if pd.isna(v) or v <= 0: v = (df["high"]-df["low"]).mean()
    return max(_safe_float(v), EPS)


def candle_direction(c):
    o, x = _safe_float(c.get("open")), _safe_float(c.get("close"))
    return "bull" if x>o else "bear" if x<o else "neutral"


def candle_metrics(c):
    o,h,l,x = map(lambda k:_safe_float(c.get(k)), ["open","high","low","close"])
    r=max(h-l,EPS); b=abs(x-o); up=max(h-max(o,x),0); lo=max(min(o,x)-l,0)
    return {"open":o,"high":h,"low":l,"close":x,"range":r,"body":b,
            "upper":up,"lower":lo,"body_ratio":b/r,"upper_ratio":up/r,
            "lower_ratio":lo/r,"close_position":(x-l)/r}


def _confirmed_swings(df,left=PIVOT_LEFT,right=PIVOT_RIGHT):
    highs,lows=[],[]
    if df is None or len(df)<left+right+3: return highs,lows
    start=max(left,len(df)-SWING_LOOKBACK); end=len(df)-right
    for i in range(start,end):
        h,l=float(df["high"].iloc[i]),float(df["low"].iloc[i])
        if h>=float(df["high"].iloc[i-left:i].max()) and h>=float(df["high"].iloc[i+1:i+right+1].max()):
            highs.append((i,h))
        if l<=float(df["low"].iloc[i-left:i].min()) and l<=float(df["low"].iloc[i+1:i+right+1].min()):
            lows.append((i,l))
    return highs,lows


def _last_swing_levels(df):
    highs,lows=_confirmed_swings(df)
    lh=highs[-1] if highs else None; ll=lows[-1] if lows else None
    w=df.tail(min(SWING_LOOKBACK,len(df)))
    if lh is None and not w.empty:
        i=int(w["high"].idxmax()); lh=(i,float(w.loc[i,"high"]))
    if ll is None and not w.empty:
        i=int(w["low"].idxmin()); ll=(i,float(w.loc[i,"low"]))
    return {"highs":highs,"lows":lows,"last_high":lh,"last_low":ll}


def detect_structure(df):
    w=_normalize(df)
    if len(w)<12: return "range"
    s=_last_swing_levels(w); hs,ls=s["highs"],s["lows"]
    atr=_atr(add_indicators(w)); gap=max(atr*0.05,EPS)
    if len(hs)>=2 and len(ls)>=2:
        if hs[-1][1]>hs[-2][1]+gap and ls[-1][1]>ls[-2][1]+gap: return "bullish"
        if hs[-1][1]<hs[-2][1]-gap and ls[-1][1]<ls[-2][1]-gap: return "bearish"
    ind=add_indicators(w); last=ind.iloc[-1]
    if last["ema9"]>last["ema21"]>last["ema50"]: return "bullish"
    if last["ema9"]<last["ema21"]<last["ema50"]: return "bearish"
    return "range"


def structure_score(df):
    w=_normalize(df)
    if len(w)<12:return 0
    s=_last_swing_levels(w); score=0
    if len(s["highs"])>=2 and s["highs"][-1][1]!=s["highs"][-2][1]:score+=1
    if len(s["lows"])>=2 and s["lows"][-1][1]!=s["lows"][-2][1]:score+=1
    st=detect_structure(w)
    if st in ("bullish","bearish"):score+=2
    ind=add_indicators(w)
    if len(ind)>=3:
        if st=="bullish" and ind["ema9"].iloc[-1]>ind["ema21"].iloc[-1]:score+=1
        if st=="bearish" and ind["ema9"].iloc[-1]<ind["ema21"].iloc[-1]:score+=1
    return min(score,5)


def _find_divergence(history,direction):
    """Busca divergencia RSI en dos mínimos/máximos separados."""
    w=history.tail(DIVERGENCE_LOOKBACK).reset_index(drop=True)
    if len(w)<10:return {"valid":False,"type":None,"a":None,"b":None,"reason":"historial insuficiente"}
    ind=add_indicators(w)
    rsi=ind["rsi"].values
    if direction=="bullish":
        candidates=[]
        for i in range(2,len(w)-2):
            if w["low"].iloc[i]<=w["low"].iloc[i-1] and w["low"].iloc[i]<=w["low"].iloc[i+1]:
                candidates.append(i)
        if len(candidates)>=2:
            a,b=candidates[-2],candidates[-1]
            if b-a>=DIVERGENCE_MIN_SEPARATION and w["low"].iloc[b] < w["low"].iloc[a] and rsi[b] > rsi[a]:
                return {"valid":True,"type":"bullish","a":a,"b":b,"price_a":float(w["low"].iloc[a]),"price_b":float(w["low"].iloc[b]),"rsi_a":float(rsi[a]),"rsi_b":float(rsi[b]),"reason":"divergencia alcista RSI"}
    else:
        candidates=[]
        for i in range(2,len(w)-2):
            if w["high"].iloc[i]>=w["high"].iloc[i-1] and w["high"].iloc[i]>=w["high"].iloc[i+1]:
                candidates.append(i)
        if len(candidates)>=2:
            a,b=candidates[-2],candidates[-1]
            if b-a>=DIVERGENCE_MIN_SEPARATION and w["high"].iloc[b] > w["high"].iloc[a] and rsi[b] < rsi[a]:
                return {"valid":True,"type":"bearish","a":a,"b":b,"price_a":float(w["high"].iloc[a]),"price_b":float(w["high"].iloc[b]),"rsi_a":float(rsi[a]),"rsi_b":float(rsi[b]),"reason":"divergencia bajista RSI"}
    return {"valid":False,"type":None,"a":None,"b":None,"reason":"sin divergencia válida"}


def _count_zone_touches(history,level,atr,side):
    """Cuenta contactos estructuralmente separados con la zona."""
    if level is None or atr<=0:return 0
    tol=max(atr*TOUCH_ZONE_ATR,EPS)
    touches=[]; last_i=-999
    for i in range(len(history)):
        row=history.iloc[i]; hit=(abs(float(row["low"])-level)<=tol or abs(float(row["high"])-level)<=tol)
        if side=="support": hit = hit or (float(row["low"])<=level+tol and float(row["close"])>=level-tol)
        else: hit = hit or (float(row["high"])>=level-tol and float(row["close"])<=level+tol)
        if hit and i-last_i>=TOUCH_MIN_SEPARATION:
            touches.append(i); last_i=i
    return len(touches), touches


def _reversal_candle(c, direction, previous):
    # Vela N debe ser una vela de reversión, no una simple vela direccional.
    if c["body_ratio"]<MIN_BODY_RATIO: return False
    if direction=="bullish":
        return (c["lower_ratio"]>=MIN_REJECTION_WICK_RATIO and
                c["lower"]>=c["body"]*MIN_WICK_BODY_RATIO and
                c["close_position"]>=MIN_CLOSE_POSITION_CALL and
                c["close"]>c["open"] and c["close"]>previous["close"])
    return (c["upper_ratio"]>=MIN_REJECTION_WICK_RATIO and
            c["upper"]>=c["body"]*MIN_WICK_BODY_RATIO and
            c["close_position"]<=MAX_CLOSE_POSITION_PUT and
            c["close"]<c["open"] and c["close"]<previous["close"])


def _ema_alignment(last,direction):
    e9,e21,e50=map(lambda k:_safe_float(last.get(k)),["ema9","ema21","ema50"]); close=_safe_float(last.get("close"))
    return e9>=e21>=e50 and close>=e21 if direction=="bullish" else e9<=e21<=e50 and close<=e21


def _body_valid(c,atr):
    ba=c["body"]/max(atr,EPS)
    return c["body_ratio"]>=MIN_BODY_RATIO and MIN_BODY_ATR<=ba<=MAX_BODY_ATR


def _room(price,opposite,atr,direction):
    if opposite is None or atr<=0:return False,0.0
    room=(opposite-price) if direction=="bullish" else (price-opposite)
    x=room/atr
    return x>=MIN_ROOM_TO_OPPOSITE_ATR,x


def analyze_market(df=None,candle_1m=None,candles_5s=None,previous_m1=None,pair=None):
    result=_empty_result()
    clean=_build_analysis_dataframe(candle_1m,previous_m1,df)
    if len(clean)<MIN_BARS:
        result["reason"]=f"Historial insuficiente {len(clean)}/{MIN_BARS}"; return result
    data=add_indicators(clean)
    current=data.iloc[-1]; history=data.iloc[:-1].copy()
    if len(history)<MIN_BARS-1:return {**result,"reason":"Historial cerrado insuficiente"}
    atr=_atr(history); rsi=_safe_float(current.get("rsi"),50)
    if atr<=0:return {**result,"reason":"ATR inválido"}
    structure=detect_structure(history); ss=structure_score(history)
    swings=_last_swing_levels(history); lh=swings["last_high"][1] if swings["last_high"] else None; ll=swings["last_low"][1] if swings["last_low"] else None
    c=candle_metrics(current); p=candle_metrics(history.iloc[-1]); price=c["close"]
    ts=int(current["from"]) if "from" in data.columns and not pd.isna(current["from"]) else None
    result.update({"direction":structure,"trend":structure,"structure":structure,"structure_score":ss,"atr":atr,"rsi":rsi,
                   "last_swing_high":lh,"last_swing_low":ll,"support":ll,"resistance":lh,
                   "candle":candle_direction(current),"candle_timestamp":ts})
    base={"structure":structure,"structure_score":ss,"rsi":rsi,"atr":atr,
          "last_swing_high":lh,"last_swing_low":ll,"support":ll,"resistance":lh,"candle":c}
    result["analysis"]=base
    if structure not in ("bullish","bearish") or ss<MIN_STRUCTURE_SCORE:
        result["reason"]="Estructura lateral/insuficiente"; return result
    if not _body_valid(c,atr):
        result["reason"]="Vela N con cuerpo inválido"; return result

    direction=structure
    level=ll if direction=="bullish" else lh
    side="support" if direction=="bullish" else "resistance"
    div=_find_divergence(history,direction)
    touches,touch_idx=_count_zone_touches(history,level,atr,side)
    result["analysis"].update({"divergence":div,"zone_touch_count":touches,"zone_touch_indices":touch_idx})
    if not div["valid"]:
        result["reason"]="Sin divergencia válida en la estructura"; return result
    if touches not in (MIN_TOUCHES,MAX_TOUCHES):
        result["reason"]=f"Zona descartada: {touches} toques; se exige 4.º o 5.º toque"; result["zone"]="zona_no_4_5_toques"; return result
    distance=abs(price-level)/atr
    if distance>MAX_ENTRY_DISTANCE_ATR:
        result["reason"]="Precio demasiado alejado de la zona"; return result
    if not _reversal_candle(c,direction,p):
        result["reason"]="N no es una vela de reversión válida"; result["zone"]="rechazo_sin_reversion"; return result
    room_ok,room=_room(price,lh if direction=="bullish" else ll,atr,direction)
    if not room_ok:
        result["reason"]="Poco espacio hasta el extremo opuesto"; return result
    if not _ema_alignment(current,direction):
        result["reason"]="EMA no confirma la estructura"; return result
    if direction=="bullish" and not (38<=rsi<=68):
        result["reason"]=f"RSI CALL fuera de rango {rsi:.1f}"; return result
    if direction=="bearish" and not (32<=rsi<=62):
        result["reason"]=f"RSI PUT fuera de rango {rsi:.1f}"; return result

    wick=(c["lower"] if direction=="bullish" else c["upper"])/c["range"]
    quality=50 + min(15,wick*30) + ss*3 + (5 if touches==5 else 0) + (5 if div["valid"] else 0)
    quality += min(10,max(0,(1-distance/ max(MAX_ENTRY_DISTANCE_ATR,EPS))*10))
    quality=int(max(0,min(100,round(quality))))
    result["analysis"].update({"zone":"soporte_rechazado" if direction=="bullish" else "resistencia_rechazada",
                               "entry_quality":quality,"room_to_opposite_atr":room,
                               "distance_to_zone_atr":distance,
                               "reversal_candle":True,"touch_count":touches})
    if quality<MIN_ENTRY_SCORE:
        result["reason"]=f"{direction.upper()} rechazado: calidad {quality}/100"; return result
    signal="call" if direction=="bullish" else "put"
    result.update({"signal":signal,"score":quality,"continuity":True,"blocked":False,
                   "zone":"soporte_rechazado" if direction=="bullish" else "resistencia_rechazada",
                   "entry_type":"reversal_on_4th_5th_touch","entry_quality":quality,
                   "signal_price":price,"candle_open":c["open"],"candle_close":c["close"],
                   "distance_to_zone_atr":distance,
                   "reason":f"{signal.upper()} | divergencia + zona + {touches}º toque + vela N de reversión | calidad={quality}/100",
                   "analysis":{**result["analysis"],"divergence_reason":div["reason"]}})
    return result


def get_signal(df): return analyze_market(df).get("signal")
def signal(df): return get_signal(df)

if __name__=="__main__":
    print("strategy.py cargado correctamente.")
