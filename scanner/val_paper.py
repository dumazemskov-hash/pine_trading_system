#!/usr/bin/env python3
"""VAL paper v2 only. Old LIMIT 80% book disabled. Times = MSK."""
from __future__ import annotations
import json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ccxt
except ImportError:
    ccxt = None

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "signals_val"
PAPER = ROOT / "paper"
LATEST = PAPER / "val_latest.txt"
START = 300.0
RISK_PCT = 0.01
GRID_R = 1.5
TAKER = 0.00055
OK_VER = {"val-fade-v2"}
SKIP_FILES = {"armed.jsonl"}
MSK = timezone(timedelta(hours=3))

def msk(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(MSK).strftime("%m-%d %H:%M")

def load_signals():
    by_key = {}
    if not SIGNALS.exists():
        return []
    for path in sorted(SIGNALS.glob("*.jsonl")):
        if path.name in SKIP_FILES or path.name.startswith("limit"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("version") not in OK_VER:
                continue
            if rec.get("kind") == "armed":
                continue
            if "entry" not in rec or "symbol" not in rec:
                continue
            key = (rec["symbol"], int(rec.get("bar_ts") or 0))
            by_key[key] = rec
    out = list(by_key.values())
    out.sort(key=lambda r: int(r.get("bar_ts") or 0))
    return out

def fee_r(entry, stop):
    risk_pct = (stop - entry) / entry if entry else 0
    if risk_pct <= 0:
        return 0.0
    return (2.0 * TAKER) / risk_pct

def resolve(ex, sig):
    symbol = sig["symbol"]
    entry = float(sig["entry"])
    stop = float(sig["stop"])
    tp2 = float(sig.get("tp04") or 0)
    tp1 = float(sig.get("grid15") or (entry - GRID_R * (stop - entry)))
    bar_ts = int(sig.get("bar_ts") or 0)
    risk = stop - entry
    if risk <= 0:
        return "ERR", 0.0, 0.0
    plan_r = (entry - tp2) / risk if tp2 and tp2 < entry else GRID_R
    fr = fee_r(entry, stop)
    try:
        raw = ex.fetch_ohlcv(symbol, "15m", since=bar_ts - 15 * 60 * 1000 if bar_ts else None, limit=300)
    except Exception:
        return "ERR", 0.0, 0.0
    bars = [c for c in raw if c[0] > bar_ts] if bar_ts else raw
    if not bars:
        return "OPEN", 0.0, 0.0
    tp1_hit = False
    for c in bars:
        hi, lo = c[2], c[3]
        if hi >= (entry if tp1_hit else stop):
            tag, r = ("BE", 0.5 * GRID_R) if tp1_hit else ("STOP", -1.0)
            return tag, r - fr, fr
        if lo <= tp1:
            tp1_hit = True
            continue
        if tp1_hit and lo <= tp2:
            r = 0.5 * GRID_R + 0.5 * plan_r
            return "TP2", r - fr, fr
    if tp1_hit:
        return "TP1", 0.5 * GRID_R - fr, fr
    return "OPEN", 0.0, 0.0

def main():
    PAPER.mkdir(parents=True, exist_ok=True)
    banner = "VAL paper v2 only. LIMIT 80% book off. time=MSK\n"
    sigs = load_signals()
    if not sigs:
        text = banner + "пока нет сделок val-fade-v2\n"
        LATEST.write_text(text, encoding="utf-8")
        print(text, end="")
        return
    if ccxt is None:
        print("need ccxt")
        return
    ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}})
    cap = START
    peak = START
    dd = 0.0
    taken = []
    lines = []
    for sig in sigs:
        tag, r, fr = resolve(ex, sig)
        name = str(sig["symbol"]).split("/")[0].replace("USDT", "").replace(":", "")
        ts = int(sig.get("bar_ts") or 0)
        tstr = msk(ts) if ts else "?"
        pump = float(sig.get("pump_pct") or 0)
        if tag not in ("OPEN", "ERR"):
            cap += cap * RISK_PCT * r
            peak = max(peak, cap)
            dd = max(dd, (peak - cap) / peak if peak else 0.0)
            taken.append(r)
        extra = f"  fee {fr:.2f}R" if fr else ""
        lines.append(f"{tstr}  {name:<8}  {tag:<4}  {r:+5.2f}R{extra}  pump {pump:.0f}%")
        time.sleep(0.12)
    n = len(taken)
    wr = 100.0 * sum(1 for x in taken if x > 0) / n if n else 0.0
    avgr = sum(taken) / n if n else 0.0
    head = (
        f"VAL v2  ${cap:.0f} ({(cap / START - 1) * 100:+.1f}%)  "
        f"N={n}  WR {wr:.0f}%  AvgR {avgr:+.2f}  DD {dd * 100:.0f}%"
    )
    text = banner + head + "\n" + "\n".join(lines) + "\n" + head + "\n"
    LATEST.write_text(text, encoding="utf-8")
    print(text, end="")

if __name__ == "__main__":
    main()
