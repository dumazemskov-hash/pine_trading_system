import ccxt
import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k"
CHAT_ID = "401292001"

TIMEFRAME = "15m"
MIN_BODY_PCT = 4.5
IMPULSE_STRENGTH = 1.25
VOLUME_RATIO = 1.9
LIQ_LOOKBACK = 6
STRUCTURE_LOOKBACK = 7
CONDITION_D = 0.6
MAX_RISK_PCT = 0.02
STOP_BUFFER = 0.003
SIMPLE_STOP_BUFFER = 0.005
PRIOR_VOLUME_MULT = 1.35

TP1_RR = 1.6
TP2_RR = 3.0

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})

sent_signals = set()

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def get_symbols():
    print("Загружаю список монет с Bybit...")
    markets = exchange.load_markets()
    symbols = []
    for symbol, market in markets.items():
        if (market.get("swap") and 
            market.get("quote") == "USDT" and 
            market.get("active") and
            symbol.endswith(":USDT")):
            symbols.append(symbol)
    print(f"Загружено {len(symbols)} активных монет")
    return symbols

def calculate_stop(entry: float, high: float, ohlcv: list) -> float:
    highs = [c[2] for c in ohlcv[-STRUCTURE_LOOKBACK-1:-1]]
    structure_high = max(highs) if highs else high
    structural_stop = structure_high * (1 + STOP_BUFFER)
    
    simple_stop = high * (1 + SIMPLE_STOP_BUFFER)
    candidate_stop = max(structural_stop, simple_stop)
    
    max_allowed_stop = entry * (1 + MAX_RISK_PCT)
    return min(candidate_stop, max_allowed_stop)

def check_signal(symbol: str, ohlcv: list):
    if len(ohlcv) < 65:
        return None

    last = ohlcv[-1]
    prev = ohlcv[-2]
    
    open_p = last[1]
    high_p = last[2]
    low_p = last[3]
    close_p = last[4]
    volume = last[5]
    
    body_pct = abs(close_p - open_p) / open_p * 100
    if body_pct < MIN_BODY_PCT:
        return None
    
    impulse = (high_p - low_p) / low_p * 100 if low_p > 0 else 0
    if impulse < MIN_BODY_PCT * IMPULSE_STRENGTH:
        return None
    
    vol_ma = sum([c[5] for c in ohlcv[-21:-1]]) / 20
    if volume < vol_ma * VOLUME_RATIO:
        return None
    
    vol_recent = sum([c[5] for c in ohlcv[-21:-1]]) / 20
    vol_older = sum([c[5] for c in ohlcv[-61:-21]]) / 40
    if vol_recent < vol_older * PRIOR_VOLUME_MULT:
        return None
    
    recent_low = min([c[3] for c in ohlcv[-LIQ_LOOKBACK-1:-1]])
    liquidity_grab = low_p < recent_low * 0.994
    bearish = close_p < open_p and close_p < prev[4]
    
    if not (liquidity_grab and bearish):
        return None
    
    if ohlcv[-1][5] < ohlcv[-2][5] * CONDITION_D:
        return None
    
    structure_low = min([c[3] for c in ohlcv[-STRUCTURE_LOOKBACK-1:-1]])
    if low_p > structure_low * 0.997:
        return None
    
    entry = close_p
    stop = calculate_stop(entry, high_p, ohlcv)
    risk = stop - entry
    
    if risk <= 0:
        return None
    
    tp1 = entry - risk * TP1_RR
    tp2 = entry - risk * TP2_RR
    
    return {
        "symbol": symbol,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "body_pct": body_pct
    }

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === Scanner v8.24 запущен ===")
    print(f"VOLUME_RATIO = {VOLUME_RATIO} | PRIOR_VOLUME = {PRIOR_VOLUME_MULT} | MAX_RISK = 2%")
    
    symbols = get_symbols()
    
    while True:
        for symbol in symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=70)
                signal = check_signal(symbol, ohlcv)
                
                if signal is None:
                    continue
                
                signal_id = f"{symbol}_{ohlcv[-1][0]}"
                if signal_id in sent_signals:
                    continue
                
                sent_signals.add(signal_id)
                
                msg = (
                    f"RAID v8.24 | {symbol}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"Stop: {signal['stop']:.6f}\n"
                    f"TP1: {signal['tp1']:.6f} | TP2: {signal['tp2']:.6f}\n"
                    f"Risk: {(signal['risk']/signal['entry']*100):.2f}%\n"
                    f"Body: {signal['body_pct']:.2f}%"
                )
                
                send_telegram(msg)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] SIGNAL → {symbol}")
                
            except Exception:
                pass
        
        time.sleep(40)

if __name__ == "__main__":
    main()
