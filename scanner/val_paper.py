#!/usr/bin/env python3
"""VAL paper: two books. limit=node_lo | close=signal bar close. $300 / 1%."""
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
MIN_PLAN_R = 0.8

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

def walk(after, fill, stop, tp2, tp1, plan_r, fr):
    if not after:
        return "OPEN", 0.0, fr
    tp1_hit = False
    for c in after:
        hi, lo = c[2], c[3]
        if hi >= (fill if tp1_hit else stop):
            tag, r = ("BE", 0.5 * GRID_R) if tp1_hit else ("STOP", -1.0)
            return tag, r - fr, fr
        if lo <= tp2:
            r = (0.5 * GRID_R + 0.5 * plan_r) if tp1_hit else plan_r
            return "TP2", r - fr, fr
        if lo <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", 0.5 * GRID_R - fr, fr
    return "OPEN", 0.0, fr

def split_bars(raw, bar_ts):
    sig_bar = None
    after = []
    for c in raw:
        if c[0] == bar_ts:
            sig_bar = c
        elif bar_ts and c[0] > bar_ts:
            after.append(c)
    if sig_bar is None:
        later = [c for c in raw if not bar_ts or c[0] >= bar_ts]
        if later:
            sig_bar = later[0]
            after = later[1:]
    return sig_bar, after

def resolve_limit(after, node, stop, tp2):
    risk = stop - node
    if risk <= 0:
        return "ERR", 0.0, 0.0
    plan_r = (node - tp2) / risk if tp2 and tp2 < node else GRID_R
    tp1 = node - GRID_R * risk
    fr = fee_r(node, stop)
    return walk(after, node, stop, tp2, tp1, plan_r, fr)

def resolve_close(sig_bar, after, node, stop, tp2):
    if sig_bar is None:
        return "OPEN", 0.0, 0.0, 0.0
    fill = float(sig_bar[4])
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
    tag, r, fr = walk(after, fill, stop, tp2, tp1, plan_r, fr)
    return tag, r, fr, slip

def score(rows):
    cap = START
    peak = START
    dd = 0.0
    taken = []
    lines = []
    for tstr, name, pump, tag, r, extra in rows:
        if tag not in ("OPEN", "ERR", "SKIP", "LATE", "THIN"):
            cap += cap * RISK_PCT * r
            peak = max(peak, cap)
            dd = max(dd, (peak - cap) / peak if peak else 0.0)
            taken.append(r)
        lines.append(f"{tstr}  {name:<8}  {tag:<4}  {r:+5.2f}R{extra}  pump {pump:.0f}%")
    n = len(taken)
    wr = 100.0 * sum(1 for x in taken if x > 0) / n if n else 0.0
    avgr = sum(taken) / n if n else 0.0
    head = (
        f"${cap:.0f} ({(cap/START-1)*100:+.1f}%)  "
        f"N={n}  WR {wr:.0f}%  AvgR {avgr:+.2f}  DD {dd*100:.0f}%"
    )
    return head, lines

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
    lim_rows = []
    cls_rows = []
    for sig in sigs:
        node = float(sig["entry"])
        stop = float(sig["stop"])
        tp2 = float(sig.get("tp04") or 0)
        bar_ts = int(sig.get("bar_ts") or 0)
        name = str(sig["symbol"]).split("/")[0].replace("USDT", "").replace(":", "")
        tstr = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M") if bar_ts else "?"
        pump = float(sig.get("pump_pct") or 0)
        try:
            raw = ex.fetch_ohlcv(sig["symbol"], "15m",
                                 since=bar_ts - 15 * 60 * 1000 if bar_ts else None, limit=300)
        except Exception:
            raw = []
        sig_bar, after = split_bars(raw, bar_ts)
        tag_l, r_l, fr_l = resolve_limit(after, node, stop, tp2)
        tag_c, r_c, fr_c, slip = resolve_close(sig_bar, after, node, stop, tp2)
        extra_l = f"  fee {fr_l:.2f}R" if fr_l else ""
        extra_c = (f"  fee {fr_c:.2f}R" if fr_c else "") + f"  slip {slip:+.1f}%"
        lim_rows.append((tstr, name, pump, tag_l, r_l, extra_l))
        cls_rows.append((tstr, name, pump, tag_c, r_c, extra_c))
        time.sleep(0.12)
    h_lim, _ = score(lim_rows)
    h_cls, _ = score(cls_rows)
    text = (
        "VAL LIMIT узел  " + h_lim + "\n"
        "VAL CLOSE бар   " + h_cls + "\n"
    )
    LATEST.write_text(text, encoding="utf-8")
    print(text, end="")
    print("LIMIT " + h_lim)
    print("CLOSE " + h_cls)

if __name__ == "__main__":
    main()
