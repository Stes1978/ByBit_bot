import pandas as pd
from engine.logger import trace_calc

def create_strategy_table_dynamic(state, symbol, current_price, orders, strategy_state, max_signals=None, buffer_rows=40):
    """
    Исправлена логика расчета "План Покупки" для устранения провалов в сетке при быстром росте.
    """

    def fmt(val):
        if isinstance(val, (int, float)):
            return f"{val:.2f}".replace('.', ',')
        return str(val) if val is not None else "—"

    coin = symbol.replace('USDT', '').upper()
    start_price = strategy_state.get(f'start_price_{coin}') or strategy_state.get('strat_last_buy_price') or current_price
    grid_step = state.get('strat_grid_step', 0.15)
    reversal_threshold = state.get('strat_reversal_threshold', 10.0)
    position_size = state.get('strat_fixed_pos_size', 5.0)
    breakeven_buffer = state.get('strat_breakeven_buffer', 5.0)

    realized = state.get('strat_total_realized_pnl', 0.0)
    open_pnl = sum(
        (current_price - o['entry_price']) * (position_size / o['entry_price'])
        for o in state.get('strat_orders', [])
        if isinstance(o, dict) and o.get('status') == 'open' and 'entry_price' in o
    )
    protection_mode = (realized + open_pnl) < 0

    orders_list = state.get('strat_orders', []) or []
    safe_orders = [o for o in orders_list if isinstance(o, dict)]
    num_executed_signals = len(safe_orders) // 2
    display_signals = max(num_executed_signals + buffer_rows, max_signals or 10)

    rows = []
    total_accumulated_pnl = 0.0
    open_order_counter = 0

    open_orders_global = [o for o in safe_orders if o.get('status') == 'open']
    num_open_positions_global = len(open_orders_global)
    
    # 🔥 КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Находим максимальную цену входа среди ВСЕХ открытых ордеров
    # Это гарантирует, что следующий шаг будет строиться от самой верхней покупки, а не от устаревшего якоря.
    max_entry_price_global = max((o['entry_price'] for o in open_orders_global), default=0)
    last_sig_num_global = max((o.get('sig_num', 0) for o in open_orders_global), default=0)

    for sig_idx in range(display_signals):
        signal_num_label = f"#{sig_idx + 1}  "
        pair_start_idx = sig_idx * 2
        signal_pair = safe_orders[pair_start_idx : pair_start_idx + 2] if pair_start_idx < len(safe_orders) else []
        is_executed = sig_idx < num_executed_signals

        last_buy = strategy_state.get('strat_last_buy_price') or current_price
        if is_executed:
            first_order = signal_pair[0] if signal_pair else None
            row_base_price = first_order.get('frozen_start_price') if (first_order and first_order.get('frozen_start_price')) else last_buy
        else:
            anchor = strategy_state.get(f'start_price_{coin}')
            if anchor is None:
                anchor = current_price
            row_base_price = anchor

        for log_type in ['breakeven', 'cascade']:
            order_match = next((o for o in signal_pair if o.get('exit_logic') == log_type), None) if isinstance(signal_pair, list) else None

            if is_executed and first_order:
                frozen_val = first_order.get('frozen_start_price')
                # Отладочный вывод можно включить при необходимости

            if is_executed and order_match:
                if 'frozen_volume_mult' in order_match:
                    volume_mult = order_match['frozen_volume_mult']
                else:
                    base_vol = order_match.get('volume_mult', 2)
                    if protection_mode and log_type == 'cascade' and base_vol == 1:
                        volume_mult = 0
                    else:
                        volume_mult = base_vol
                    order_match['frozen_volume_mult'] = volume_mult
            elif protection_mode:
                volume_mult = 0 if log_type == 'cascade' else 1
            else:
                volume_mult = 2

            be_price_calc = "— "
            profit_price_display = "— "
            d_exit_plan = "— "
            d_exit_fact = "— "
            d_open_pnl = "— "
            d_pnl = "— "
            d_total_pnl = "— "
            d_status = "План  " if not is_executed else "Ожидание  "
            entry_p_display = "— "
            flag_up_profit = 0
            current_unrealized = 0.0

            if order_match and 'entry_price' in order_match:
                entry_p = float(order_match['entry_price'])
                entry_p_display = fmt(entry_p)
                is_open = order_match.get('status') == 'open'

                if is_open:
                    d_status = "Open  "
                    current_unrealized = (current_price - entry_p) * (position_size / entry_p) * volume_mult
                    d_open_pnl = fmt(current_unrealized)
                    total_accumulated_pnl += current_unrealized
                    d_total_pnl = fmt(total_accumulated_pnl)
                    open_order_counter += 1

                    if order_match.get('exit_logic') == 'cascade':
                        d_exit_plan = "— "
                    else:
                        is_max_price = (entry_p == max_entry_price_global)
                        is_last_purchase = (order_match.get('sig_num') == last_sig_num_global)
                        start_price_row = order_match.get('frozen_start_price') or entry_p
                        has_plus_step = (current_price >= start_price_row + grid_step)

                        if (not (is_max_price and is_last_purchase)) and has_plus_step:
                            exit_val = entry_p + breakeven_buffer
                            d_exit_plan = fmt(exit_val)
                        else:
                            d_exit_plan = "— "
                else:
                    d_status = "Closed"
                    if order_match.get('frozen_exit_plan') is not None:
                        d_exit_plan = fmt(order_match['frozen_exit_plan'])
                    else:
                        d_exit_plan = "—"

                    exit_p = float(order_match.get('exit_price', entry_p))
                    d_exit_fact = fmt(exit_p)
                    realized_pnl = (exit_p - entry_p) * (position_size / entry_p)
                    d_pnl = fmt(realized_pnl)

            display_start = fmt(row_base_price)
            t_pct = ((current_price - row_base_price) / row_base_price) * 100 if row_base_price else 0
            display_trend = f"{'+' if t_pct >= 0 else ''}{t_pct:.1f}% ".replace('.', ',')

            # 🔥 ИСПРАВЛЕННЫЙ БЛОК РАСЧЕТА "План Покупки"
            if order_match and 'entry_price' in order_match:
                frozen = order_match.get('frozen_plan_buy')
                if frozen is not None:
                    display_plan = fmt(frozen)
                else:
                    display_plan = "— "
            else:
                # Логика для НЕИСПОЛНЕННЫХ (плановых) строк
                # 1. Определяем базу для следующего шага.
                # Если это первый плановый сигнал после исполненных, берем МАКСИМУМ из исполненных.
                # Иначе используем якорь (row_base_price).
                
                prev_entry = None
                if sig_idx > 0:
                    # Проверяем предыдущий сигнал в таблице
                    prev_pair_idx = (sig_idx - 1) * 2
                    if prev_pair_idx < len(safe_orders):
                        prev_order = safe_orders[prev_pair_idx]
                        if isinstance(prev_order, dict) and 'entry_price' in prev_order:
                            prev_entry = float(prev_order['entry_price'])
                
                # Если предыдущий сигнал был исполнен, строим от него.
                # Если нет (цепочка планов), строим от якоря.
                if prev_entry is not None:
                    base_for_step = prev_entry
                else:
                    # Для самого первого планового сигнала после исполненных
                    # Используем глобальный максимум открытых, чтобы избежать разрывов
                    if max_entry_price_global > 0:
                        base_for_step = max_entry_price_global
                    else:
                        base_for_step = row_base_price

                # Вычисляем план
                plan = base_for_step + grid_step

                # Защита от наложения на существующие ордера
                all_entries = [float(o['entry_price']) for o in safe_orders
                               if isinstance(o, dict) and o.get('status') == 'open' and 'entry_price' in o]
                tolerance = grid_step * 0.5
                
                # Если план попадает в зону существующего ордера, сдвигаем вверх
                for _ in range(20):
                    collision = False
                    for entry in all_entries:
                        if abs(plan - entry) <= tolerance:
                            collision = True
                            break
                    if collision:
                        plan += grid_step
                    else:
                        break
                
                display_plan = fmt(plan)

            if is_executed and order_match and order_match.get('status') == 'open':
                current_plan = row_base_price + grid_step if current_price >= row_base_price else row_base_price * (1 - reversal_threshold / 100)
                prev_plan = order_match.get('_last_plan_buy_trace')
                if prev_plan is None or abs(prev_plan - current_plan) > 0.01:
                    trace_calc(
                        order_id=signal_num_label.strip(),
                        col_name="Plan_Buy ",
                        inputs={'base': row_base_price, 'price': current_price},
                        formula="base+step или base*(1-reversal%) ",
                        expected=order_match.get('frozen_plan_buy', current_plan),
                        actual=current_plan
                    )
                    order_match['_last_plan_buy_trace'] = current_plan

            rows.append({
                '№ Сигнала': signal_num_label,
                'Монета': coin,
                'Объём': volume_mult,
                'Тип': log_type.upper(),
                'Старт Цена': display_start,
                'Тренд': display_trend,
                'План Покупки': display_plan,
                'Цена Входа': entry_p_display,
                'BE цена': be_price_calc,
                'PROFIT цена': profit_price_display,
                'up_Profit': flag_up_profit,
                'План Выхода': d_exit_plan,
                'Факт Выхода': d_exit_fact,
                'Open PnL': d_open_pnl,
                'Total PnL': d_total_pnl,
                'PnL': d_pnl,
                'Sum PnL': fmt(total_accumulated_pnl),
                'Статус': d_status
            })

    cols = [
        '№ Сигнала', 'Монета', 'Объём', 'Тип', 'Старт Цена', 'Тренд',
        'План Покупки', 'Цена Входа', 'BE цена', 'PROFIT цена', 'up_Profit',
        'План Выхода', 'Факт Выхода', 'Open PnL', 'Total PnL', 'PnL', 'Sum PnL', 'Статус'
    ]
    return pd.DataFrame(rows, columns=cols)


def update_table_with_trend_and_plan(state, df_table, symbol, current_price, orders):
    return df_table