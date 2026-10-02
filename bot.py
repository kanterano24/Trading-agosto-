from __future__ import annotations
"""IQ Option OTC M1 scanner. Demo only; real-money orders are blocked."""
import logging, os, time, threading
from typing import Any, Optional
import pandas as pd
import requests
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.constants as OP_code
from strategy import M1, WINDOW, analyze_market

# Este bot opera exclusivamente en Binary OTC.
# Algunas versiones de iqoptionapi lanzan un hilo cuyo target es
# _get_digital_open. Sobrescribir el método no siempre basta, porque la
# biblioteca puede haber guardado el target antes de la conexión.
# Por eso bloqueamos únicamente el inicio de ese hilo concreto.
_original_thread_start = threading.Thread.start

def _thread_start_without_digital(self, *args, **kwargs):
    target = getattr(self, "_target", None)
    target_name = getattr(target, "__name__", "")
    thread_name = getattr(self, "name", "")
    if target_name == "_get_digital_open" or thread_name == "_get_digital_open":
        log.warning("Hilo interno Digital omitido: este bot solo utiliza Binary OTC.")
        return None
    return _original_thread_start(self, *args, **kwargs)

threading.Thread.start = _thread_start_without_digital

class OTCOnlyIQOption(IQ_Option):
    def _get_digital_open(self, *args, **kwargs):
        log.info("Consulta Digital desactivada; se utiliza únicamente Binary OTC.")
        return None

IQ_EMAIL=os.getenv('IQ_EMAIL'); IQ_PASSWORD=os.getenv('IQ_PASSWORD')
TELEGRAM_TOKEN=os.getenv('TELEGRAM_TOKEN'); TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID')
REFRESH_SECONDS=max(60,int(os.getenv('CATALOG_REFRESH_SECONDS','600')))
AMOUNT=float(os.getenv('AMOUNT','150'))
EXPIRATION=1; CANDLE_COUNT=max(120,int(os.getenv('CANDLE_COUNT_M1','120')))
POLL=max(.15,float(os.getenv('POLL_SECONDS','.5')))
DEMO_ENABLED=os.getenv('ENABLE_DEMO_TRADING','1').lower() in {'1','true','yes','on'}
REAL_TRADING_ENABLED=False
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('otc_m1')
IQ: Optional[IQ_Option]=None
PAIRS:list[str]=[]; excluded_until_refresh:set[str]=set(); last_refresh=0.0
last_candle:dict[str,int]={}; last_signal_candle:dict[str,int]={}
RUNNING=threading.Event()  # Arranca detenido; se activa desde Telegram.
TG_OFFSET=0
TG_LOCK=threading.Lock()


def tg(msg:str)->None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',data={'chat_id':str(TELEGRAM_CHAT_ID),'text':msg},timeout=6)
    except Exception as e: log.warning('Telegram: %s',e)

def tg_control_panel(chat_id: str | int | None = None, message: str = "🎛️ Control del bot OTC M1") -> None:
    """Send Telegram start/stop controls; only the configured chat is authorized."""
    if not TELEGRAM_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        return
    target = str(chat_id or TELEGRAM_CHAT_ID)
    state = "🟢 EN MARCHA" if RUNNING.is_set() else "⏸️ DETENIDO"
    keyboard = {"inline_keyboard": [[
        {"text": "▶️ INICIAR", "callback_data": "bot_start"},
        {"text": "⏹️ DETENER", "callback_data": "bot_stop"}
    ], [{"text": "ℹ️ ESTADO", "callback_data": "bot_status"}]]}
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": target, "text": f"{message}\nEstado: {state}",
                            "reply_markup": keyboard}, timeout=8)
    except Exception as e:
        log.warning("Telegram panel: %s", e)


def telegram_control_loop() -> None:
    """Long-poll Telegram updates and handle inline buttons plus /start, /stop, /status."""
    global TG_OFFSET
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado; control remoto deshabilitado.")
        return
    base=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    while True:
        try:
            response=requests.get(f"{base}/getUpdates", params={"offset":TG_OFFSET,"timeout":25}, timeout=32)
            response.raise_for_status()
            payload=response.json()
            if not payload.get("ok"):
                time.sleep(3); continue
            for update in payload.get("result",[]):
                TG_OFFSET=max(TG_OFFSET,int(update.get("update_id",0))+1)
                callback=update.get("callback_query")
                msg=update.get("message") or (callback or {}).get("message") or {}
                chat=msg.get("chat",{})
                chat_id=str(chat.get("id",""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    if callback:
                        requests.post(f"{base}/answerCallbackQuery",data={"callback_query_id":callback["id"],"text":"No autorizado"},timeout=5)
                    continue
                action=(callback or {}).get("data","")
                command=(msg.get("text","").strip().split() or [""])[0].lower()
                if action=="bot_start" or command=="/start":
                    RUNNING.set(); reply="▶️ BOT INICIADO. Comienza el análisis M1 y las órdenes demo según las señales."
                elif action=="bot_stop" or command=="/stop":
                    RUNNING.clear(); reply="⏹️ BOT DETENIDO. Se pausa el análisis y no se envían nuevas órdenes."
                elif action=="bot_status" or command=="/status":
                    reply="🟢 EN MARCHA" if RUNNING.is_set() else "⏸️ DETENIDO"
                    reply+=f"\nPares en catálogo: {len(PAIRS)}\nPares excluidos: {len(excluded_until_refresh)}"
                else:
                    continue
                if callback:
                    requests.post(f"{base}/answerCallbackQuery",data={"callback_query_id":callback["id"]},timeout=5)
                    tg_control_panel(chat_id, reply)
                else:
                    tg_control_panel(chat_id, reply)
                log.info("Control Telegram: %s",reply.replace("\n"," | "))
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 409:
                log.error("Telegram 409 Conflict: otro proceso está usando getUpdates con este token. "
                          "Deja una sola instancia activa en Railway y detén otros pollers.")
                time.sleep(15)
            else:
                log.warning("Telegram polling HTTP: %s", e)
                time.sleep(5)
        except Exception as e:
            log.warning("Telegram polling: %s",e); time.sleep(5)


def canonical(name:str)->str:
    return name.strip().upper().replace('_OTC','-OTC')

def active_catalog()->dict[str,int]:
    found={}
    try:
        init=IQ.get_all_init_v2() if IQ else {}
        for market in ('binary','turbo'):
            section=init.get(market,{}) if isinstance(init,dict) else {}
            actives=section.get('actives',{}) if isinstance(section,dict) else {}
            if isinstance(actives,dict):
                for aid,info in actives.items():
                    if not isinstance(info,dict): continue
                    name=canonical(str(info.get('name','')))
                    if name.endswith('-OTC'):
                        try: found[name]=int(aid)
                        except (TypeError,ValueError): pass
        for name,aid in getattr(OP_code,'ACTIVES',{}).items():
            n=canonical(str(name))
            if n.endswith('-OTC'): found.setdefault(n,int(aid))
    except Exception as e: log.warning('Catálogo: %s',e)
    return found

def binary_open_names()->set[str]:
    """Availability snapshot; order rejection is the final expiry-specific test."""
    out=set()
    try:
        data=IQ.get_all_open_time() if IQ else {}
        binary=data.get('binary',{}) if isinstance(data,dict) else {}
        for name,info in binary.items():
            if isinstance(info,dict) and info.get('open') is True:
                out.add(canonical(str(name)))
    except Exception as e: log.warning('Estado de apertura: %s',e)
    return out

def refresh_pairs(force=False)->None:
    global PAIRS,last_refresh,excluded_until_refresh
    now=time.time()
    if not force and now-last_refresh<REFRESH_SECONDS: return
    catalog=active_catalog(); opened=binary_open_names()
    # Only OTC names present in the live binary catalog and marked open.
    otc_names={p for p in catalog if p.endswith('-OTC')}
    new=sorted(p for p in otc_names if p in opened)
    if not new:
        log.warning("Catálogo sin coincidencias OTC: activos OTC=%d, abiertos Binary=%d. "
                    "Muestras activos=%s | abiertos=%s",
                    len(otc_names), len(opened), sorted(otc_names)[:8], sorted(opened)[:8])
    removed=set(PAIRS)-set(new)
    for p in removed: last_candle.pop(p,None); last_signal_candle.pop(p,None)
    PAIRS=new; excluded_until_refresh.clear(); last_refresh=now
    for p in PAIRS: OP_code.ACTIVES[p]=catalog[p]
    msg=('🔄 CATÁLOGO OTC ACTUALIZADO\n'
         f'Pares disponibles para análisis: {len(PAIRS)}\n'
         f'Revisión: cada {REFRESH_SECONDS//60} min\n'
         f'Expiración solicitada: {EXPIRATION} minuto\n'
         f'Importe demo: {AMOUNT:.2f} USD\n\n'+('\n'.join(PAIRS[:100]) if PAIRS else 'No hay pares OTC abiertos.'))
    log.info('Pares OTC disponibles: %s',len(PAIRS)); tg(msg)

def ensure_connection()->bool:
    if IQ is None:return False
    try:
        if IQ.check_connect():return True
    except Exception: pass
    try:
        r=IQ.connect(); return bool(r[0] if isinstance(r,tuple) else r)
    except Exception as e: log.warning('Reconexión: %s',e); return False

def candle_frame(pair:str)->pd.DataFrame:
    try:
        rows=IQ.get_candles(pair,M1,CANDLE_COUNT,time.time()) if IQ else []
        if not rows:return pd.DataFrame()
        df=pd.DataFrame(rows).rename(columns={'max':'high','min':'low'})
        needed=['from','open','high','low','close']
        if any(c not in df for c in needed):return pd.DataFrame()
        for c in needed:df[c]=pd.to_numeric(df[c],errors='coerce')
        df=df.dropna(subset=needed).sort_values('from').drop_duplicates('from')
        # Exclude the currently forming M1 candle.
        current=int(time.time()//M1)*M1
        return df[df['from']<current].reset_index(drop=True)
    except Exception as e: log.warning('%s candles: %s',pair,e); return pd.DataFrame()

def configure_demo()->bool:
    if not DEMO_ENABLED:return True
    try:
        IQ.change_balance('PRACTICE')
        mode=getattr(IQ,'get_balance_mode',lambda: 'PRACTICE')()
        if mode and str(mode).upper()!='PRACTICE':
            tg(f'⛔ Operación bloqueada: cuenta no PRACTICE ({mode})'); return False
        return True
    except Exception as e: log.error('Cuenta demo: %s',e); return False

def analyze_pair(pair:str)->None:
    if pair in excluded_until_refresh:return
    df=candle_frame(pair)
    if len(df)<WINDOW:return
    ts=int(df.iloc[-1]['from'])
    if last_candle.get(pair)==ts:return
    last_candle[pair]=ts
    result=analyze_market(df=df)
    pred=result.get('prediction','NO SIGNAL')
    if pred not in ('CALL','PUT'):
        log.info('%s | NO SIGNAL | %s',pair,result.get('prediction_reason',result.get('reason',''))); return
    # A candle may generate at most one order attempt.
    if last_signal_candle.get(pair)==ts:return
    last_signal_candle[pair]=ts
    reason=result.get('prediction_reason',result.get('reason',''))
    msg=(f'📊 SEÑAL OTC M1\nPar: {pair}\nDirección: {pred}\n'
         f'Calidad estructural: {result.get("prediction_quality","N/D")}\n'
         f'Expiración: 1 minuto\nMotivo: {reason}')
    tg(msg); log.info('%s | %s | %s',pair,pred,reason)
    if not DEMO_ENABLED:return
    # Last pre-order check. If 1-minute binary order is rejected, exclude this pair
    # from further analysis until the next catalog refresh.
    try:
        if not configure_demo():return
        ok,order=IQ.buy(AMOUNT,pair,pred.lower(),EXPIRATION)
        if not ok:
            excluded_until_refresh.add(pair)
            tg(f'🚫 PAR EXCLUIDO HASTA LA PRÓXIMA ACTUALIZACIÓN\n{pair}\nLa orden binaria de 1 minuto fue rechazada.\nRespuesta: {order}')
            log.warning('%s rechazado; excluido hasta refresh: %s',pair,order); return
        tg(f'✅ ORDEN DEMO ACEPTADA\nPar: {pair}\nDirección: {pred}\nImporte: {AMOUNT:.2f} USD\nExpiración: 1 minuto\nID: {order}\nCuenta: PRACTICE')
    except Exception as e:
        excluded_until_refresh.add(pair)
        log.exception('Orden %s: %s',pair,e)
        tg(f'🚫 {pair} excluido hasta la próxima actualización por error de orden: {e}')

def main()->None:
    global IQ
    if not all((IQ_EMAIL,IQ_PASSWORD)):
        log.error('FALTAN IQ_EMAIL e/o IQ_PASSWORD. Configura ambas variables en Railway > Variables; el proceso permanecerá activo para que el servicio no termine silenciosamente.')
        while not (os.getenv('IQ_EMAIL') and os.getenv('IQ_PASSWORD')):
            time.sleep(30)
        log.error('Credenciales detectadas después del arranque. Reinicia el servicio para iniciar la conexión.')
        return
    try:
        IQ=OTCOnlyIQOption(IQ_EMAIL,IQ_PASSWORD); log.info('Conectando con cliente OTC-only...'); ok,reason=IQ.connect()
        if not ok:raise ConnectionError(reason)
        if not configure_demo():return
        threading.Thread(target=telegram_control_loop,daemon=True,name='telegram-control').start()
        tg_control_panel(message='🤖 BOT OTC M1 CONECTADO\nCuenta: PRACTICE\nOperaciones reales: BLOQUEADAS')
        tg('⏸️ El bot inicia DETENIDO. Pulsa INICIAR en Telegram para comenzar.')
        refresh_pairs(force=True)
        while True:
            if not RUNNING.wait(timeout=0.5):
                continue
            if not ensure_connection():time.sleep(2);continue
            refresh_pairs()
            for pair in list(PAIRS):
                if not RUNNING.is_set(): break
                if pair in excluded_until_refresh:continue
                analyze_pair(pair)
                time.sleep(POLL)
            time.sleep(POLL)
    except KeyboardInterrupt: log.info('Detenido por usuario')
    except Exception as e:
        log.exception('Error fatal: %s',e); tg(f'❌ Error del bot: {type(e).__name__}: {e}')

if __name__=='__main__':main()
