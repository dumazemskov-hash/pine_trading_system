import ccxt
import time
import requests
import json
import os
import sys
import atexit
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === DUMP LAB Scanner — parallel to v0.2b ===
# Same entry as v0.2b + SKIP Asia session UTC 00:00–05:59
# BT120d skip_asia: OOS AvgR +0.93 N=10 | FULL +0.71
# Primary dump_scanner.py НЕ трогаем. Сигналы → signals_dump_lab/
TELEGRAM_TOKEN = "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k"
CHAT_ID = "401292001"

TIMEFRAME = "15m"
MIN_BODY_PCT = 6.5
MAX_BODY_PCT = 9.0
PUMP_LOOKBACK = 6
PUMP_MIN_BODY = 8.0
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
VOLUME_RATIO = 3.0
COOLDOWN_BARS = 32
MAX_RISK_PCT = 0.03
STOP_ATR_MULT = 0.35
SWEEP_LOOKBACK = 10
TP1_RR = 1.6
TP2_RR = 3.0
CANDLES_TO_LOG = 40
VERSION = "dump-lab-asia"
ASIA_HOUR_END = 6  # skip if bar UTC hour < 6

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})

sent_signals = set()
last_signal_bar = {}

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump_lab"
SIGNALS_DIR.mkdir(exist_ok=True)
LOCK_PATH = ROOT / "scanner" / ".dump_scanner_lab.lock"
_lock_fd = None


def acquire_lock():
    global _lock_fd
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age > 2 * 3600:
            try:
                LOCK_PATH.unlink()
            except Exception:
                pass
        else:
            try:
                info = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                info = "?"
            print(f"DUMP-LAB уже запущен.\nLock: {LOCK_PATH}\n{info}")
            print("Останови lab или удали scanner\\.dump_scanner_lab.lock")
            sys.exit(0)
    try:
        _lock_fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_lock_fd, f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n".encode())
    except FileExistsError:
        print("DUMP-LAB уже запущен (O_EXCL). Выход.")
        sys.exit(0)

    def _clear():
        global _lock_fd
        try:
            if _lock_fd is not None:
                os.close(_lock_fd)
                _lock_fd = None
        except Exception:
            pass
        try:
            if LOCK_PATH.exists():
                LOCK_PATH.unlink()
        except Exception:
            pass
    atexit.register(_clear)


def load_sent_from_disk():
    loaded = 0
    for d in range(0, 3):
        day = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
        path = SIGNALS_DIR / f"{day}.jsonl"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sym = rec.get("symbol")
                bar_ts = rec.get("bar_ts")
                if sym is None or bar_ts is None:
                    continue
                sid = f"{sym}_{bar_ts}"
                sent_signals.add(sid)
                prev = last_signal_bar.get(sym)
                if prev is None or bar_ts > prev:
                    last_signal_bar[sym] = bar_ts
                loaded += 1
    print(f"LAB дедуп с диска: {loaded} записей, unique={len(sent_signals)}")


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"TG: {e}")


def log_signal(signal, ohlcv, atr):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIGNALS_DIR / f"{today}.jsonl"
    recent = [{
        "ts": c[0], "o": round(c[1], 8), "h": round(c[2], 8),
        "l": round(c[3], 8), "c": round(c[4], 8), "v": round(c[5], 4),
    } for c in ohlcv[-CANDLES_TO_LOG:]]
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "symbol": signal["symbol"],
        "entry": signal["entry"], "stop": signal["stop"],
        "tp1": signal["tp1"], "tp2": signal["tp2"],
        "risk": signal["risk"],
        "risk_pct": round(signal["risk"] / signal["entry"] * 100, 3),
        "body_pct": signal["body_pct"],
        "bar_ts": signal["bar_ts"],
        "atr": atr,
        "filter": "skip_asia_utc0_5",
        "candles": recent,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_symbols():
    markets = exchange.load_markets()
    symbols = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ]
    print(f"Символов: {len(symbols)}")
    return symbols


def calc_atr(ohlcv, period=14):
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def had_pump(ohlcv):
    if len(ohlcv) < PUMP_LOOKBACK + 2:
        return False
    for j in range(-PUMP_LOOKBACK - 1, -1):
        o, c = ohlcv[j][1], ohlcv[j][4]
        if o > 0 and (c - o) / o * 100 >= PUMP_MIN_BODY:
            return True
    window = ohlcv[-PUMP_LOOKBACK - 1:-1]
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    return lo > 0 and (hi - lo) / lo * 100 >= PUMP_MIN_BODY


def check_signal(symbol, ohlcv_raw):
    if len(ohlcv_raw) < 75:
        return None
    ohlcv = ohlcv_raw[:-1]
    if len(ohlcv) < 75:
        return None
    if not had_pump(ohlcv):
        return None
    last = ohlcv[-1]
    open_p, high_p, low_p, close_p, volume = last[1], last[2], last[3], last[4], last[5]

    # LAB: skip Asia UTC 00:00–05:59 (bar open hour)
    hour = (last[0] // 1000) % 86400 // 3600
    if hour < ASIA_HOUR_END:
        return None

    if close_p >= open_p:
        return None
    body_pct = (open_p - close_p) / open_p * 100
    if body_pct < MIN_BODY_PCT or body_pct > MAX_BODY_PCT:
        return None
    rng = high_p - low_p
    if rng <= 0:
        return None
    if (close_p - low_p) / rng > CLOSE_IN_RANGE_MAX:
        return None
    if abs(close_p - open_p) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(c[5] for c in ohlcv[-21:-1]) / 20
    if volume < vol_ma * VOLUME_RATIO:
        return None
    recent_lows = [c[3] for c in ohlcv[-SWEEP_LOOKBACK:]]
    if low_p > min(recent_lows):
        return None
    atr = calc_atr(ohlcv)
    entry = close_p
    stop = min(high_p + (atr * STOP_ATR_MULT if atr else high_p * 0.005), entry * (1 + MAX_RISK_PCT))
    risk = stop - entry
    if risk <= 0:
        return None
    return {
        "symbol": symbol,
        "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body_pct": body_pct,
        "bar_ts": last[0], "atr": atr, "ohlcv_closed": ohlcv,
        "hour_utc": hour,
    }


def main():
    acquire_lock()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === DUMP LAB (skip Asia UTC0-5) ===")
    print(
        f"v0.2b filters + skip hour<6 | body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | "
        f"vol>={VOLUME_RATIO}x | stop {MAX_RISK_PCT*100:.0f}% | → signals_dump_lab/"
    )
    load_sent_from_disk()
    symbols = get_symbols()
    while True:
        try:
            if _lock_fd is not None:
                os.utime(LOCK_PATH, None)
        except Exception:
            pass
        for symbol in symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=90)
                signal = check_signal(symbol, ohlcv)
                if signal is None:
                    continue
                sid = f"{symbol}_{signal['bar_ts']}"
                if sid in sent_signals:
                    continue
                last_ts = last_signal_bar.get(symbol)
                if last_ts is not None:
                    if (signal["bar_ts"] - last_ts) / (15 * 60 * 1000) < COOLDOWN_BARS:
                        continue
                sent_signals.add(sid)
                last_signal_bar[symbol] = signal["bar_ts"]
                log_signal(signal, signal["ohlcv_closed"], signal.get("atr"))
                rp = signal["risk"] / signal["entry"] * 100
                msg = (
                    f"DUMP LAB asia | {symbol}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"Stop:  {signal['stop']:.6f}\n"
                    f"TP1:   {signal['tp1']:.6f} | TP2: {signal['tp2']:.6f}\n"
                    f"Risk:  {rp:.2f}%\n"
                    f"Body:  {signal['body_pct']:.2f}%\n"
                    f"HourUTC: {signal.get('hour_utc')}\n"
                    f"Mode:  lab skip-asia (parallel v0.2b)"
                )
                send_telegram(msg)
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] LAB → {symbol} | "
                    f"body={signal['body_pct']:.1f}% h={signal.get('hour_utc')}"
                )
            except Exception:
                pass
        time.sleep(45)


if __name__ == "__main__":
    main()
