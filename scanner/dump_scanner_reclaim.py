import ccxt
import time
import requests
import json
import os
import sys
import atexit
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === DUMP RECLAIM Scanner — parallel lab ===
# Live dump_scanner.py НЕ трогаем.
# Dump-свеча v0.2b только pending-уровень.
# Вход: отскок задевает open dump, high < dump.high, close < open, медвежья свеча.
# Стоп = high отскока * 1.001. Окно 8 баров (2ч). Иначе TIMEOUT / SWEPT.
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
VERSION = "dump-lab-reclaim"
WINDOW_BARS = 8
MAX_STOP_PCT = 0.08

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})

sent_signals = set()
last_signal_bar = {}
pending = {}

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump_reclaim"
SIGNALS_DIR.mkdir(exist_ok=True)
PENDING_PATH = SIGNALS_DIR / "pending.json"
LOCK_PATH = ROOT / "scanner" / ".dump_scanner_reclaim.lock"
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
                print(f"RECLAIM: снят stale lock (pid={pid}, age={age/60:.0f}m)")
            except Exception as e:
                print(f"RECLAIM lock: {e}")
                sys.exit(1)
        else:
            print(f"DUMP-RECLAIM уже запущен (pid={pid}).\n{info}")
            sys.exit(0)
    try:
        _lock_fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_lock_fd, f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n".encode())
    except FileExistsError:
        print("DUMP-RECLAIM уже запущен (O_EXCL).")
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
        print(f"RECLAIM pending с диска: {len(pending)}")
    except Exception as e:
        print(f"RECLAIM pending load: {e}")


def save_pending():
    try:
        PENDING_PATH.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"RECLAIM pending save: {e}")


def load_sent_from_disk():
    loaded = 0
    for d in range(0, 5):
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
                sent_signals.add(f"{sym}_{bar_ts}")
                sent_signals.add(f"{sym}_{bar_ts}_{rec.get('status', '')}")
                prev = last_signal_bar.get(sym)
                if prev is None or bar_ts > prev:
                    last_signal_bar[sym] = bar_ts
                loaded += 1
    print(f"RECLAIM дедуп: {loaded}, unique={len(sent_signals)}")


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"TG: {e}")


def log_record(record, ohlcv=None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIGNALS_DIR / f"{today}.jsonl"
    if ohlcv:
        record["candles"] = [{
            "ts": c[0], "o": round(c[1], 8), "h": round(c[2], 8),
            "l": round(c[3], 8), "c": round(c[4], 8), "v": round(c[5], 4),
        } for c in ohlcv[-CANDLES_TO_LOG:]]
    record["logged_at"] = datetime.now(timezone.utc).isoformat()
    record["version"] = VERSION
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


def check_dump(symbol, ohlcv_raw):
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
    return {
        "symbol": symbol,
        "dump_o": open_p, "dump_h": high_p, "dump_l": low_p, "dump_c": close_p,
        "body_pct": body_pct, "bar_ts": last[0], "atr": atr,
        "ohlcv_closed": ohlcv,
    }


def resolve_pending(symbol, ohlcv_raw):
    pend = pending.get(symbol)
    if not pend:
        return
    if len(ohlcv_raw) < 3:
        return
    closed = ohlcv_raw[:-1]
    after = [c for c in closed if c[0] > pend["bar_ts"]]
    dump_h = float(pend["dump_h"])
    dump_o = float(pend["dump_o"])
    wait = len(after)

    def finish(status, extra=None, trade=None):
        rec = {
            "status": status,
            "symbol": symbol,
            "bar_ts": pend["bar_ts"],
            "dump_o": dump_o, "dump_h": dump_h,
            "dump_l": pend.get("dump_l"), "dump_c": pend.get("dump_c"),
            "body_pct": pend.get("body_pct"),
            "wait_bars": wait,
        }
        if extra:
            rec.update(extra)
        if trade:
            rec.update(trade)
        log_record(rec, closed)
        sent_signals.add(f"{symbol}_{pend['bar_ts']}")
        pending.pop(symbol, None)
        save_pending()
        return rec

    if not after:
        return

    for bar in after:
        ts, o, h, l, c = bar[0], bar[1], bar[2], bar[3], bar[4]
        if h >= dump_h:
            rec = finish("swept", {"confirm_ts": ts, "high1": h})
            send_telegram(f"DUMP RECLAIM SWEPT | {symbol}\nhigh {h:.6f} >= dump.high {dump_h:.6f}")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] RECLAIM SWEPT → {symbol}")
            return
        if h < dump_o:
            continue
        if c >= o:
            continue
        if c >= dump_o:
            continue
        entry = c
        stop = h * 1.001
        risk = stop - entry
        if risk <= 0 or risk / entry > MAX_STOP_PCT:
            rec = finish("wide", {"confirm_ts": ts, "entry": entry, "stop": stop})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] RECLAIM WIDE → {symbol}")
            return
        trade = {
            "entry": entry, "stop": stop,
            "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
            "risk": risk, "risk_pct": round(risk / entry * 100, 3),
            "confirm_ts": ts, "reject_high": h,
        }
        rec = finish("confirmed", trade=trade)
        send_telegram(
            f"DUMP RECLAIM OK | {symbol}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop:  {stop:.6f}\n"
            f"TP1:   {trade['tp1']:.6f} | TP2: {trade['tp2']:.6f}\n"
            f"Risk:  {trade['risk_pct']:.2f}%\n"
            f"Wait:  {wait} bars | stop=reject high"
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] RECLAIM OK → {symbol} wait={wait}")
        return

    if wait >= WINDOW_BARS:
        finish("timeout")
        send_telegram(f"DUMP RECLAIM TIMEOUT | {symbol}\nокно {WINDOW_BARS} баров, отскока к open не было")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] RECLAIM TIMEOUT → {symbol}")


def main():
    acquire_lock()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === DUMP RECLAIM (failed reclaim after dump) ===")
    print(f"pending dump → confirm if bounce to open, high<dump.high, close<open | window {WINDOW_BARS}")
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
                dump = check_dump(symbol, ohlcv)
                if dump is None:
                    continue
                sid = f"{symbol}_{dump['bar_ts']}"
                if sid in sent_signals:
                    continue
                last_ts = last_signal_bar.get(symbol)
                if last_ts is not None:
                    if (dump["bar_ts"] - last_ts) / BAR_MS < COOLDOWN_BARS:
                        continue
                last_signal_bar[symbol] = dump["bar_ts"]
                store = {k: v for k, v in dump.items() if k != "ohlcv_closed"}
                pending[symbol] = store
                save_pending()
                log_record({
                    "status": "pending",
                    "symbol": symbol,
                    "bar_ts": dump["bar_ts"],
                    "dump_o": dump["dump_o"], "dump_h": dump["dump_h"],
                    "dump_l": dump["dump_l"], "dump_c": dump["dump_c"],
                    "body_pct": dump["body_pct"],
                }, dump["ohlcv_closed"])
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] RECLAIM pending → {symbol} | "
                    f"body={dump['body_pct']:.1f}% wait bounce to open"
                )
            except Exception:
                pass
        time.sleep(45)


if __name__ == "__main__":
    main()
