from __future__ import annotations
import logging, os, threading, time
from typing import Any, Dict, Optional, Tuple
import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code
from strategy import analyze_market

# ============================================================
# CONFIGURACIÓN
# ============================================================
IQ_EMAIL=os.getenv("IQ_EMAIL")
IQ_PASSWORD=os.getenv("IQ_PASSWORD")
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

TIMEFRAME=60
EXPIRATION=1
AMOUNT=float(os.getenv("AMOUNT","1000"))
CANDLE_COUNT=int(os.getenv("CANDLE_COUNT","60"))
MAX_BINARY_PAIRS=int(os.getenv("MAX_BINARY_PAIRS","100"))
PAIR_REFRESH_SECONDS=60.0
SNIPER_POLL=0.02
TRADE_COOLDOWN=60.0

BOT_RUNNING=False
IQ:Optional[IQ_Option]=None
PAIRS:list[str]=[]
LAST_PAIR_REFRESH=0.0
LIVE_STATE:Dict[str,Dict[str,Any]]={}
PENDING_ENTRY:Dict[str,Dict[str,Any]]={}
LAST_TRADE_TIME:Dict[str,float]={}
LAST_TRADE_CANDLE:Dict[str,int]={}
STATE_LOCK=threading.RLock()

logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s")
logger=logging.getLogger(__name__)

# ============================================================
# DESACTIVAR DIGITAL; BINARY SE MANTIENE
# ============================================================
def _binary_only_digital_underlying(self): return {"underlying":[]}
def _disabled_digital_open(self,*args,**kwargs): return None
setattr(IQ_Option,"get_digital_underlying_list_data",_binary_only_digital_underlying)
for n in ("_IQ_Option__get_digital_open","__get_digital_open","_get_digital_open"):
    if hasattr(IQ_Option,n): setattr(IQ_Option,n,_disabled_digital_open)

# ============================================================
# TELEGRAM
# ============================================================
def _telegram_post(endpoint,data,timeout=3.0):
    if not TELEGRAM_TOKEN:return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}",data=data,timeout=timeout)
        return r.status_code==200
    except Exception:return False

def telegram_send(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    threading.Thread(target=lambda:_telegram_post("sendMessage",{"chat_id":TELEGRAM_CHAT_ID,"text":message}),daemon=True).start()

def telegram_command_loop():
    global BOT_RUNNING
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    last=None
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    while True:
        try:
            params={"timeout":1}
            if last is not None:params["offset"]=last+1
            data=requests.get(url,params=params,timeout=3).json()
            if not data.get("ok"):time.sleep(.5);continue
            for u in data.get("result",[]):
                if u.get("update_id") is not None:last=int(u["update_id"])
                m=u.get("message") or {}
                text=str(m.get("text","")).strip().lower()
                chat=str((m.get("chat") or {}).get("id",""))
                if chat!=str(TELEGRAM_CHAT_ID):continue
                if text=="/start":
                    BOT_RUNNING=True
                    telegram_send(f"🟢 BOT ACTIVADO\n\nBINARY OTC + NO OTC\nM1\nEntrada N+1\nExpiración: 1 minuto\nPares: {len(PAIRS)}")
                elif text=="/stop":
                    BOT_RUNNING=False
                    telegram_send("🔴 BOT DETENIDO\n\nNo se abrirán nuevas operaciones.")
                elif text=="/status":
                    telegram_send(f"📊 ESTADO\n\nEstado: {'🟢 ACTIVO' if BOT_RUNNING else '🔴 DETENIDO'}\nMercado: BINARY OTC + NO OTC\nTemporalidad: 1 minuto\nEntrada: N+1\nExpiración: 1 minuto\nPares: {len(PAIRS)}")
        except Exception as exc:
            logger.debug("Telegram: %s",exc);time.sleep(1)

# ============================================================
# DESCUBRIMIENTO DE TODOS LOS BINARY
# ============================================================
def _load_binary_catalog():
    if IQ is None or not hasattr(IQ,"get_all_init_v2"):return [],False
    try:data=IQ.get_all_init_v2()
    except Exception as exc:
        logger.warning("Catálogo BINARY no disponible: %s",exc);return [],False
    if not isinstance(data,dict):return [],False
    binary=data.get("binary")
    if not isinstance(binary,dict):
        result=data.get("result")
        if isinstance(result,dict):binary=result.get("binary")
    if not isinstance(binary,dict):return [],False
    actives=binary.get("actives",{})
    if not isinstance(actives,dict):return [],False
    pairs=[]
    for active_id,info in actives.items():
        if not isinstance(info,dict):continue
        raw=info.get("name")
        if not isinstance(raw,str):continue
        name=raw.split(".",1)[1] if "." in raw else raw
        name=name.strip()
        enabled=info.get("enabled",True)
        suspended=info.get("is_suspended",info.get("suspended",False))
        if enabled is False or suspended is True:continue
        try:nid=int(active_id)
        except Exception:continue
        OP_code.ACTIVES[name]=nid
        pairs.append(name)
    return sorted(set(pairs)),True

def discover_binary_pairs():
    pairs,ok=_load_binary_catalog()
    return pairs if ok else []

def refresh_binary_pairs(force=False):
    global PAIRS,LAST_PAIR_REFRESH
    now=time.time()
    if not force and now-LAST_PAIR_REFRESH<PAIR_REFRESH_SECONDS:return list(PAIRS)
    discovered=discover_binary_pairs()
    selected=discovered[:MAX_BINARY_PAIRS]
    previous=set(PAIRS); current=set(selected)
    with STATE_LOCK:
        PAIRS=list(selected);LAST_PAIR_REFRESH=now
        for pair in previous-current:
            LIVE_STATE.pop(pair,None);PENDING_ENTRY.pop(pair,None)
            LAST_TRADE_CANDLE.pop(pair,None)
    if current!=previous:
        logger.info("UNIVERSO BINARY ACTUALIZADO | %d pares",len(selected))
        telegram_send("🔄 UNIVERSO BINARY ACTUALIZADO\n\n"
                      f"OTC + NO OTC\nPares analizados: {len(selected)}/{MAX_BINARY_PAIRS}")
    return list(PAIRS)

# Compatibilidad con nombres anteriores
def discover_binary_otc_pairs(): return discover_binary_pairs()
def refresh_binary_otc_pairs(force=False): return refresh_binary_pairs(force)

# ============================================================
# RELOJ / CONEXIÓN
# ============================================================
def get_iq_server_timestamp():
    if IQ is None:return time.time()
    try:
        x=float(IQ.get_server_timestamp())
        return x if x>0 else time.time()
    except Exception:return time.time()

def floor_candle_timestamp(ts):return int(ts//TIMEFRAME)*TIMEFRAME

def connect_iq():
    global IQ
    if not IQ_EMAIL or not IQ_PASSWORD:raise ValueError("Faltan IQ_EMAIL/IQ_PASSWORD")
    logger.info("Conectando a IQ Option...")
    IQ=IQ_Option(IQ_EMAIL,IQ_PASSWORD)
    connected,reason=IQ.connect()
    if not connected:raise ConnectionError(f"No se pudo conectar a IQ Option: {reason}")
    refresh_binary_pairs(True)
    telegram_send("🟢 IQ OPTION CONECTADO\n\nBINARY OTC + NO OTC\nM1\nN cerrada → N+1\nExpiración: 1 minuto")
    return True

def ensure_connection():
    global IQ
    try:
        if IQ is None:return connect_iq()
        if IQ.check_connect():return True
        logger.warning("Conexión perdida. Reconectando...")
        ok,reason=IQ.connect()
        if not ok:logger.error("No se pudo reconectar: %s",reason);return False
        refresh_binary_pairs(True);telegram_send("🟢 IQ OPTION RECONECTADO");return True
    except Exception as exc:
        logger.error("Error conexión: %s",exc);return False

# ============================================================
# VELAS CERRADAS
# ============================================================
def start_realtime_streams(): return None
def realtime_dataframe(pair): return pd.DataFrame()

def get_closed_candles(pair):
    if IQ is None:return None
    try:
        candles=IQ.get_candles(pair,TIMEFRAME,CANDLE_COUNT,get_iq_server_timestamp())
        if not candles:return None
        df=pd.DataFrame(candles).rename(columns={"max":"high","min":"low"})
        req=["from","open","high","low","close"]
        if not all(c in df.columns for c in req):return None
        for c in req:df[c]=pd.to_numeric(df[c],errors="coerce")
        df.dropna(subset=req,inplace=True);df["from"]=df["from"].astype(int)
        return df.drop_duplicates("from",keep="last").sort_values("from").tail(CANDLE_COUNT).reset_index(drop=True)
    except Exception as exc:
        logger.debug("%s | historial: %s",pair,exc);return None

def get_row_by_ts(df,ts):
    if df is None or df.empty or "from" not in df.columns:return None
    x=df[df["from"].astype(int)==int(ts)]
    return x.iloc[-1] if not x.empty else None

def candle_values(row):
    return {k:float(row[k]) for k in ("open","high","low","close")}

# ============================================================
# ANÁLISIS N
# ============================================================
def analyze_closed_candle(pair,expected_closed_ts):
    df=get_closed_candles(pair)
    closed=get_row_by_ts(df,expected_closed_ts) if df is not None else None
    if closed is None or df is None or len(df)<25:return False
    df=df[df["from"].astype(int)<=expected_closed_ts].sort_values("from").reset_index(drop=True)
    if len(df)<22:return False
    state=LIVE_STATE.get(pair)
    if state and int(state.get("analyzed_ts",-1))==expected_closed_ts:return True

    result=analyze_market(candle_1m=closed.to_dict(),previous_m1=df.iloc[:-1].copy(),pair=pair)
    with STATE_LOCK:
        LIVE_STATE[pair]={"analyzed_ts":expected_closed_ts,"signal":result.get("signal"),
                          "score":int(result.get("score",0)),"reason":result.get("reason",""),
                          "analysis":result.get("analysis",{}),"created_at":time.time()}
    signal=result.get("signal")
    logger.info("%s | N CERRADA | signal=%s | score=%s | %s",pair,signal,result.get("score",0),result.get("reason",""))
    if signal not in ("call","put"):return True

    values=candle_values(closed)
    execution_ts=expected_closed_ts+TIMEFRAME
    with STATE_LOCK:
        PENDING_ENTRY[pair]={"signal":signal,"score":int(result.get("score",0)),
            "continuity_ts":expected_closed_ts,"execution_ts":execution_ts,
            **values,"reason":result.get("reason",""),"analysis":result.get("analysis",{}),
            "created_at":time.time()}
    side="CALL 🟢" if signal=="call" else "PUT 🔴"
    a=result.get("analysis",{})
    telegram_send(f"🎯 SEÑAL COMPLETA\n\nPar: {pair}\nDirección: {side}\nScore: {result.get('score',0)}/100\n"
                  f"Toques zona: {a.get('zone_touch_count',0)}\nDivergencia: {a.get('divergence',{}).get('type')}\n"
                  f"Vela N: {expected_closed_ts}\nN+1: {execution_ts}\n⚡ Entrada N+1\n⏳ Expiración: 1 minuto\n\n{result.get('reason','')}")
    return True

# ============================================================
# EJECUCIÓN
# ============================================================
def cooldown_active(pair):return time.time()-LAST_TRADE_TIME.get(pair,0)<TRADE_COOLDOWN

def buy_binary(pair,signal):
    if IQ is None or signal not in ("call","put"):return False,None
    try:
        r=IQ.buy(AMOUNT,pair,signal,EXPIRATION)
        if isinstance(r,tuple):return bool(r[0]),r[1] if len(r)>1 else None
        return (True,r) if r not in (None,False,"error",-1) else (False,r)
    except Exception as exc:
        logger.error("%s | buy error: %s",pair,exc);return False,None

def execute_sniper(pair,pending):
    execution_ts=int(pending["execution_ts"]); signal=str(pending["signal"])
    current=floor_candle_timestamp(get_iq_server_timestamp())
    if current<execution_ts:return False
    if current>execution_ts:
        with STATE_LOCK:PENDING_ENTRY.pop(pair,None)
        return False
    if LAST_TRADE_CANDLE.get(pair)==execution_ts or cooldown_active(pair):return False
    if floor_candle_timestamp(get_iq_server_timestamp())!=execution_ts:return False
    sent_at=get_iq_server_timestamp()
    ok,order_id=buy_binary(pair,signal)
    if not ok:
        with STATE_LOCK:PENDING_ENTRY.pop(pair,None)
        telegram_send(f"❌ ORDEN RECHAZADA\n\nPar: {pair}\nDirección: {signal.upper()}\nN+1: {execution_ts}")
        return False
    LAST_TRADE_TIME[pair]=time.time();LAST_TRADE_CANDLE[pair]=execution_ts
    with STATE_LOCK:PENDING_ENTRY.pop(pair,None)
    telegram_send(f"✅ SNIPER EJECUTADO\n\nPar: {pair}\nDirección: {signal.upper()}\nN+1: {execution_ts}\nReloj IQ: {sent_at:.3f}\nID: {order_id}\n⏳ Expiración: 1 minuto")
    logger.info("%s | EJECUTADO | %s | N=%s | N+1=%s | ID=%s",pair,signal.upper(),pending["continuity_ts"],execution_ts,order_id)
    return True

# ============================================================
# MOTOR POR PAR / TODOS LOS BINARY
# ============================================================
def process_pair(pair):
    if IQ is None:return
    current=floor_candle_timestamp(get_iq_server_timestamp())
    closed_ts=current-TIMEFRAME
    state=LIVE_STATE.get(pair)
    analyzed=int(state.get("analyzed_ts",-1)) if state else -1
    if analyzed!=closed_ts:analyze_closed_candle(pair,closed_ts)
    pending=PENDING_ENTRY.get(pair)
    if pending is not None and int(pending["execution_ts"])==current:
        execute_sniper(pair,pending)

def analyze_all_pairs():
    if not BOT_RUNNING:return
    refresh_binary_pairs()
    for pair in list(PAIRS):
        if not BOT_RUNNING:return
        try:process_pair(pair)
        except Exception:logger.exception("Error procesando %s",pair)

# ============================================================
# MAIN
# ============================================================
def main():
    global BOT_RUNNING
    logger.info("========================================")
    logger.info("BOT BINARY OTC + NO OTC | M1 | EXPIRACION 1 MINUTO")
    logger.info("========================================")
    required={"IQ_EMAIL":IQ_EMAIL,"IQ_PASSWORD":IQ_PASSWORD,"TELEGRAM_TOKEN":TELEGRAM_TOKEN,"TELEGRAM_CHAT_ID":TELEGRAM_CHAT_ID}
    missing=[k for k,v in required.items() if not v]
    if missing:logger.error("Faltan variables: %s",", ".join(missing));return
    threading.Thread(target=telegram_command_loop,daemon=True).start()
    try:connect_iq()
    except Exception as exc:
        logger.exception("No se pudo iniciar IQ Option");telegram_send(f"❌ ERROR DE CONEXIÓN\n\n{exc}");return
    BOT_RUNNING=False
    telegram_send("🤖 BOT LISTO\n\n🔎 Analiza TODOS los BINARY disponibles (OTC + NO OTC).\n"
                  "📌 N cerrada = análisis\n⚡ N+1 = entrada\n⏳ Expiración = 1 minuto\nUsa /start para activar.")
    while True:
        try:
            if not BOT_RUNNING:time.sleep(.25);continue
            if not ensure_connection():time.sleep(1);continue
            analyze_all_pairs();time.sleep(SNIPER_POLL)
        except KeyboardInterrupt:
            BOT_RUNNING=False;telegram_send("🔴 BOT DETENIDO MANUALMENTE");break
        except Exception as exc:
            logger.exception("Error principal");telegram_send(f"⚠️ ERROR EN BOT\n\n{exc}");time.sleep(1)

if __name__=="__main__":main()
