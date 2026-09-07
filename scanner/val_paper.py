#!/usr/bin/env python3
"""VAL paper honest: fill = close of signal bar, not node_lo. $300 / 1%."""
from __future__ import annotations
import json, time
from datetime import datetime, timezone
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
MIN_PLAN_R = 0.8  # skip if close already ate the 0.4

def load_signals():
    by_key = {}
    if not SIGNALS.exists():
        return []
    for path in sorted(SIGNALS.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
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
    node = float(sig["entry"])
    stop = float(sig["stop"])
    tp2 = float(sig.get("tp04") or 0)
    bar_ts = int(sig.get("bar_ts") or 0)
    if stop <= node:
        return "ERR", 0.0, 0.0, 0.0
    try:
        raw = ex.fetch_ohlcv(symbol, "15m", since=bar_ts - 15 * 60 * 1000 if bar_ts else None, limit=300)
    except Exception:
        return "ERR", 0.0, 0.0, 0.0
    sig_bar = None
    after = []
    for c in raw:
        if c[0] == bar_ts:
            sig_bar = c
        elif c[0] > bar_ts:
            after.append(c)
    if sig_bar is None and raw:
        later = [c for c in raw if c[0] >= bar_ts]
        if later:
            sig_bar = later[0]
            after = later[1:]
    if sig_bar is None:
        return "OPEN", 0.0, 0.0, 0.0
    fill = float(sig_bar[4])  # close of break bar
    slip = (node - fill) / node * 100.0 if node else 0.0
    if fill >= stop:
        return "SKIP", 0.0, 0.0, slip
    if tp2 and fill <= tp2:
        return "LATE", 0.0, 0.0, slip
    risk = stop - fill
    if risk <= 0:
        return "SKIP", 0.0, 0.0, slip
    plan_r = (fill - tp2) / risk if tp2 and tp2 < fill else 0.0
    if plan_r < MIN_PLAN_R:
        return "THIN", 0.0, 0.0, slip
    tp1 = fill - GRID_R * risk
    fr = fee_r(fill, stop)
    if not after:
        return "OPEN", 0.0, 0.0, slip
    tp1_hit = False
    for c in after:
        hi, lo = c[2], c[3]
        if hi >= (fill if tp1_hit else stop):
            tag, r = ("BE", 0.5 * GRID_R) if tp1_hit else ("STOP", -1.0)
            return tag, r - fr, fr, slip
        if lo <= tp2:
            r = (0.5 * GRID_R + 0.5 * plan_r) if tp1_hit else plan_r
            return "TP2", r - fr, fr, slip
        if lo <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", 0.5 * GRID_R - fr, fr, slip
    return "OPEN", 0.0, 0.0, slip

def main():
    PAPER.mkdir(parents=True, exist_ok=True)
    sigs = load_signals()
    if not sigs:
        text = "VAL scan  no signals in signals_val/\n"
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
        tag, r, fr, slip = resolve(ex, sig)
        name = str(sig["symbol"]).split("/")[0].replace("USDT", "").replace(":", "")
        ts = int(sig.get("bar_ts") or 0)
        tstr = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M") if ts else "?"
        pump = float(sig.get("pump_pct") or 0)
        if tag not in ("OPEN", "ERR", "SKIP", "LATE", "THIN"):
            cap += cap * RISK_PCT * r
            peak = max(peak, cap)
            dd = max(dd, (peak - cap) / peak if peak else 0.0)
            taken.append(r)
        extra = f"  fee {fr:.2f}R" if fr else ""
        lines.append(
            f"{tstr}  {name:<8}  {tag:<4}  {r:+5.2f}R{extra}  slip {slip:+.1f}%  pump {pump:.0f}%"
        )
        time.sleep(0.15)
    n = len(taken)
    wr = 100.0 * sum(1 for x in taken if x > 0) / n if n else 0.0
    avgr = sum(taken) / n if n else 0.0
    head = (
        f"VAL close-fill  ${cap:.0f} ({(cap/START-1)*100:+.1f}%)  "
        f"N={n}  WR {wr:.0f}%  AvgR {avgr:+.2f}  DD {dd*100:.0f}%  "
        f"entry=close бара"
    )
    text = head + "\n" + "\n".join(lines) + "\n"
    LATEST.write_text(text, encoding="utf-8")
    print(text, end="")

if __name__ == "__main__":
    main()
