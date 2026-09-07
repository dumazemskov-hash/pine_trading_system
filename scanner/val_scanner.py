#!/usr/bin/env python3
"""VAL-FADE scanner. ARMED = limit on node. FIRED = close below node (journal)."""
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
ARMED_PATH = SIGNALS_DIR / "armed.jsonl"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "401292001")
TIMEFRAME = "15m"
LOOP_SLEEP = int(os.environ.get("VAL_LOOP_SLEEP", "90"))
REQ_SLEEP = float(os.environ.get("VAL_REQ_SLEEP", "0.18"))
MAX_SYMBOLS = int(os.environ.get("VAL_MAX_SYMBOLS", "50"))
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
        return {"sent": [], "armed": [], "last": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent": [], "armed": [], "last": {}}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

def append_jsonl(path: Path, rec: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

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

def pack(node, i, bar, kind, pump_ts):
    entry, stop, tp = node["node_lo"], node["node_hi"], node["tp"]
    risk = stop - entry
    return {
        "kind": kind,
        "pump_bar": i,
        "pump_ts": pump_ts,
        "entry": entry,
        "stop": stop,
        "tp04": tp,
        "grid": entry - GRID_R * risk,
        "stop_pct": (risk / entry) * 100.0,
        "plan_r": (entry - tp) / risk,
        "pump_pct": node["pump_pct"],
        "bar_ts": bar[0],
    }

def find_node(bars):
    n = len(bars)
    if n < PUMP_LB + 20:
        return None
    last = n - 1
    bar = bars[last]
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
        pump_ts = bars[i][0]
        if bar[2] >= stop:
            return pack(node, i, bar, "dead", pump_ts)
        if bar[4] < entry:
            already = any(bars[j][4] < entry for j in range(start, last))
            if already:
                return pack(node, i, bar, "dead", pump_ts)
            return pack(node, i, bar, "fired", pump_ts)
        return pack(node, i, bar, "armed", pump_ts)
    return None

def rec_base(symbol, node, btc):
    return {
        "logged_at": now().isoformat(),
        "version": "val-fade-scan-0.2",
        "symbol": symbol,
        "kind": node["kind"],
        "entry": round(node["entry"], 8),
        "stop": round(node["stop"], 8),
        "tp04": round(node["tp04"], 8),
        "grid15": round(node["grid"], 8),
        "risk_pct": round(node["stop_pct"], 3),
        "plan_r": round(node["plan_r"], 2),
        "pump_pct": round(node["pump_pct"], 1),
        "bar_ts": node["bar_ts"],
        "pump_bar": node["pump_bar"],
        "pump_ts": node.get("pump_ts"),
        "btc_ret": None if btc is None else round(btc * 100, 3),
    }

def make_exchange():
    if ccxt is None:
        return None
    return ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
    })

def top_symbols(ex):
    if ex is None:
        return []
    rows = []
    for s, t in ex.fetch_tickers().items():
        if s.endswith(":USDT"):
            rows.append((t.get("quoteVolume") or 0, s))
    rows.sort(reverse=True)
    return [s for _, s in rows[:MAX_SYMBOLS]]

def is_rate_limit(err) -> bool:
    s = str(err)
    return "10006" in s or "Too many visits" in s or "RateLimit" in s

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
    except Exception as e:
        if is_rate_limit(e):
            raise
        return None

def main():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[{now().strftime('%H:%M:%S')}] VAL-FADE  top{MAX_SYMBOLS}  ARMED+FIRED")
    print(f"sleep {REQ_SLEEP}s/req  loop {LOOP_SLEEP}s")
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    print(f"symbols {len(symbols)}")
    state = load_state()
    sent = set(state.get("sent") or [])
    armed_sent = set(state.get("armed") or [])
    last_bar = state.get("last") or {}
    cycles = 0
    while True:
        try:
            btc = btc_bar_ret(ex)
        except Exception as e:
            if is_rate_limit(e):
                print(f"[{now().strftime('%H:%M:%S')}] rate limit BTC, sleep 20s")
                time.sleep(20)
                continue
            btc = None
        if btc is not None and btc <= -BTC_DUMP:
            print(f"[{now().strftime('%H:%M:%S')}] BTC {btc*100:.2f}% skip cycle")
            time.sleep(LOOP_SLEEP); continue
        rate_hits = 0
        for symbol in symbols:
            try:
                bars = fetch_ohlcv(ex, symbol)
                time.sleep(REQ_SLEEP)
                node = find_node(bars)
                if node is None or node["kind"] == "dead":
                    continue
                short = symbol.split("/")[0]
                if node["kind"] == "armed":
                    aid = f"{symbol}_{node.get('pump_ts') or node['pump_bar']}_armed"
                    if aid in armed_sent:
                        continue
                    armed_sent.add(aid)
                    rec = rec_base(symbol, node, btc)
                    append_jsonl(ARMED_PATH, rec)
                    save_state({"sent": list(sent)[-400:], "armed": list(armed_sent)[-400:], "last": last_bar})
                    send_telegram(
                        f"VAL-FADE ARMED | {symbol}\n"
                        f"лимит {rec['entry']}\nстоп  {rec['stop']}\n"
                        f"TP1 {rec['grid15']}\nTP2 {rec['tp04']}\n"
                        f"pump {rec['pump_pct']:.1f}%  risk {rec['risk_pct']:.2f}%  planR {rec['plan_r']:.2f}\n"
                        f"не рынок — лимитка на узел"
                    )
                    print(f"[{now().strftime('%H:%M:%S')}] ARMED {short}  limit={rec['entry']}  pump={rec['pump_pct']:.1f}%")
                    continue
                sid = f"{symbol}_{node['bar_ts']}"
                if sid in sent:
                    continue
                prev = last_bar.get(symbol)
                if prev and (node["bar_ts"] - prev) / (15 * 60 * 1000) < COOLDOWN_BARS:
                    continue
                sent.add(sid)
                last_bar[symbol] = node["bar_ts"]
                rec = rec_base(symbol, node, btc)
                day = SIGNALS_DIR / f"{now().strftime('%Y-%m-%d')}.jsonl"
                append_jsonl(day, rec)
                save_state({"sent": list(sent)[-400:], "armed": list(armed_sent)[-400:], "last": last_bar})
                send_telegram(
                    f"VAL-FADE FIRED | {symbol}\n"
                    f"close под узлом  {rec['entry']}\n"
                    f"стоп {rec['stop']}  TP2 {rec['tp04']}\n"
                    f"pump {rec['pump_pct']:.1f}%\n"
                    f"если лимитки не было — не догонять"
                )
                print(f"[{now().strftime('%H:%M:%S')}] FIRED {short}  pump={rec['pump_pct']:.1f}%")
            except Exception as e:
                if is_rate_limit(e):
                    rate_hits += 1
                    wait = min(30, 8 * rate_hits)
                    print(f"[{now().strftime('%H:%M:%S')}] 10006 {symbol.split('/')[0]} sleep {wait}s")
                    time.sleep(wait)
                    if rate_hits >= 4:
                        print(f"[{now().strftime('%H:%M:%S')}] rate limit, skip rest of cycle")
                        break
                else:
                    print(f"{symbol.split('/')[0]}: {str(e)[:80]}")
                time.sleep(REQ_SLEEP)
        cycles += 1
        if cycles % 20 == 0:
            try:
                symbols = top_symbols(ex)
                print(f"[{now().strftime('%H:%M:%S')}] refresh symbols {len(symbols)}")
            except Exception as e:
                print(f"tickers: {e}")
        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()
