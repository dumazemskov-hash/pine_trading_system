#!/usr/bin/env python3
"""VAL-FADE live scanner. Not DUMP. Writes signals_val/. Telegram prefix VAL-FADE."""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
import requests
try:
    import ccxt
except ImportError:
    ccxt = None

ROOT = Path(__file__).resolve().parents[1]
SIGNALS_DIR = ROOT / "signals_val"
STATE_PATH = SIGNALS_DIR / "sent.json"
LATEST_PATH = ROOT / "paper" / "val_latest.txt"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "401292001")
TIMEFRAME = "15m"
LOOP_SLEEP = 45
MAX_SYMBOLS = int(os.environ.get("VAL_MAX_SYMBOLS", "80"))
COOLDOWN_BARS = 48
LIMIT = 250
PUMP_MIN = 0.30
PUMP_LB = 192
N_BINS = 20
HVN_FRAC = 0.55
UPPER_FRAC = 0.62
HOLD = 6
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.08
MIN_RR = 1.2
CLOSE_FILL = True
BTC_DUMP = 0.006
GRID_R = 1.5

def now():
    return datetime.now(timezone.utc)

def send_telegram(message: str):
    if os.environ.get("VAL_TG_OFF") == "1":
        print("TG off:", message.splitlines()[0]); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"TG: {e}")

def load_state():
    if not STATE_PATH.exists():
        return {"sent": [], "last": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent": [], "last": {}}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

def log_signal(rec):
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGNALS_DIR / f"{now().strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(
        f"{rec['logged_at']}  {rec['symbol']}  entry={rec['entry']}  stop={rec['stop']}  tp04={rec['tp04']}\n",
        encoding="utf-8")

def local_high(bars, i, left=3, right=3):
    h = bars[i][2]
    a = max(0, i - left); b = min(len(bars), i + right + 1)
    return h >= max(bars[j][2] for j in range(a, b))

def profile_cluster(bars, lo_i, hi_i):
    window = bars[lo_i:hi_i + 1]
    if len(window) < 8:
        return None
    lo = min(b[3] for b in window); hi = max(b[2] for b in window)
    if lo <= 0 or (hi - lo) / lo < PUMP_MIN:
        return None
    width = (hi - lo) / N_BINS
    if width <= 0:
        return None
    bins = [0.0] * N_BINS
    for _, _, h, l, c, v in window:
        mid = (h + l + c) / 3.0
        k = int((mid - lo) / width)
        k = max(0, min(N_BINS - 1, k))
        bins[k] += v
    peak = max(bins)
    if peak <= 0:
        return None
    hvn = [k for k, val in enumerate(bins) if val >= peak * HVN_FRAC]
    if not hvn:
        return None
    upper_cut = lo + UPPER_FRAC * (hi - lo)
    best = None; best_vol = -1.0; s = 0
    while s < len(hvn):
        e = s
        while e + 1 < len(hvn) and hvn[e + 1] == hvn[e] + 1:
            e += 1
        cluster = hvn[s:e + 1]
        node_lo = lo + cluster[0] * width
        node_hi = lo + (cluster[-1] + 1) * width
        mid = (node_lo + node_hi) / 2.0
        vol = sum(bins[k] for k in cluster)
        if mid >= upper_cut and vol > best_vol:
            best_vol = vol
            best = (node_lo, node_hi, lo, hi)
        s = e + 1
    if best is None:
        return None
    node_lo, node_hi, lo, hi = best
    if not (lo < node_lo < node_hi < hi):
        return None
    return {"lo": lo, "hi": hi, "node_lo": node_lo, "node_hi": node_hi,
            "tp": hi - 0.4 * (hi - lo), "pump_pct": (hi - lo) / lo * 100.0}

def find_setup(bars):
    n = len(bars)
    if n < PUMP_LB + 20:
        return None
    last = n - 1
    i = last - HOLD
    while i > n - PUMP_LB and i > 8:
        if not local_high(bars, i):
            i -= 1; continue
        win_lo = max(0, i - PUMP_LB)
        lo_i = min(range(win_lo, i + 1), key=lambda k: bars[k][3])
        node = profile_cluster(bars, lo_i, i)
        if node is None:
            i -= 1; continue
        entry, stop, tp = node["node_lo"], node["node_hi"], node["tp"]
        if entry <= 0 or stop <= entry:
            i -= 1; continue
        stop_pct = (stop - entry) / entry
        if stop_pct < MIN_STOP_PCT or stop_pct > MAX_STOP_PCT:
            i -= 1; continue
        rr = (entry - tp) / (stop - entry)
        if rr < MIN_RR:
            i -= 1; continue
        start = i + HOLD
        if last < start:
            i -= 1; continue
        bar = bars[last]
        hit = bar[4] < entry if CLOSE_FILL else bar[3] <= entry
        if not hit:
            return None
        already = any(bars[j][4] < entry if CLOSE_FILL else bars[j][3] <= entry for j in range(start, last))
        if already:
            return None
        return {"pump_bar": i, "entry": entry, "stop": stop, "tp04": tp,
                "grid": entry - GRID_R * (stop - entry), "stop_pct": stop_pct * 100.0,
                "plan_r": rr, "pump_pct": node["pump_pct"], "bar_ts": bar[0]}
    return None

def make_exchange():
    if ccxt is None:
        return None
    return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}})

def top_symbols(ex):
    if ex is None:
        return []
    rows = []
    for s, t in ex.fetch_tickers().items():
        if s.endswith(":USDT"):
            rows.append((t.get("quoteVolume") or 0, s))
    rows.sort(reverse=True)
    return [s for _, s in rows[:MAX_SYMBOLS]]

def fetch_ohlcv(ex, symbol):
    raw = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT)
    return raw[:-1] if len(raw) >= 2 else []

def btc_bar_ret(ex):
    try:
        raw = fetch_ohlcv(ex, "BTC/USDT:USDT")
        if not raw:
            return None
        o, c = raw[-1][1], raw[-1][4]
        return (c - o) / o if o > 0 else None
    except Exception:
        return None

def main():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[{now().strftime('%H:%M:%S')}] VAL-FADE scanner  15m  top{MAX_SYMBOLS}  close_fill={CLOSE_FILL}")
    print("signals ->", SIGNALS_DIR)
    print("NE DUMP")
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    print(f"symbols {len(symbols)}")
    state = load_state()
    sent = set(state.get("sent") or [])
    last_bar = state.get("last") or {}
    while True:
        btc = btc_bar_ret(ex)
        if btc is not None and btc <= -BTC_DUMP:
            print(f"[{now().strftime('%H:%M:%S')}] BTC {btc*100:.2f}% skip cycle")
            time.sleep(LOOP_SLEEP); continue
        for symbol in symbols:
            try:
                bars = fetch_ohlcv(ex, symbol)
                sig = find_setup(bars)
                if sig is None:
                    continue
                sid = f"{symbol}_{sig['bar_ts']}"
                if sid in sent:
                    continue
                prev = last_bar.get(symbol)
                if prev and (sig["bar_ts"] - prev) / (15 * 60 * 1000) < COOLDOWN_BARS:
                    continue
                sent.add(sid); last_bar[symbol] = sig["bar_ts"]
                rec = {
                    "logged_at": now().isoformat(), "version": "val-fade-scan-0.1",
                    "symbol": symbol, "entry": round(sig["entry"], 8),
                    "stop": round(sig["stop"], 8), "tp04": round(sig["tp04"], 8),
                    "grid15": round(sig["grid"], 8), "risk_pct": round(sig["stop_pct"], 3),
                    "plan_r": round(sig["plan_r"], 2), "pump_pct": round(sig["pump_pct"], 1),
                    "bar_ts": sig["bar_ts"],
                    "btc_ret": None if btc is None else round(btc * 100, 3),
                }
                log_signal(rec)
                save_state({"sent": list(sent)[-400:], "last": last_bar})
                send_telegram(
                    f"VAL-FADE | {symbol}\npump {rec['pump_pct']:.1f}%\n"
                    f"Entry: {rec['entry']}\nStop:  {rec['stop']}\n"
                    f"TP1 1.5R: {rec['grid15']}\nTP2 0.4:  {rec['tp04']}\n"
                    f"Risk:  {rec['risk_pct']:.2f}%   planR {rec['plan_r']:.2f}\n"
                    f"BTC bar: {rec['btc_ret']}\nsetka 50% @ 1.5R -> BE\nNE DUMP"
                )
                print(f"[{now().strftime('%H:%M:%S')}] VAL -> {symbol}  pump={rec['pump_pct']:.1f}%")
            except Exception as e:
                print(f"{symbol}: {e}")
        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()
