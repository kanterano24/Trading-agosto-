from __future__ import annotations
"""Recolector de anatomia de 10 velas M1. Sin indicadores, S/R ni rechazo."""
from typing import Any, Dict, Optional
import pandas as pd
M1=60
WINDOW=10

def normalize(df: Optional[pd.DataFrame])->pd.DataFrame:
    if df is None or not isinstance(df,pd.DataFrame) or df.empty:return pd.DataFrame()
    d=df.copy().rename(columns={'max':'high','min':'low'})
    req=['from','open','high','low','close']
    if any(c not in d.columns for c in req):return pd.DataFrame()
    for c in req:d[c]=pd.to_numeric(d[c],errors='coerce')
    return d.dropna(subset=req).drop_duplicates('from').sort_values('from').reset_index(drop=True)

def candle_data(row:pd.Series)->Dict[str,Any]:
    o,h,l,c=map(float,(row['open'],row['high'],row['low'],row['close']))
    color='VERDE' if c>o else 'ROJA' if c<o else 'DOJI'
    return {'timestamp':int(row['from']),'open':o,'lower_wick':max(0.0,min(o,c)-l),'close':c,'upper_wick':max(0.0,h-max(o,c)),'high':h,'low':l,'body':abs(c-o),'color':color}

def last_10_closed(df:pd.DataFrame):
    d=normalize(df)
    return [candle_data(r) for _,r in d.iloc[-WINDOW:].iterrows()] if len(d)>=WINDOW else []

def summarize_window(df:pd.DataFrame):
    cs=last_10_closed(df)
    return {'ready':len(cs)==WINDOW,'count':len(cs),'candles':cs,'sequence':' '.join('G' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D' for c in cs)}

def compare_window_to_next(previous_10:list[dict],next_candle:dict):
    if len(previous_10)!=WINDOW:return {'ready':False}
    return {'ready':True,'sequence':' '.join('G' if c['color']=='VERDE' else 'R' if c['color']=='ROJA' else 'D' for c in previous_10),'next_color':next_candle['color'],'next_open':next_candle['open'],'next_lower_wick':next_candle['lower_wick'],'next_close':next_candle['close'],'next_upper_wick':next_candle['upper_wick']}

def analyze_market(df:Optional[pd.DataFrame]=None,**_:Any):
    return {'signal':None,'blocked':True,'reason':'modo recoleccion: no se ejecutan operaciones','analysis_timeframe':'M1','entry_timeframe':'M1','target_expiration_minutes':1,'window':summarize_window(df if df is not None else pd.DataFrame())}

def get_signal(df):return None
def signal(df):return None
