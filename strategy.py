"""strategy.py - MOMENTUM M1 para Binary OTC.

Analiza exclusivamente velas M1 cerradas. No usa soporte/resistencia.
La vela N cerrada genera una señal para ejecutar en N+1.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
import math
import pandas as pd

MIN_BARS = 35
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 5
MIN_BODY_RATIO = 0.55
MIN_BODY_ATR = 0.35
MAX_BODY_ATR = 4.0
MIN_CLOSE_POSITION = 0.75
MIN_RANGE_ATR = 0.70
MIN_MOMENTUM_SCORE = 70
MAX_CONSECUTIVE = 5
EPS = 1e-12

def _empty_result(reason="sin señal") -> Dict[str, Any]:
    return {"signal": None, "direction": "range", "trend": "range", "reason": reason,
            "score": 0, "continuity": False, "blocked": True, "zone": "momentum",
            "entry_type": "momentum", "entry_quality": 0, "rsi": 50.0, "atr": 0.0,
            "candle_timestamp": None, "analysis": {}}

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty: return pd.DataFrame()
    out=df.copy()
    if "max" in out.columns and "high" not in out.columns: out=out.rename(columns={"max":"high"})
    if "min" in out.columns and "low" not in out.columns: out=out.rename(columns={"min":"low"})
    required=["open","high","low","close"]
    if any(c not in out.columns for c in required): return pd.DataFrame()
    for c in required: out[c]=pd.to_numeric(out[c],errors="coerce")
    if "from" in out.columns: out["from"]=pd.to_numeric(out["from"],errors="coerce")
    out=out.dropna(subset=required).sort_values("from" if "from" in out.columns else required[0]).reset_index(drop=True)
    return out

def _ema(s, period): return s.ewm(span=period, adjust=False).mean()

def _atr(df, period=ATR_PERIOD):
    prev=df["close"].shift(1)
    tr=pd.concat([(df["high"]-df["low"]),(df["high"]-prev).abs(),(df["low"]-prev).abs()],axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1]) if len(tr)>=period and pd.notna(tr.rolling(period).mean().iloc[-1]) else 0.0

def _rsi(s, period=14):
    d=s.diff(); gain=d.clip(lower=0).rolling(period).mean(); loss=(-d.clip(upper=0)).rolling(period).mean()
    if loss.iloc[-1] == 0: return 100.0 if gain.iloc[-1] > 0 else 50.0
    return float(100 - 100/(1 + gain.iloc[-1]/loss.iloc[-1]))

def _metrics(c):
    rng=max(float(c.high-c.low),EPS); body=abs(float(c.close-c.open))
    return body, rng, body/rng, (float(c.close-c.low)/rng)

def analyze_market(df: Optional[pd.DataFrame]=None, candle_1m: Any=None, previous_m1: Optional[pd.DataFrame]=None,
                   candle_5m: Any=None, previous_m5: Optional[pd.DataFrame]=None, m1_block=None, pair=None, **kwargs):
    if df is None:
        if previous_m1 is not None:
            base=previous_m1.copy()
            if candle_1m is not None: base=pd.concat([base, pd.DataFrame([candle_1m])],ignore_index=True)
        elif previous_m5 is not None:
            base=previous_m5.copy()
            if candle_5m is not None: base=pd.concat([base, pd.DataFrame([candle_5m])],ignore_index=True)
        else: base=pd.DataFrame()
    else: base=df.copy()
    data=_normalize(base)
    result=_empty_result()
    if len(data)<MIN_BARS: result["reason"]=f"Historial M1 insuficiente {len(data)}/{MIN_BARS}"; return result
    current=data.iloc[-1]; hist=data.iloc[:-1]
    atr=_atr(data); rsi=_rsi(data["close"])
    if atr<=0: result["reason"]="ATR M1 inválido"; return result
    body,rng,body_ratio,close_pos=_metrics(current)
    direction="bullish" if current.close>current.open else "bearish" if current.close<current.open else "range"
    ema9=_ema(data.close,EMA_FAST).iloc[-1]; ema21=_ema(data.close,EMA_MID).iloc[-1]; ema50=_ema(data.close,EMA_SLOW).iloc[-1]
    ema9p=_ema(data.close,EMA_FAST).iloc[-4]; ema21p=_ema(data.close,EMA_MID).iloc[-4]
    recent=hist.tail(BREAKOUT_LOOKBACK)
    prev_high=float(recent.high.max()); prev_low=float(recent.low.min())
    bullish_break=float(current.close)>prev_high
    bearish_break=float(current.close)<prev_low
    body_atr=body/atr; range_atr=rng/atr
    trend_bull=ema9>ema21>ema50 and ema9>ema9p and ema21>ema21p and current.close>ema21
    trend_bear=ema9<ema21<ema50 and ema9<ema9p and ema21<ema21p and current.close<ema21
    dirs=["bull" if r.close>r.open else "bear" if r.close<r.open else "neutral" for _,r in data.tail(MAX_CONSECUTIVE+1).iterrows()]
    consecutive=0
    for d in reversed(dirs):
        if d==("bull" if direction=="bullish" else "bear"): consecutive+=1
        else: break
    score=0; reasons=[]
    if direction=="bullish":
        checks=[bullish_break, body_ratio>=MIN_BODY_RATIO, body_atr>=MIN_BODY_ATR, range_atr>=MIN_RANGE_ATR,
                close_pos>=MIN_CLOSE_POSITION, trend_bull, consecutive<=MAX_CONSECUTIVE]
        score=sum(15 for x in checks if x)
        if bullish_break: reasons.append("rompe máximo de las últimas M1")
        if body_ratio>=MIN_BODY_RATIO: reasons.append("cuerpo fuerte")
        if body_atr>=MIN_BODY_ATR: reasons.append("cuerpo con fuerza ATR")
        if close_pos>=MIN_CLOSE_POSITION: reasons.append("cierre cerca del máximo")
        if trend_bull: reasons.append("EMA 9/21/50 alineadas al alza")
        valid=all(checks[:6]) and consecutive<=MAX_CONSECUTIVE
    elif direction=="bearish":
        close_pos_bear=1-close_pos
        checks=[bearish_break, body_ratio>=MIN_BODY_RATIO, body_atr>=MIN_BODY_ATR, range_atr>=MIN_RANGE_ATR,
                close_pos_bear>=MIN_CLOSE_POSITION, trend_bear, consecutive<=MAX_CONSECUTIVE]
        score=sum(15 for x in checks if x)
        if bearish_break: reasons.append("rompe mínimo de las últimas M1")
        if body_ratio>=MIN_BODY_RATIO: reasons.append("cuerpo fuerte")
        if body_atr>=MIN_BODY_ATR: reasons.append("cuerpo con fuerza ATR")
        if close_pos_bear>=MIN_CLOSE_POSITION: reasons.append("cierre cerca del mínimo")
        if trend_bear: reasons.append("EMA 9/21/50 alineadas a la baja")
        valid=all(checks[:6]) and consecutive<=MAX_CONSECUTIVE
    else: valid=False
    score=min(100,score+ (10 if range_atr>=1.0 else 0) + (10 if body_ratio>=0.70 else 0))
    ts=int(current["from"]) if "from" in data.columns and pd.notna(current["from"]) else None
    result.update({"direction":direction,"trend":direction,"rsi":rsi,"atr":atr,"candle_timestamp":ts,
                   "analysis":{"timeframe":"M1","momentum":True,"breakout_high":prev_high,"breakout_low":prev_low,
                   "body_ratio":body_ratio,"body_atr":body_atr,"range_atr":range_atr,"close_position":close_pos,
                   "consecutive":consecutive,"ema9":float(ema9),"ema21":float(ema21),"ema50":float(ema50),"reasons":reasons}})
    if not valid:
        result["reason"]="Momentum M1 insuficiente"
        return result
    signal="call" if direction=="bullish" else "put"
    result.update({"signal":signal,"score":int(score),"continuity":True,"blocked":False,"entry_quality":int(score),
                   "reason":f"{signal.upper()} | MOMENTUM M1 | {"; ".join(reasons)}"})
    return result

def get_signal(df): return analyze_market(df=df).get("signal")
def signal(df): return get_signal(df)
