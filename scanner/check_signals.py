import ccxt
from datetime import datetime, timezone
import time

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}
})

signals = [
    {"time": "2026-08-07 14:17", "symbol": "WENSTOCK/USDT:USDT", "entry": 7.170000, "stop": 7.313400, "tp1": 6.940560, "tp2": 6.739800},
    {"time": "2026-08-07 16:47", "symbol": "NBIS/USDT:USDT", "entry": 188.180000, "stop": 191.943600, "tp1": 182.158240, "tp2": 176.889200},
    {"time": "2026-08-07 17:16", "symbol": "BE/USDT:USDT", "entry": 215.800000, "stop": 220.116000, "tp1": 208.894400, "tp2": 202.852000},
    {"time": "2026-08-08 08:32", "symbol": "BSB/USDT:USDT", "entry": 0.158650, "stop": 0.161823, "tp1": 0.153573, "tp2": 0.149131},
    {"time": "2026-08-08 18:17", "symbol": "BAN/USDT:USDT", "entry": 0.074140, "stop": 0.075623, "tp1": 0.071768, "tp2": 0.069692},
    {"time": "2026-08-09 06:15", "symbol": "BLESS/USDT:USDT", "entry": 0.011787, "stop": 0.012023, "tp1": 0.011410, "tp2": 0.011080},
    {"time": "2026-08-09 10:48", "symbol": "BTW/USDT:USDT", "entry": 0.180330, "stop": 0.183937, "tp1": 0.174559, "tp2": 0.169510},
    {"time": "2026-08-09 12:31", "symbol": "NAORIS/USDT:USDT", "entry": 0.029640, "stop": 0.030233, "tp1": 0.028692, "tp2": 0.027862},
    {"time": "2026-08-09 16:49", "symbol": "COOKIE/USDT:USDT", "entry": 0.012800, "stop": 0.013056, "tp1": 0.012390, "tp2": 0.012032},
]

def to_ms(s):
    # Telegram время считаем UTC+3
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return int((dt.replace(tzinfo=timezone.utc).timestamp() - 3 * 3600) * 1000)

def check(sig):
    symbol = sig["symbol"]
    entry, stop, tp1, tp2 = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"]
    signal_ts = to_ms(sig["time"])

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", since=signal_ts - 15*60*1000, limit=250)
    except Exception as e:
        return f"ERROR: {e}"

    if not ohlcv:
        return "NO DATA"

    # свечи после сигнала
    bars = [c for c in ohlcv if c[0] > signal_ts]
    if not bars:
        return "NO BARS AFTER"

    hit = None
    bars_to = 0
    tp1_done = False

    for i, c in enumerate(bars):
        high, low = c[2], c[3]
        bars_to = i + 1

        # шорт: стоп сверху, тейки снизу
        if high >= stop:
            hit = "STOP"
            break
        if low <= tp2:
            hit = "TP2" if not tp1_done else "TP1+TP2"
            break
        if low <= tp1:
            tp1_done = True
            if hit is None:
                hit = "TP1"

    if hit == "TP1":
        for j, c in enumerate(bars[bars_to:]):
            high, low = c[2], c[3]
            if high >= stop:
                hit = "TP1 → STOP"
                bars_to += j + 1
                break
            if low <= tp2:
                hit = "TP1 + TP2"
                bars_to += j + 1
                break
        else:
            last = bars[-1][4]
            hit = f"TP1 (ещё открыт, last={last:.6g})"

    if hit is None:
        last = bars[-1][4]
        max_h = max(c[2] for c in bars)
        min_l = min(c[3] for c in bars)
        hit = f"OPEN last={last:.6g} | maxHigh={max_h:.6g} | minLow={min_l:.6g}"
        bars_to = len(bars)

    return f"{hit} | через {bars_to} свечей"

print("=" * 60)
print("Проверка 9 сигналов RAID v8.27")
print("=" * 60)

for s in signals:
    name = s["symbol"].split("/")[0]
    result = check(s)
    print(f"{s['time']} | {name:10} | {result}")
    time.sleep(0.3)

print("=" * 60)
print("Готово. Скопируй результат и пришли мне.")