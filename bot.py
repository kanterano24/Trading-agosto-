"""QUANT MODE - bot robusto para IQ Option OTC M1.
Analiza velas M1 cerradas y ejecuta exactamente la señal de strategy.py.
"""
import logging
import os
import time
import requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, analyze_market

PAIRS = ['EURUSD-OTC', 'EURJPY-OTC', 'EURGBP-OTC', 'GBPUSD-OTC', 'USDCHF-OTC']

TF = 60
COUNT = int(os.getenv('CANDLE_COUNT', '200'))
EXPIRATION = 1
AMOUNT = float(os.getenv('AMOUNT', '1'))
ENABLE_TRADES = os.getenv('ENABLE_TRADES', 'true').lower() in ('1', 'true', 'yes', 'si')

POLL = max(1.0, float(os.getenv('POLL_SECONDS', '2')))
COOLDOWN = max(0.0, float(os.getenv('TRADE_COOLDOWN', '60')))
BUY_RETRIES = max(1, int(os.getenv('BUY_RETRIES', '4')))
BUY_RETRY_SECONDS = max(0.5, float(os.getenv('BUY_RETRY_SECONDS', '2')))
RECONNECT_SECONDS = max(3.0, float(os.getenv('RECONNECT_SECONDS', '5')))
HEARTBEAT_SECONDS = max(30.0, float(os.getenv('HEARTBEAT_SECONDS', '60')))

EMAIL = os.getenv('IQ_EMAIL', '')
PASSWORD = os.getenv('IQ_PASSWORD', '')
TOKEN = os.getenv('TELEGRAM_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)

sess = requests.Session()
tg_last = 0.0


def tg(msg):
    global tg_last
    if not TOKEN or not CHAT:
        return False
    delay = 3.2 - (time.monotonic() - tg_last)
    if delay > 0:
        time.sleep(delay)
    try:
        r = sess.post(
            f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            data={'chat_id': CHAT, 'text': msg},
            timeout=15
        )
        if r.status_code == 429:
            logging.warning('Telegram 429; aviso omitido')
            tg_last = time.monotonic()
            return False
        r.raise_for_status()
        tg_last = time.monotonic()
        return bool(r.json().get('ok'))
    except Exception as e:
        logging.warning('Telegram: %s', e)
        return False


def connect():
    if not EMAIL or not PASSWORD:
        raise RuntimeError('Configura IQ_EMAIL e IQ_PASSWORD')

    logging.info('Conectando a IQ Option...')
    iq = IQ_Option(EMAIL, PASSWORD)
    ok, why = iq.connect()

    if not ok:
        raise RuntimeError(f'Conexión fallida: {why}')

    iq.change_balance('PRACTICE')
    try:
        iq.update_ACTIVES_OPCODE()
    except Exception:
        pass

    logging.info('Conectado a PRACTICE | operaciones=%s', ENABLE_TRADES)
    return iq


def ensure_connection(iq):
    try:
        if iq is not None and iq.check_connect():
            return iq
    except Exception as e:
        logging.warning('Comprobación de conexión falló: %s', e)

    while True:
        try:
            return connect()
        except Exception as e:
            logging.error('No se pudo reconectar: %s | reintento en %ss',
                          e, RECONNECT_SECONDS)
            time.sleep(RECONNECT_SECONDS)


def candles(iq, pair):
    now = int(iq.get_server_timestamp())
    raw = iq.get_candles(pair, TF, COUNT + 10, now) or []
    current = now - now % TF
    closed = [
        x for x in raw
        if int(x.get('from', 0)) + TF <= current
    ]
    return normalize_candles(closed)[-COUNT:]


def refresh_active_codes(iq):
    try:
        iq.update_ACTIVES_OPCODE()
        logging.info('Códigos de activos actualizados')
    except Exception as e:
        logging.warning('No se pudieron actualizar códigos: %s', e)


def binary_open(iq, pair):
    try:
        data = iq.get_all_open_time() or {}
        turbo = bool(data.get('turbo', {}).get(pair, {}).get('open', False))
        binary = bool(data.get('binary', {}).get(pair, {}).get('open', False))
        logging.info('%s disponibilidad | turbo=%s binary=%s',
                     pair, turbo, binary)
        return turbo or binary
    except Exception as e:
        logging.warning('%s no se pudo consultar disponibilidad: %s', pair, e)
        return False


def buy_when_available(iq, pair, direction):
    last_error = 'activo no disponible'

    for attempt in range(1, BUY_RETRIES + 1):
        if not binary_open(iq, pair):
            last_error = 'active is suspended / activo cerrado'
            logging.warning(
                '%s no disponible | intento %d/%d',
                pair, attempt, BUY_RETRIES
            )
            refresh_active_codes(iq)
            if attempt < BUY_RETRIES:
                time.sleep(BUY_RETRY_SECONDS)
            continue

        try:
            logging.info(
                'ENVIANDO ORDEN | %s | %s | intento %d/%d',
                pair, direction, attempt, BUY_RETRIES
            )
            ok, oid = iq.buy(
                AMOUNT,
                pair,
                direction.lower(),
                EXPIRATION
            )
        except Exception as e:
            ok, oid = False, str(e)

        if ok:
            logging.info(
                'ORDEN EJECUTADA | %s | %s | ID=%s',
                pair, direction, oid
            )
            return True, oid

        last_error = oid or 'respuesta vacía'
        text_error = str(last_error).lower()
        logging.warning(
            'Orden rechazada | %s | %s | %s',
            pair, direction, last_error
        )

        if 'suspended' in text_error or 'active is suspended' in text_error:
            refresh_active_codes(iq)
            if attempt < BUY_RETRIES:
                time.sleep(BUY_RETRY_SECONDS)
                continue

        break

    return False, last_error


def main():
    iq = None
    seen = {}
    last_trade = {}
    last_heartbeat = 0.0

    iq = ensure_connection(iq)

    tg(
        '🟢 QUANT MODE iniciado | PRACTICE | M1 | expiración 1 min | '
        + ('ÓRDENES HABILITADAS' if ENABLE_TRADES else 'SOLO ANÁLISIS')
    )

    logging.info(
        'Bot activo | pares=%s | amount=%s | expiración=%s min',
        len(PAIRS), AMOUNT, EXPIRATION
    )

    while True:
        try:
            iq = ensure_connection(iq)

            now_mono = time.monotonic()
            if now_mono - last_heartbeat >= HEARTBEAT_SECONDS:
                logging.info('HEARTBEAT | bot activo | analizando %d pares', len(PAIRS))
                last_heartbeat = now_mono

            for pair in PAIRS:
                try:
                    cs = candles(iq, pair)

                    if len(cs) < 30:
                        logging.warning(
                            '%s | historial insuficiente: %d/30',
                            pair, len(cs)
                        )
                        continue

                    stamp = cs[-1]['timestamp']

                    # Solo analiza una vez cada vela M1 cerrada.
                    if seen.get(pair) == stamp:
                        continue

                    seen[pair] = stamp

                    res = analyze_market(cs)
                    ctx = res['contexts']
                    stages = ', '.join(
                        k for k, v in res['stages'].items() if v
                    ) or 'ninguna'
                    ctxs = ' | '.join(
                        f'{n}:{ctx[n]["bias"]}'
                        for n in (2, 3, 5, 10)
                    )

                    logging.info(
                        '%s | señal=%s | %s',
                        pair, res['signal'], res['reason']
                    )

                    if res['signal'] not in ('CALL', 'PUT'):
                        continue

                    # La estrategia determina la dirección; no se invierte.
                    if (
                        ENABLE_TRADES
                        and time.time() - last_trade.get(pair, 0) >= COOLDOWN
                    ):
                        iq.change_balance('PRACTICE')
                        ok, oid = buy_when_available(
                            iq, pair, res['signal']
                        )

                        if ok:
                            last_trade[pair] = time.time()
                            tg(
                                f'🧪 ORDEN EJECUTADA\n'
                                f'{pair} → {res["signal"]}\n'
                                f'Expiración: {EXPIRATION} min\n'
                                f'Monto: {AMOUNT}\n'
                                f'ID: {oid}'
                            )
                        else:
                            # No consume cooldown si IQ Option la rechazó.
                            tg(
                                f'⚠️ ORDEN NO EJECUTADA\n'
                                f'{pair} señal {res["signal"]}\n'
                                f'Motivo: {oid}'
                            )

                    tg(
                        f'📈 {pair} M1\n'
                        f'Señal: {res["signal"]}\n'
                        f'Contexto: {ctxs}\n'
                        f'Etapas: {stages}\n'
                        f'Motivo: {res["reason"]}'
                    )

                except Exception as e:
                    logging.exception('Error en %s: %s', pair, e)

            time.sleep(POLL)

        except KeyboardInterrupt:
            logging.info('Bot detenido manualmente')
            return

        except Exception as e:
            # El proceso NO termina por una caída temporal de IQ Option.
            logging.exception(
                'Error del ciclo principal: %s | reconectando en %ss',
                e, RECONNECT_SECONDS
            )
            iq = None
            time.sleep(RECONNECT_SECONDS)


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.exception(
                'Fallo no controlado: %s | reinicio interno en %ss',
                e, RECONNECT_SECONDS
            )
            time.sleep(RECONNECT_SECONDS)
