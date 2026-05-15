# 📄 config.py
# 📥 ВХОД: Нет (чистые константы)
# 🔗 ЗАВИСИТ ОТ: Нет
# 📤 ВЫХОД: Единый источник конфигурации для всех модулей
# 💡 ИСПРАВЛЕНО: Добавлен алиас TRADES_CSV_FILENAME для совместимости с logger.py



# === 🎯 ПАРАМЕТРЫ СТРАТЕГИИ ===
DEFAULT_STRATEGY_CONFIG = {
    'strat_fixed_pos_size': 5.0,
    'strat_grid_step': 15.00,
    'strat_breakeven_buffer': 5.00,
    'strat_reversal_threshold': 10.0,
    'strat_entry_restrict_pct': 10.0,
    'grid_step_percent': 0.5
}

MARKET_DATA_CSV = "market_data.csv"
DEBUG_LOG_FILENAME = "debug_logs_long.txt"
TRADES_LOG_FILENAME = "trades_log_long.csv"
TRADES_CSV_FILENAME = TRADES_LOG_FILENAME

def get_positions_filename(symbol):
    coin = symbol.replace('USDT', '').lower()
    return f"open_positions_{coin}.json"

OPEN_POSITIONS_FILE = None

MAX_LOG_ENTRIES = 100
PNL_HISTORY_MAX_POINTS = 100
BALANCE_REFRESH_INTERVAL_SEC = 30
BYBIT_API_TIMEOUT = 10
BYBIT_RECV_WINDOW = 5000
MAIN_LOOP_SLEEP_REAL = 10
MAIN_LOOP_SLEEP_SIM_DEFAULT_SPEED = 1.0