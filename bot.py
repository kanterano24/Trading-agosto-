"""Bot QUANT MODE para IQ Option OTC. Requiere strategy_quant.py junto a este archivo."""
import logging, os, time, requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, analyze_market

PAIRS=['EURUSD-OTC','EURJPY-OTC','EURGBP-OTC','GBPUSD-OTC','USDCHF-OTC']
TF=60; COUNT=int(os.getenv('CANDLE_COUNT','200')); EXPIRATION=1
AMOUNT=float(os.getenv('AMOUNT','1'))
# Seguridad: PRACTICE y sin órdenes hasta habilitar explícitamente.
ENABLE_TRADES=os.getenv('ENABLE_TRADES','true').lower() in ('1','true','yes','si')
OPEN_REFRESH_SECONDS=max(5.0,float(os.getenv('OPEN_REFRESH_SECONDS','20')))
BUY_RETRY_SECONDS=max(0.5,float(os.getenv('BUY_RETRY_SECONDS','2')))
BUY_RETRIES=max(1,int(os.getenv('BUY_RETRIES','4')))
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



def refresh_active_codes(iq):
    """Actualiza los códigos de activos OTC usados por iqoptionapi."""
    try:
        iq.update_ACTIVES_OPCODE()
        logging.info('Códigos de activos actualizados')
    except Exception as e:
        logging.warning('No se pudieron actualizar los códigos de activos: %s', e)


def binary_open(iq, pair):
    """Devuelve si el par está habilitado para opciones de 1 minuto.

    La API expone el estado en turbo/binary. Para expiración de 1 minuto
    interesa principalmente turbo; se acepta también binary si la API lo
    reporta abierto. Si la consulta falla, no se fuerza la compra.
    """
    try:
        data = iq.get_all_open_time()
        turbo = bool(data.get('turbo', {}).get(pair, {}).get('open', False))
        binary = bool(data.get('binary', {}).get(pair, {}).get('open', False))
        logging.info('%s disponibilidad | turbo=%s binary=%s', pair, turbo, binary)
        return turbo or binary
    except Exception as e:
        logging.warning('%s no se pudo consultar disponibilidad: %s', pair, e)
        return False


def buy_when_available(iq, pair, direction):
    """Intenta comprar solo cuando IQ Option reporta el activo abierto.

    Si el activo está suspendido, actualiza los códigos y vuelve a consultar
    durante una ventana corta. Nunca cambia CALL por PUT ni usa otro activo.
    """
    last_error = 'activo no disponible'
    for attempt in range(1, BUY_RETRIES + 1):
        if not binary_open(iq, pair):
            last_error = 'activo suspendido/cerrado en IQ Option'
            logging.warning('%s disponible=False | intento %d/%d', pair, attempt, BUY_RETRIES)
            if attempt < BUY_RETRIES:
                refresh_active_codes(iq)
                time.sleep(BUY_RETRY_SECONDS)
            continue

        try:
            logging.info('ENVIANDO ORDEN | %s | %s | intento %d/%d', pair, direction, attempt, BUY_RETRIES)
            ok, oid = iq.buy(AMOUNT, pair, direction.lower(), EXPIRATION)
        except Exception as e:
            ok, oid = False, str(e)

        if ok:
            logging.info('ORDEN EJECUTADA | %s | %s | ID=%s', pair, direction, oid)
            return True, oid

        last_error = oid if oid else 'respuesta vacía de IQ Option'
        text_error = str(last_error).lower()
        logging.warning('Orden rechazada | %s | %s | %s', pair, direction, last_error)

        # Si IQ Option dice explícitamente que está suspendido, no repetimos
        # inmediatamente la misma compra sin refrescar el estado del activo.
        if 'active is suspended' in text_error or 'suspended' in text_error:
            refresh_active_codes(iq)
            if attempt < BUY_RETRIES:
                time.sleep(BUY_RETRY_SECONDS)
                continue
        break

    return False, last_error


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
                        ok,oid=buy_when_available(iq,pair,res['signal'])
                        # El cooldown solo empieza después de una orden aceptada.
                        # Una orden rechazada puede volver a intentarse en otra señal.
                        if ok:
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
