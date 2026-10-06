"""Bot EURUSD-OTC M1: contexto 2/3/5/10 y secuencia de precio. PRACTICE solamente."""
import logging, os, time, requests
from iqoptionapi.stable_api import IQ_Option
from strategy import normalize_candles, analyze_market

TF = 60
COUNT = 200
EXPIRATION = 1
MAX_OTC_PAIRS = 20
PAIR_REFRESH_SECONDS = 15 * 60
AMOUNT = float(os.getenv('AMOUNT', '130'))
TRADES_ENABLED = os.getenv('ENABLE_TRADES', 'true').lower() in ('1', 'true', 'yes', 'si')
EMAIL = os.getenv('IQ_EMAIL', '')
PASSWORD = os.getenv('IQ_PASSWORD', '')
TOKEN = os.getenv('TELEGRAM_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
POLL = max(1, float(os.getenv('POLL_SECONDS', '2')))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
sess = requests.Session()
tg_last = 0.0
telegram_offset = None

# Evita el hilo digital que en algunas versiones falla al indexar None.
def safe_underlying(self):
    return {'underlying': []}

def no_digital_open(self):
    return None

IQ_Option.get_digital_underlying_list_data = safe_underlying
if hasattr(IQ_Option, '_get_digital_open'):
    IQ_Option._get_digital_open = no_digital_open


def tg_api(method, data=None):
    if not TOKEN:
        return None
    r = sess.post(
        f'https://api.telegram.org/bot{TOKEN}/{method}',
        data=data or {},
        timeout=15,
    )
    r.raise_for_status()
    result = r.json()
    if not result.get('ok'):
        raise RuntimeError(str(result))
    return result.get('result')


def tg(msg, reply_markup=None):
    global tg_last
    if not TOKEN or not CHAT:
        return False
    try:
        wait=1.2-(time.monotonic()-tg_last)
        if wait>0:
            time.sleep(wait)
        data={'chat_id':CHAT,'text':msg}
        if reply_markup is not None:
            import json
            data['reply_markup']=json.dumps(reply_markup, ensure_ascii=False)
        r=sess.post(
            f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            data=data,
            timeout=15
        )
        r.raise_for_status()
        result=r.json()
        if not result.get('ok'):
            raise RuntimeError(str(result))
        tg_last=time.monotonic()
        return True
    except Exception:
        logging.exception('Error Telegram')
        return False

def connect():
    if not EMAIL or not PASSWORD:
        raise RuntimeError('Faltan IQ_EMAIL/IQ_PASSWORD en Railway')
    iq = IQ_Option(EMAIL, PASSWORD)
    ok, why = iq.connect()
    if not ok:
        raise RuntimeError(f'Conexión fallida: {why}')
    iq.change_balance('PRACTICE')
    logging.info('Conectado a PRACTICE')
    return iq


def connect_retry():
    delay = 5
    while True:
        try:
            return connect()
        except Exception as e:
            logging.exception('No conectó; reintento en %ss', delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def discover_pairs(iq):
    """Devuelve hasta 20 pares BINARY OTC que estén abiertos."""
    try:
        opened = iq.get_all_open_time() or {}
        binary = opened.get('binary') or {}
        available = []
        for pair, info in binary.items():
            if not pair.endswith('-OTC'):
                continue
            if not isinstance(info, dict):
                continue
            if info.get('open') is True or info.get('active') is True:
                available.append(pair)
        return sorted(set(available))[:MAX_OTC_PAIRS]
    except Exception:
        logging.exception('No se pudieron consultar los pares OTC abiertos')
        return []


def get_candles(iq, pair):
    try:
        now = int(iq.get_server_timestamp())
    except Exception:
        now = int(time.time())
    raw = iq.get_candles(pair, TF, COUNT + 20, now) or []
    minute = now - now % TF
    closed = []
    for c in raw:
        try:
            start = int(c.get('from', c.get('at', 0)))
            if start > 0 and start + TF <= minute:
                closed.append(c)
        except (TypeError, ValueError):
            pass
    return normalize_candles(closed)[-COUNT:], now


def status_text(pairs, selected_pair, enabled):
    selected = 'TODOS LOS 20' if selected_pair is None else selected_pair
    return (
        '📊 ESTADO DEL BOT\n'
        f'Estado: {"🟢 ACTIVO" if enabled else "🔴 DETENIDO"}\n'
        f'Pares disponibles: {len(pairs)}/{MAX_OTC_PAIRS}\n'
        f'Par seleccionado: {selected}\n'
        f'Expiración: {EXPIRATION} minuto\n'
        'Mercado: BINARY OTC\n'
        'Cuenta: PRACTICE'
    )


def process_telegram(pairs, selected_pair, enabled):
    """Procesa botones y devuelve (selected_pair, enabled)."""
    global telegram_offset
    if not TOKEN or not CHAT:
        return selected_pair, enabled
    try:
        params = {'timeout': 0, 'allowed_updates': '["callback_query","message"]'}
        if telegram_offset is not None:
            params['offset'] = telegram_offset
        updates = tg_api('getUpdates', params) or []
        for update in updates:
            telegram_offset = update.get('update_id', telegram_offset) + 1
            callback = update.get('callback_query')
            message = update.get('message')

            if callback:
                callback_chat = str(callback.get('message', {}).get('chat', {}).get('id', ''))
                if callback_chat != str(CHAT):
                    continue
                data = callback.get('data', '')
                tg_api('answerCallbackQuery', {'callback_query_id': callback.get('id', '')})

                if data == 'bot_start':
                    enabled = True
                    tg('▶️ Bot iniciado.\nOperaciones habilitadas.', tg_keyboard())
                elif data == 'bot_stop':
                    enabled = False
                    tg('⏹ Bot detenido.\nNo ejecutará nuevas entradas.', tg_keyboard())
                elif data == 'bot_status':
                    tg(status_text(pairs, selected_pair, enabled), tg_keyboard())
                elif data == 'choose_pair':
                    if pairs:
                        tg('📋 Selecciona el par que quieres analizar y operar:', pair_keyboard(pairs))
                    else:
                        tg('⚠️ No hay pares OTC disponibles en este momento.', tg_keyboard())
                elif data == 'main_menu':
                    tg('🎛 PANEL DEL BOT', tg_keyboard())
                elif data.startswith('pair:'):
                    value = data[5:]
                    selected_pair = None if value == 'ALL' else value
                    tg(
                        f'✅ Par seleccionado: {"TODOS LOS 20" if selected_pair is None else selected_pair}\n'
                        f'Expiración: {EXPIRATION} minuto.',
                        tg_keyboard(),
                    )

            elif message:
                chat_id = str(message.get('chat', {}).get('id', ''))
                if chat_id != str(CHAT):
                    continue
                command = (message.get('text') or '').strip().lower()
                if command == '/panel':
                    tg('🎛 PANEL DEL BOT', tg_keyboard())
                elif command == '/pares':
                    tg('📋 Selecciona el par:', pair_keyboard(pairs) if pairs else tg_keyboard())
                elif command == '/status':
                    tg(status_text(pairs, selected_pair, enabled), tg_keyboard())
                elif command == '/start':
                    tg('🎛 PANEL DEL BOT', tg_keyboard())
        return selected_pair, enabled
    except Exception:
        logging.exception('Error procesando Telegram')
        return selected_pair, enabled


def main():
    iq = connect_retry()
    pairs = []
    selected_pair = None
    last_candle = {}
    last_error = 0.0
    refresh = 0.0
    enabled = TRADES_ENABLED

    # Primer descubrimiento y luego actualización cada 15 minutos.
    pairs = discover_pairs(iq)
    refresh = time.monotonic()
    logging.info('Pares OTC cargados=%d | %s', len(pairs), ', '.join(pairs))

    # El panel se puede abrir con /panel o /start; no se envían avisos de análisis.
    if TOKEN and CHAT:
        tg('🎛 PANEL DEL BOT\nSelecciona una opción:', tg_keyboard())

    while True:
        try:
            if not iq.check_connect():
                iq = connect_retry()
                pairs = []
                refresh = 0.0

            selected_pair, enabled = process_telegram(pairs, selected_pair, enabled)

            mono = time.monotonic()
            if not pairs or mono - refresh >= PAIR_REFRESH_SECONDS:
                new = discover_pairs(iq)
                if new:
                    # Si el par elegido dejó de estar disponible, vuelve a TODOS LOS 20.
                    if selected_pair is not None and selected_pair not in new:
                        selected_pair = None
                    pairs = new
                    refresh = mono
                    logging.info('Pares OTC actualizados=%d | %s', len(pairs), ', '.join(pairs))
                else:
                    logging.warning('No se detectaron pares OTC abiertos; se reintentará.')
                    refresh = mono

            active_pairs = [selected_pair] if selected_pair in pairs else pairs
            for pair in active_pairs:
                try:
                    cs, now = get_candles(iq, pair)
                    if len(cs) < COUNT:
                        logging.info('%s esperando historial %d/%d', pair, len(cs), COUNT)
                        continue

                    stamp = cs[-1]['timestamp']
                    if stamp == last_candle.get(pair):
                        continue
                    last_candle[pair] = stamp

                    res = analyze_market(cs)
                    logging.info(
                        '%s señal=%s sesgo=%s CALL=%s PUT=%s | %s',
                        pair, res['signal'], res['bias'], res['call_score'],
                        res['put_score'], res['reason']
                    )

                    # EXPIRATION permanece fijo en 1 minuto y solo se opera BINARY OTC.
                    if res['signal'] in ('CALL', 'PUT') and enabled:
                        iq.change_balance('PRACTICE')
                        inverse_signal = 'PUT' if res['signal'] == 'CALL' else 'CALL'
                        ok, oid = iq.buy(AMOUNT, pair, inverse_signal.lower(), EXPIRATION)
                        logging.info(
                            '%s señal=%s | orden invertida=%s | ok=%s | respuesta=%s',
                            pair, res['signal'], inverse_signal, ok, oid
                        )
                        if ok:
                            tg(
                                f'🟢 ORDEN EJECUTADA\n'
                                f'Par: {pair}\n'
                                f'Señal: {res["signal"]} → orden: {inverse_signal}\n'
                                f'ID: {oid}'
                            )
                except Exception as e:
                    logging.exception('Error analizando %s', pair)
                    if time.monotonic() - last_error > 60:
                        last_error = time.monotonic()

        except Exception as e:
            logging.exception('Error de ciclo')
            if time.monotonic() - last_error > 60:
                last_error = time.monotonic()
            try:
                if not iq.check_connect():
                    iq = connect_retry()
            except Exception:
                iq = connect_retry()
        time.sleep(POLL)


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception('Fallo principal; reinicio en 10s')
            time.sleep(10)
