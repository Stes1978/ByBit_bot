# strategy.py

import time
import pandas as pd
import json
import os
from .logger import add_log, log_trade_to_file, fmt
from engine.persistence import save_open_orders
from engine.logger import trace_calc

def get_pos_filepath(symbol):
    coin = symbol.replace('USDT', '').lower()
    return f"open_positions_{coin}.json"

def init_strategy_state(state):
    defaults = {
        'strat_orders': [], 'strat_last_buy_price': None, 'strat_waiting_for_reversal': False,
        'strat_cascade_active': False, 'strat_total_realized_pnl': 0.0, 'strat_prev_close': None,
        'strat_last_candle_idx': -1, 'strat_exits_this_candle': False, 'last_buy_candle_idx': -1,
        'strat_current_trend': "+", 'strat_targets_frozen': False, 'strat_trend_pct': 0.0,
        'strat_plan_price': None, 'strat_prev_open_count': 0, 'signal_history': [],
        'next_signal_num': 1, 'strat_waiting_for_two_steps': False,
        'strat_entry_made_this_candle': False, 'strat_start_price_frozen': False
    }
    for k, v in defaults.items():
        if k not in state: state[k] = v
    for o in state.get('strat_orders', []):
        if not isinstance(o, dict): continue
        o.setdefault('plan_exit', None)
        o.setdefault('target_price', None)
        if not o.get('id'): o['id'] = f"restored_{int(time.time())}"
        if 'target_price' not in o: o['target_price'] = None

def load_open_positions_from_json(filepath, state):
    if not os.path.exists(filepath):
        add_log(f"⚠️ Файл {filepath} не найден", state=state)
        return False
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            state['strat_total_realized_pnl'] = data.get('total_realized_pnl', 0.0)
            loaded = data.get('orders', [])
        else:
            loaded = data if isinstance(data, list) else []
        if not loaded: return False

        existing_ids = {o.get('order_id') for o in state.get('strat_orders', []) if o.get('order_id')}
        merged = 0
        for o in loaded:
            if o.get('order_id') in existing_ids: continue
            o['status'] = 'open'
            o.setdefault('exit_logic', 'breakeven')
            o.setdefault('target_price', None)
            o.setdefault('plan_exit', None)
            o.setdefault('exit_allowed', False)
            o.setdefault('is_new', False)
            o.setdefault('up_profit_flag', False)
            o.setdefault('cascade_4_flag', False)
            o['frozen_be_price'] = round(o['entry_price'] + state.get('strat_breakeven_buffer', 0.05), 2)
            state['strat_orders'].append(o)
            merged += 1

        if merged:
            add_log(f"✅ Загружено {merged} позиций + PnL={state.get('strat_total_realized_pnl',0):.2f}$", state=state)
            recalculate_targets_v2(state, state['strat_orders'], state.get('strat_grid_step', 0.15), state.get('strat_breakeven_buffer', 0.05))
            return True
        return False
    except Exception as e:
        add_log(f"❌ Ошибка чтения JSON: {e}", state=state)
        return False

def save_strategy_positions(symbol, state):
    filepath = f"open_positions_{symbol.replace('USDT', '').lower()}.json"
    data = {
        'symbol': symbol,
        'last_updated': pd.Timestamp.now().isoformat(),
        'total_realized_pnl': state.get('strat_total_realized_pnl', 0.0),
        'orders': state.get('strat_orders', [])
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)

def recalculate_targets_v2(state, orders, grid_step, breakeven_buffer):
    """
    Расчёт целевых цен выхода для открытых ордеров.
    """
    open_orders = [o for o in orders if o.get('status') == 'open']
    if not open_orders: return
    
    state['max_sig_num'] = max(o.get('sig_num', 0) for o in open_orders)
    unique_entries = sorted(list(set(round(o['entry_price'], 4) for o in open_orders)))
    current_n = len(unique_entries)
    prev_n = state.get('strat_prev_open_count', 0)

    if current_n < prev_n:
        state['strat_targets_frozen'] = True
    elif current_n > prev_n:
        state['strat_targets_frozen'] = False
    state['strat_prev_open_count'] = current_n

    if state.get('strat_targets_frozen', False): return

    for o in open_orders:
        if o.get('exit_logic') == 'breakeven':
            o['target_price'] = calc_breakeven_exit(o, state, state)
        elif o.get('exit_logic') == 'cascade':
            o['target_price'] = None
            o['plan_exit'] = None
    
    for o in open_orders:
        if o.get('exit_logic') == 'cascade':
            o['plan_exit'] = None
            o['target_price'] = None
            o['exit_allowed'] = False

def setup_symbol_context(state, symbol, current_price=None):
    coin = symbol.replace('USDT', '').lower()
    state['strat_orders'] = []
    state['strat_total_realized_pnl'] = 0.0
    state['strat_last_buy_price'] = None
    state['strat_waiting_for_reversal'] = False
    state['strat_bottom_price'] = None
    state['strat_entry_made_this_candle'] = False
    state['strat_last_candle_idx'] = -1
    state['last_entry_candle_ts'] = None
    state['strat_prev_open_count'] = 0

    filepath = f"open_positions_{coin}.json"
    load_open_positions_from_json(filepath, state)

    if not state.get('strat_orders'):
        base = current_price or state.get(f'start_price_{coin.upper()}')
        if base:
            state[f'start_price_{coin.upper()}'] = base
            state['strat_last_buy_price'] = base
            add_log(f"🎯 База инициализирована по реальной цене: ${base:.2f}", state=state)

def execute_long_entry(state, price, symbol, current_idx=-1, timestamp=None):
    entry_p = float(price)
    current_time_val = timestamp if timestamp is not None else pd.Timestamp.now()
    current_ts_seconds = current_time_val.timestamp() if hasattr(current_time_val, 'timestamp') else time.time()

    base_size = state.get('strat_fixed_pos_size', 5.0)
    open_orders = [o for o in state.get('strat_orders', []) if o.get('status') == 'open']
    current_p = state.get('last_known_price', entry_p)
    realized_pnl = state.get('strat_total_realized_pnl', 0.0)
    open_pnl = sum((current_p - o['entry_price']) * (base_size / o['entry_price']) for o in open_orders)
    total_pnl = realized_pnl + open_pnl

    if total_pnl < 0:
        vol_mult = 1
        logics_to_create = ['breakeven']
    else:
        vol_mult = 2
        logics_to_create = ['breakeven', 'cascade']

    actual_size = base_size * vol_mult

    if state.get('trading_mode') == "real":
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(testnet=False, api_key=state['api_key'], api_secret=state['api_secret'], recv_window=8000, timeout=10)
            for logic in (['breakeven'] if total_pnl < 0 else ['breakeven', 'cascade']):
                resp = session.place_order(
                    category="spot", symbol=symbol, side="Buy", orderType="Market",
                    qty=str(actual_size), marketUnit="quoteCoin"
                )
                oid = resp.get('result', {}).get('orderId')
                add_log(f"🚀 API {logic.upper()}: Куплено ${actual_size} | ID: {oid}", state=state)
        except Exception as e:
            add_log(f"❌ Ошибка API при покупке: {e}", state=state)
            return

    if 'strat_orders' not in state or not isinstance(state['strat_orders'], list):
        state['strat_orders'] = []

    sig_num = state.get('next_signal_num', 1)
    be_val = round(entry_p + state.get('strat_breakeven_buffer', 0.05), 2)

    coin_upper = symbol.replace('USDT', '').upper()
    anchor = state.get(f'start_price_{coin_upper}')
    if anchor is None:
        anchor = entry_p
    frozen_base = anchor

    frozen_plan_buy = price

    print(f"DEBUG: entry_p={entry_p}, anchor={anchor}, frozen_base={frozen_base}")

    for logic in ['breakeven', 'cascade']:
        state['strat_orders'].append({
            'entry_price': entry_p,
            'exit_logic': logic,
            'status': 'open',
            'sig_num': sig_num,
            'target_price': None,
            'plan_exit': None,
            'order_id': f"sim_{logic}_{int(current_ts_seconds)}_{len(state['strat_orders'])}",
            'pnl': 0.0,
            'exit_allowed': False,
            'is_new': True,
            'created_at': current_ts_seconds,
            'timestamp': current_time_val,
            'entry_timestamp': current_ts_seconds,
            'candle_idx': current_idx,
            'frozen_be_price': be_val,
            'is_last_step': False,
            'volume_mult': vol_mult,
            'frozen_start_price': frozen_base,
            'frozen_plan_buy': frozen_plan_buy,    
        })

    state['next_signal_num'] = sig_num + 1
    state['strat_start_price_frozen'] = True
    state['strat_first_entry_frozen'] = True
    
    add_log(f"✅ Ордера зафиксированы: ${entry_p:.2f} | Сигнал #{sig_num} | Физ. объём: {actual_size}$", state=state)
    recalculate_targets_v2(state, state['strat_orders'], state.get('strat_grid_step', 0.15), state.get('strat_breakeven_buffer', 0.05))
    save_open_orders(state)

def run_strategy_cycle(state, df, symbol, api_key, api_secret, is_simulation=False):
    if df is None or len(df) < 2:
        return
    init_strategy_state(state)
    orders = state['strat_orders']
    for o in orders:
        if not isinstance(o, dict):
            continue
        is_frozen = o.get('plan_exit_frozen', False)
        for k in ['plan_exit', 'target_price', 'exit_allowed', 'is_new', 'up_profit_flag', 'cascade_4_flag']:
            if is_frozen and k == 'plan_exit':
                continue
            if k not in o or o[k] is None:
                o[k] = None if k in ['plan_exit', 'target_price'] else False

    grid_step = state['strat_grid_step']
    breakeven_buffer = state.get('strat_breakeven_buffer', 0.05)
    volume_usdt = state.get('strat_fixed_pos_size', 5.0)
    last_closed_idx = len(df) - 2

    if state.get('strat_last_candle_idx', -1) >= last_closed_idx:
        return
    state['strat_entry_made_this_candle'] = False
    row = df.iloc[last_closed_idx]
    open_p, close_p, low_p, high_p = float(row['open']), float(row['close']), float(row['low']), float(row['high'])

    # Исправлена ошибка синтаксиса (добвлено 'o for o in')
    open_orders = [o for o in orders if isinstance(o, dict) and o.get('status') == 'open']
    if not open_orders:
        state['strat_last_candle_idx'] = last_closed_idx
        return

    # 1️⃣ ПРОВЕРКА ПРОДАЖИ
    for o in orders:
        if not isinstance(o, dict) or o.get('status') != 'open':
            continue
        plan_exit = o.get('plan_exit')
        if plan_exit is None:
            continue

        if low_p <= plan_exit:
            add_log(f"📉 [EXIT TRIGGER] #{o.get('sig_num')} {o.get('exit_logic')} | Low: {low_p:.2f} <= Plan: {plan_exit:.2f}", state=state)
            if not is_simulation and state.get('trading_mode') == "real":
                try:
                    from pybit.unified_trading import HTTP
                    session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret, recv_window=8000, timeout=10)
                    qty_coins = round(volume_usdt / o['entry_price'], 3)
                    resp = session.place_order(category="spot", symbol=symbol, side="Sell", orderType="Market", qty=str(qty_coins))
                    add_log(f"✅ API Sell OK! OrderID: {resp.get('result', {}).get('orderId')}", state=state)
                except Exception as e:
                    add_log(f"❌ API Sell FAILED: {type(e).__name__}: {str(e)}", state=state)
                    continue

            o['status'] = 'closed'
            o['exit_price'] = plan_exit if is_simulation else close_p
            o['exit_candle_idx'] = last_closed_idx
            o['exit_timestamp'] = row.get('timestamp', pd.Timestamp.now())
            trade_pnl = (o['exit_price'] - o['entry_price']) * (volume_usdt / o['entry_price'])
            o['pnl'] = trade_pnl
            state['strat_total_realized_pnl'] += trade_pnl
            add_log(f"📉 EXIT {o['exit_logic'].upper()} | Price: {o['exit_price']:.2f} | PnL: {trade_pnl:+.2f}$", state=state)
            log_trade_to_file({
                'datetime': pd.Timestamp.now(), 'action': 'SELL', 'symbol': symbol,
                'price': o['exit_price'], 'amount_usd': volume_usdt, 'order_id': o.get('order_id'), 'status': 'closed'
            })
            state['strat_exits_this_candle'] = True

    # 2️⃣ АКТИВАЦИЯ НОВЫХ ПЛАНОВ
    open_orders_now = [o for o in orders if isinstance(o, dict) and o.get('status') == 'open']
    max_entry_now = max((o['entry_price'] for o in open_orders_now), default=0)
    max_sig_now = max((o.get('sig_num', 0) for o in open_orders_now), default=0)

    for o in orders:
        if not isinstance(o, dict) or o.get('status') != 'open':
            continue
        if o.get('exit_logic') == 'cascade':
            o['plan_exit'] = None
            o['target_price'] = None
            o['plan_exit_frozen'] = False
            o['exit_allowed'] = False
            continue
        
        if o.get('exit_logic') != 'breakeven':
            continue
        if o.get('plan_exit_frozen', False):
            continue

        entry_p = o.get('entry_price')
        if entry_p is None:
            continue

        is_max_price = (entry_p == max_entry_now)
        is_last_purchase = (o.get('sig_num') == max_sig_now)
        if is_max_price and is_last_purchase:
            o['plan_exit'] = None
            o['plan_exit_frozen'] = False
            o['exit_allowed'] = False
            continue

        if close_p >= entry_p + grid_step:
            target = round(entry_p + breakeven_buffer, 2)
            o['plan_exit'] = target
            o['target_price'] = target
            o['plan_exit_frozen'] = True
            o['exit_allowed'] = True
            add_log(f"🔒 PLAN EXIT #{o.get('sig_num')}: {target:.2f} (вход {entry_p:.2f} + буфер {breakeven_buffer}) | +Шаг выполнен", state=state)
        else:
            o['plan_exit'] = None
            o['target_price'] = None
            o['plan_exit_frozen'] = False
            o['exit_allowed'] = False

    state['strat_last_candle_idx'] = last_closed_idx

def process_entry_on_candle_close(state, closed_candle, symbol):
    if closed_candle is None: return
    candle_ts = closed_candle.get('timestamp')
    if state.get('last_entry_candle_ts') != candle_ts:
        state['strat_entry_made_this_candle'] = False
        state['last_entry_candle_ts'] = candle_ts

    close_p = float(closed_candle['close'])
    grid_step = float(state['strat_grid_step'])
    rev_threshold = float(state.get('strat_reversal_threshold', 10.0))
    coin = symbol.replace('USDT', '').upper()

    start_price = state.get(f'start_price_{coin}')
    if start_price is None:
        start_price = close_p
        state[f'start_price_{coin}'] = start_price
        state['strat_last_buy_price'] = start_price
    elif close_p < start_price:
        start_price = close_p
        state[f'start_price_{coin}'] = start_price
        state['strat_last_buy_price'] = start_price

    rev_limit = start_price * (1 - rev_threshold / 100)
    
    # --- ЛОГИКА РАЗВОРОТА (2 ШАГА) ---
    if close_p <= rev_limit and not state.get('strat_waiting_for_reversal', False):
        state['strat_waiting_for_reversal'] = True
        state['strat_bottom_price'] = close_p
        add_log(f"📉 Просадка! Дно зафиксировано: ${close_p:.2f}. Ждём отскок +{2*grid_step}", state=state)
        return

    enter_price = None
    
    if state.get('strat_waiting_for_reversal', False):
        # Мы в режиме ожидания разворота после просадки
        # ... внутри if state.get('strat_waiting_for_reversal', False):
        bottom = state.get('strat_bottom_price', start_price)
        
        # Обновляем дно, если цена пошла еще ниже
        if close_p < bottom:
            state['strat_bottom_price'] = close_p
        
        # Вход только при пробое Дна + 2 ШАГА
        elif close_p >= bottom + (2 * grid_step):
            enter_price = bottom + (2 * grid_step)
            state['strat_waiting_for_reversal'] = False
            add_log(f"🚀 РАЗВОРОТ! Вход по {enter_price:.2f} (Дно {bottom:.2f} + 2 шага)", state=state)

    # --- ЛОГИКА ТРЕНДА / БОКОВИКА (1 ШАГ ОТ МАКСИМУМА) ---
    else:
        # ------ ИСПРАВЛЕННАЯ ЛОГИКА ПОСЛЕ ПРОСАДКИ ------
        open_ords = [o for o in state.get('strat_orders', []) if o.get('status') == 'open']
        
        # Находим минимальную цену входа среди открытых, чтобы понять контекст
        min_entry_open = min((o['entry_price'] for o in open_ords), default=float('inf'))
        max_entry_open = max((o['entry_price'] for o in open_ords), default=0)

        # Сценарий А: Якорь (минимум цены) обновился и стал ниже всех открытых позиций (или позиций нет)
        # В этом случае следующая покупка должна быть строго через 2 шага от НОВОГО дна (start_price)
        if not open_ords or start_price < min_entry_open - 0.01:
            plan_price = start_price + (2 * grid_step)
            add_log(f"📉 Просадка! Новое дно {start_price:.2f}. План входа: +2 шага = {plan_price:.2f}", state=state)
        
        # Сценарий Б: Мы растем от последнего входа (обычный тренд)
        # Покупаем через 1 шаг от последней совершенной покупки
        else:
            last_buy = state.get('strat_last_buy_price', max_entry_open)
            plan_price = last_buy + grid_step
            add_log(f"📈 Тренд. Последняя покупка {last_buy:.2f}. План входа: +1 шаг = {plan_price:.2f}", state=state)

        # 🛡 ПРОВЕРКА НА ПОВТОРЯЕМОСТЬ (НАЛОЖЕНИЕ С ОТКРЫТЫМИ ПОЗИЦИЯМИ)
        # Если план попадает в зону существующего входа, последовательно сдвигаем его вверх
        tolerance = grid_step * 0.5
        shifts = 0
        while any(abs(plan_price - o['entry_price']) <= tolerance for o in open_ords) and shifts < 10:
            plan_price += grid_step
            shifts += 1
        if shifts > 0:
            add_log(f"⚠️ Наложение! План сдвинут на {shifts} шаг(ов) до {plan_price:.2f}", state=state)

        # Исполняем только если цена закрытия пробила скорректированный план
        if close_p >= plan_price:
            enter_price = plan_price
            add_log(f"📈 Вход по {enter_price:.2f}", state=state)

    # ИСПОЛНЕНИЕ ОРДЕРА
    if enter_price and not state.get('strat_entry_made_this_candle', False):
        last_idx = state.get('strat_last_candle_idx', -1)
        execute_long_entry(state, enter_price, symbol, current_idx=last_idx, timestamp=candle_ts)
        
        # Обновляем последнюю цену покупки и якорь
        state['strat_last_buy_price'] = enter_price
        state[f'start_price_{coin}'] = enter_price
        print(f"🔁 ANCHOR FORCED UPDATE: {coin} -> {enter_price}")
        
        state['strat_entry_made_this_candle'] = True

def calc_breakeven_exit(order, state, config):
    buffer = (config.get('strat_breakeven_buffer') or state.get('strat_breakeven_buffer')) or 5.0
    if order.get('entry_price'):
        plan = round(order['entry_price'] + buffer, 2)
        order['frozen_plan_exit'] = plan
        return plan
    return None

def calc_cascade_exit(state, orders, grid_step):
    open_cascade = [o for o in orders if o.get('status') == 'open' and o.get('exit_logic') == 'cascade']
    if not open_cascade: return
    prev_n = state.get('strat_prev_cascade_count', 0)
    curr_n = len(open_cascade)
    state['strat_prev_cascade_count'] = curr_n
    if curr_n < prev_n:
        state['strat_targets_frozen'] = True
    elif curr_n >= 4 and curr_n > prev_n:
        state['strat_targets_frozen'] = False
    if state.get('strat_targets_frozen', False): return

    entries = [o['entry_price'] for o in open_cascade]
    avg_price = sum(entries) / curr_n
    highest = max(entries)
    cascade_step = (highest - avg_price) / (curr_n - 1) if curr_n > 1 else grid_step
    sorted_orders = sorted(open_cascade, key=lambda x: x['entry_price'])
    max_sig = max(o.get('sig_num', 0) for o in open_cascade)

    for rank, o in enumerate(sorted_orders):
        is_top = (o['entry_price'] == highest)
        is_last = (o.get('sig_num') == max_sig)
        if is_top or is_last:
            o['target_price'] = None
            o['plan_exit'] = None
            continue
        target = round(avg_price + (cascade_step * rank), 2)
        if o.get('frozen_cascade_target') is not None:
            o['target_price'] = o['frozen_cascade_target']
        else:
            o['frozen_cascade_target'] = target
            o['target_price'] = target