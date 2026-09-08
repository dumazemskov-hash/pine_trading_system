#!/usr/bin/env python3
"""Honest VAL book: all ARMED. Fake exit = close of touch bar. FIRED walks TP/stop."""
from __future__ import annotations
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from val_scanner import (  # noqa: E402
    find_node, top_symbols, make_exchange, is_rate_limit, REQ_SLEEP,
    PUMP_LB, HOLD, GRID_R,
)

LOOK_BARS = 48
FETCH = 1000
TAKER = 0.00055
START = 300.0
RISK_PCT = 0.01

def fee_r(entry, stop):
    risk_pct = (stop - entry) / entry if entry else 0
    if risk_pct <= 0:
        return 0.0
    return (2.0 * TAKER) / risk_pct

def walk_fired(bars, start_i, entry, stop, tp2):
    risk = stop - entry
    if risk <= 0:
        return "ERR", 0.0
    plan_r = (entry - tp2) / risk if tp2 and tp2 < entry else GRID_R
    tp1 = entry - GRID_R * risk
    fr = fee_r(entry, stop)
    tp1_hit = False
    for c in bars[start_i + 1:]:
        hi, lo = c[2], c[3]
        if hi >= (entry if tp1_hit else stop):
            tag, r = ("BE", 0.5 * GRID_R) if tp1_hit else ("STOP", -1.0)
            return tag, r - fr
        if lo <= tp2:
            r = (0.5 * GRID_R + 0.5 * plan_r) if tp1_hit else plan_r
            return "TP2", r - fr
        if lo <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", 0.5 * GRID_R - fr
    return "OPEN", 0.0

def resolve(bars, armed_i, entry, stop, tp2):
    risk = stop - entry
    if risk <= 0:
        return "ERR", 0.0
    fr = fee_r(entry, stop)
    wick = False
    end = min(len(bars) - 1, armed_i + LOOK_BARS)
    for j in range(armed_i + 1, end + 1):
        hi, lo, cl = bars[j][2], bars[j][3], bars[j][4]
        if lo <= entry:
            if cl < entry:
                tag, r = walk_fired(bars, j, entry, stop, tp2)
                return "FIRED_" + tag, r
            if hi >= stop:
                return "WICK_STOP", -1.0 - fr
            if cl > entry:
                return "WICK_FAKE", (entry - cl) / risk - fr
            wick = True
        elif hi >= stop and not wick:
            return "DEAD", 0.0
    return ("WICK_OPEN", 0.0) if wick else ("MISS", 0.0)

def main():
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    start = PUMP_LB + HOLD + 20
    print("VAL HONEST  all ARMED  fake=close  FIRED=walk")
    cap = START
    peak = START
    dd = 0.0
    taken = []
    tally = {}
    seen = set()
    lines = []
    for n, symbol in enumerate(symbols, 1):
        short = symbol.split("/")[0]
        try:
            raw = ex.fetch_ohlcv(symbol, "15m", limit=FETCH)
            time.sleep(REQ_SLEEP)
        except Exception as e:
            print(f"{short}: {str(e)[:60]}")
            if is_rate_limit(e):
                time.sleep(12)
            continue
        bars = raw[:-1] if len(raw) >= 2 else raw
        if len(bars) < start + 10:
            continue
        found = 0
        for last in range(start, len(bars)):
            node = find_node(bars[: last + 1])
            if node is None or node["kind"] != "armed":
                continue
            pid = (symbol, node.get("pump_ts") or round(node["entry"], 8))
            if pid in seen:
                continue
            seen.add(pid)
            tag, r = resolve(bars, last, node["entry"], node["stop"], node["tp04"])
            tally[tag] = tally.get(tag, 0) + 1
            found += 1
            if tag not in ("DEAD", "MISS", "WICK_OPEN", "ERR"):
                cap += cap * RISK_PCT * r
                peak = max(peak, cap)
                dd = max(dd, (peak - cap) / peak if peak else 0.0)
                taken.append(r)
            lines.append(f"{short:<10} {tag:<14} {r:+5.2f}R")
        print(f"[{n}/{len(symbols)}] {short:<10} {found}  N {len(taken)}")
    n = len(taken)
    wr = 100.0 * sum(1 for x in taken if x > 0) / n if n else 0.0
    avgr = sum(taken) / n if n else 0.0
    print("----")
    for row in lines:
        print(row)
    print("----")
    for k in sorted(tally):
        print(f"{k:<16} {tally[k]}")
    print(
        f"HONEST  ${cap:.0f} ({(cap / START - 1) * 100:+.1f}%)  "
        f"taken {n}  ARMED {sum(tally.values())}  WR {wr:.0f}%  "
        f"AvgR {avgr:+.2f}  DD {dd * 100:.0f}%"
    )
    print("taken = fill (FIRED / FAKE / WICK_STOP). DEAD/MISS = 0, не в WR.")

if __name__ == "__main__":
    main()
