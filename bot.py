"""Bot QUANT MODE para IQ Option OTC. Requiere strategy_quant.py junto a este archivo."""
import logging, os, time, requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, analyze_market

PAIRS=['EURUSD-OTC','EURJPY-OTC','EURGBP-OTC','GBPAUD-OTC','GBPUSD-OTC','USDCHF-OTC']
TF=60; COUNT=int(os.getenv('CANDLE_COUNT','200')); EXPIRATION=1
AMOUNT=float(os.getenv('AMOUNT','1'))
# Seguridad: PRACTICE y sin órdenes hasta habilitar explícitamente.
ENABLE_TRADES=os.getenv('ENABLE_TRADES','false').lower() in ('1','true','yes','si')
EMAIL=os.getenv('IQ_EMAIL',''); PASSWORD=os.getenv('IQ_PASSWORD',''); TOKEN=os.getenv('TELEGRAM_TOKEN',''); CHAT=os.getenv('TELEGRAM_CHAT_ID','')
POLL=max(1.0,float(os.getenv('POLL_SECONDS','2'))); COOLDOWN=float(os.getenv('TRADE_COOLDOWN','60'))
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
sess=requests.Session(); tg_last=0.0

def tg(msg):
    global tg_last
    if not TOKEN or not CHAT:return False
    delay=3.2-(time.monotonic()-tg_last)
    if delay>0:time.sleep(delay)
    try:
        r=sess.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',data={'chat_id':CHAT,'text':msg},timeout=15)
        if r.status_code==429:
            logging.warning('Telegram 429; aviso omitido'); tg_last=time.monotonic(); return False
        r.raise_for_status(); tg_last=time.monotonic(); return bool(r.json().get('ok'))
    except Exception as e:logging.warning('Telegram: %s',e);return False

def connect():
    if not EMAIL or not PASSWORD:raise RuntimeError('Configura IQ_EMAIL e IQ_PASSWORD')
    iq=IQ_Option(EMAIL,PASSWORD); ok,why=iq.connect()
    if not ok:raise RuntimeError(f'Conexión fallida: {why}')
    iq.change_balance('PRACTICE'); logging.info('Conectado a PRACTICE');return iq

def candles(iq,pair):
    now=int(iq.get_server_timestamp()); raw=iq.get_candles(pair,TF,COUNT+10,now) or []; current=now-now%TF
    closed=[x for x in raw if int(x.get('from',0))+TF<=current]
    return normalize_candles(closed)[-COUNT:]

def main():
    iq=connect(); seen={}; last_trade={}; disabled=set()
    tg('🟢 QUANT MODE iniciado | PRACTICE | M1 | expiración 1 min | '+('órdenes habilitadas' if ENABLE_TRADES else 'solo análisis'))
    while True:
        if not iq.check_connect():iq=connect()
        for pair in PAIRS:
            if pair in disabled:continue
            try:
                cs=candles(iq,pair)
                if len(cs)<30:continue
                stamp=cs[-1]['timestamp']
                if seen.get(pair)==stamp:continue
                seen[pair]=stamp
                res=analyze_market(cs); ctx=res['contexts']; stages=', '.join(k for k,v in res['stages'].items() if v) or 'ninguna'
                ctxs=' | '.join(f'{n}:{ctx[n]["bias"]}' for n in (2,3,5,10))
                logging.info('%s señal=%s | %s',pair,res['signal'],res['reason'])
                if res['signal'] in ('CALL','PUT'):
                    # La orden sigue la señal: se elimina la inversión que venía perdiendo.
                    if ENABLE_TRADES and time.time()-last_trade.get(pair,0)>=COOLDOWN:
                        iq.change_balance('PRACTICE')
                        ok,oid=iq.buy(AMOUNT,pair,res['signal'].lower(),EXPIRATION)
                        last_trade[pair]=time.time()
                        tg(f'{"🧪 Orden enviada" if ok else "⚠️ Orden rechazada"}\n{pair} señal {res["signal"]} → orden {res["signal"]}\nID/respuesta: {oid}')
                    tg(f'📈 {pair} M1\nSeñal: {res["signal"]}\nContexto: {ctxs}\nEtapas: {stages}\nMotivo: {res["reason"]}')
            except Exception as e:
                msg=str(e)
                if 'not found on consts' in msg.lower() or ('asset' in msg.lower() and 'not found' in msg.lower()):
                    disabled.add(pair); logging.error('Par no reconocido, omitido: %s (%s)',pair,e)
                else:logging.exception('Error analizando %s',pair)
        time.sleep(POLL)

if __name__=='__main__':
    while True:
        try:main()
        except KeyboardInterrupt:break
        except Exception:logging.exception('Fallo principal; reintento en 10 s');time.sleep(10)
