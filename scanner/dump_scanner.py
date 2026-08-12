import ccxt
import time
import requests
import json
import os
import sys
import atexit
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === DUMP-after-PUMP Scanner v0.2_strict ===
# BT 60d: +31.6% | Avg +0.25R | MaxDD 16.8% | 62 sig
TELEGRAM_TOKEN = "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k"
CHAT_ID = "401292001"

TIMEFRAME = "15m"
MIN_BODY_PCT = 5.0
MAX_BODY_PCT = 9.0
PUMP_LOOKBACK = 6
PUMP_MIN_BODY = 8.0
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
VOLUME_RATIO = 2.5
COOLDOWN_BARS = 32
MAX_RISK_PCT = 0.02
STOP_ATR_MULT = 0.35
SWEEP_LOOKBACK = 10
TP1_RR = 1.6
TP2_RR = 3.0
CANDLES_TO_LOG = 40
VERSION = "dump-v0.2"

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})

sent_signals = set()
last_signal_bar = {}

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump"
SIGNALS_DIR.mkdir(exist_ok=True)
LOCK_PATH = ROOT / "scanner" / ".dump_scanner.lock"


def acquire_lock():
    """Один процесс DUMP. Если уже запущен — выход."""
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip().split("\n")[0])
            # Windows / Unix: check if pid alive is OS-specific; stale lock if > 6h
            mtime = LOCK_PATH.stat().st_mtime
            if time.time() - mtime < 6 * 3600:
                print(f"DUMP уже запущен (lock pid={old_pid}). Выход.")
                print("Если это старый лок: удали scanner/.dump_scanner.lock")
                sys.exit(0)
        except Exception:
            pass
    LOCK_PATH.write_text(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")

    def _clear():
        try:
            if LOCK_PATH.exists():
                LOCK_PATH.unlink()
        except Exception:
            pass
    atexit.register(_clear)


def load_sent_from_disk():
    """Подтянуть sid из jsonl за 3 дня — после рестарта не дублировать TG."""
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
    print(f"Дедуп с диска: {loaded} записей, unique sid={len(sent_signals)}")


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
    }


def main():
    acquire_lock()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === DUMP Scanner v0.2_strict ===")
    print(f"pump>={PUMP_MIN_BODY}%/{PUMP_LOOKBACK}b | body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | vol>={VOLUME_RATIO}x | risk {MAX_RISK_PCT*100:.0f}%")
    load_sent_from_disk()
    symbols = get_symbols()
    while True:
        # touch lock so it is not considered stale
        try:
            LOCK_PATH.write_text(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
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
                # mark BEFORE telegram so crash mid-send still dedups next time
                sent_signals.add(sid)
                last_signal_bar[symbol] = signal["bar_ts"]
                log_signal(signal, signal["ohlcv_closed"], signal.get("atr"))
                rp = signal["risk"] / signal["entry"] * 100
                msg = (
                    f"DUMP v0.2 | {symbol}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"Stop:  {signal['stop']:.6f}\n"
                    f"TP1:   {signal['tp1']:.6f} | TP2: {signal['tp2']:.6f}\n"
                    f"Risk:  {rp:.2f}%\n"
                    f"Body:  {signal['body_pct']:.2f}%\n"
                    f"Mode:  dump-after-pump strict"
                )
                send_telegram(msg)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] DUMP → {symbol} | body={signal['body_pct']:.1f}%")
            except Exception:
                pass
        time.sleep(45)


if __name__ == "__main__":
    main()
