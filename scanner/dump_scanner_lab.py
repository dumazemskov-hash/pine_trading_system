import ccxt
import time
import requests
import json
import os
import sys
import atexit
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === DUMP LAB Scanner — v0.2b + skip Asia + confirm gate ===
# Live dump_scanner.py НЕ трогаем.
# Гейт: сигнал становится pending. После закрытия bar+1:
#   CANCEL если high(bar+1) >= original stop
#   иначе CONFIRM с исходными entry/stop/TP
# Оптимистичный fill (вход по close сигнала). Честные варианты — в backtest_dump_gate.py
TELEGRAM_TOKEN = "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k"
CHAT_ID = "401292001"

TIMEFRAME = "15m"
BAR_MS = 15 * 60 * 1000
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
VERSION = "dump-lab-gate"
ASIA_HOUR_END = 6

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})

sent_signals = set()
last_signal_bar = {}
pending = {}  # symbol -> dict

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump_lab"
SIGNALS_DIR.mkdir(exist_ok=True)
PENDING_PATH = SIGNALS_DIR / "pending.json"
LOCK_PATH = ROOT / "scanner" / ".dump_scanner_lab.lock"
_lock_fd = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform.startswith("win"):
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x00100000, 0, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError, ValueError):
        return False


def _read_lock_pid():
    try:
        text = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
        first = text.splitlines()[0].strip() if text else ""
        return int(first), text
    except Exception:
        return None, ""


def acquire_lock():
    global _lock_fd
    if LOCK_PATH.exists():
        pid, info = _read_lock_pid()
        age = time.time() - LOCK_PATH.stat().st_mtime
        stale = (pid is None) or (not _pid_alive(pid)) or (age > 30 * 60)
        if stale:
            try:
                LOCK_PATH.unlink()
                print(f"LAB: снят stale lock (pid={pid}, age={age/60:.0f}m)")
            except Exception as e:
                print(f"LAB lock: {e}")
                sys.exit(1)
        else:
            print(f"DUMP-LAB уже запущен (pid={pid}).\n{info}")
            sys.exit(0)
    try:
        _lock_fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_lock_fd, f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n".encode())
    except FileExistsError:
        print("DUMP-LAB уже запущен (O_EXCL).")
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


def load_pending():
    if not PENDING_PATH.exists():
        return
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            pending.update(data)
        print(f"LAB pending с диска: {len(pending)}")
    except Exception as e:
        print(f"LAB pending load: {e}")


def save_pending():
    try:
        PENDING_PATH.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"LAB pending save: {e}")


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
                sid = f"{sym}_{bar_ts}_{rec.get('status', '')}"
                sent_signals.add(sid)
                sent_signals.add(f"{sym}_{bar_ts}")
                prev = last_signal_bar.get(sym)
                if prev is None or bar_ts > prev:
                    last_signal_bar[sym] = bar_ts
                loaded += 1
    print(f"LAB дедуп: {loaded}, unique={len(sent_signals)}")


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"TG: {e}")


def log_signal(signal, ohlcv, atr, status, extra=None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIGNALS_DIR / f"{today}.jsonl"
    recent = [{
        "ts": c[0], "o": round(c[1], 8), "h": round(c[2], 8),
        "l": round(c[3], 8), "c": round(c[4], 8), "v": round(c[5], 4),
    } for c in ohlcv[-CANDLES_TO_LOG:]]
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "status": status,
        "symbol": signal["symbol"],
        "entry": signal["entry"], "stop": signal["stop"],
        "tp1": signal["tp1"], "tp2": signal["tp2"],
        "risk": signal["risk"],
        "risk_pct": round(signal["risk"] / signal["entry"] * 100, 3),
        "body_pct": signal["body_pct"],
        "bar_ts": signal["bar_ts"],
        "atr": atr,
        "filter": "skip_asia_utc0_5+gate_h1",
        "candles": recent,
    }
    if extra:
        record.update(extra)
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


def resolve_pending(symbol, ohlcv_raw):
    pend = pending.get(symbol)
    if not pend:
        return
    if len(ohlcv_raw) < 3:
        return
    closed = ohlcv_raw[:-1]
    after = [c for c in closed if c[0] > pend["bar_ts"]]
    if not after:
        return
    bar1 = after[0]
    high1 = bar1[2]
    stop = float(pend["stop"])
    extra = {
        "confirm_ts": bar1[0],
        "high1": high1,
        "close1": bar1[4],
        "bounce_pct": round((high1 - pend["entry"]) / pend["entry"] * 100, 3) if pend["entry"] else None,
    }
    sid = f"{symbol}_{pend['bar_ts']}"
    if high1 >= stop:
        extra["reason"] = "high1>=orig_stop"
        log_signal(pend, closed, pend.get("atr"), "skip_bounce", extra)
        send_telegram(
            f"DUMP LAB GATE SKIP | {symbol}\n"
            f"high1 {high1:.6f} >= stop {stop:.6f}\n"
            f"Body: {pend['body_pct']:.2f}%\n"
            f"Mode: cancel bounce bar+1"
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] LAB SKIP bounce → {symbol}")
    else:
        log_signal(pend, closed, pend.get("atr"), "confirmed", extra)
        rp = pend["risk"] / pend["entry"] * 100
        send_telegram(
            f"DUMP LAB GATE OK | {symbol}\n"
            f"Entry: {pend['entry']:.6f}\n"
            f"Stop:  {pend['stop']:.6f}\n"
            f"TP1:   {pend['tp1']:.6f} | TP2: {pend['tp2']:.6f}\n"
            f"Risk:  {rp:.2f}%\n"
            f"Body:  {pend['body_pct']:.2f}%\n"
            f"high1: {high1:.6f} < stop\n"
            f"Mode:  confirm bar+1 | orig levels"
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] LAB CONFIRM → {symbol} | high1<{stop:.6f}")
    sent_signals.add(sid)
    pending.pop(symbol, None)
    save_pending()


def main():
    acquire_lock()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === DUMP LAB (skip Asia + gate h1) ===")
    print(
        f"v0.2b + skip hour<6 + CANCEL if high(bar+1)>=stop | "
        f"body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | vol>={VOLUME_RATIO}x | → signals_dump_lab/"
    )
    load_sent_from_disk()
    load_pending()
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
                if symbol in pending:
                    resolve_pending(symbol, ohlcv)
                    continue
                signal = check_signal(symbol, ohlcv)
                if signal is None:
                    continue
                sid = f"{symbol}_{signal['bar_ts']}"
                if sid in sent_signals:
                    continue
                last_ts = last_signal_bar.get(symbol)
                if last_ts is not None:
                    if (signal["bar_ts"] - last_ts) / BAR_MS < COOLDOWN_BARS:
                        continue
                last_signal_bar[symbol] = signal["bar_ts"]
                store = {k: v for k, v in signal.items() if k != "ohlcv_closed"}
                pending[symbol] = store
                save_pending()
                log_signal(signal, signal["ohlcv_closed"], signal.get("atr"), "pending")
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] LAB pending → {symbol} | "
                    f"body={signal['body_pct']:.1f}% wait bar+1"
                )
            except Exception:
                pass
        time.sleep(45)


if __name__ == "__main__":
    main()
