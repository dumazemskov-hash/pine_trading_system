#!/usr/bin/env python3
"""Count ARMED -> wick / FIRED / miss on ~14d top50. No live orders."""
from __future__ import annotations
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from val_scanner import (  # noqa: E402
    find_node, top_symbols, make_exchange, is_rate_limit, REQ_SLEEP,
    PUMP_LB, HOLD,
)

LOOK_BARS = 16

def classify(bars, armed_i, entry, stop):
    wick = False
    end = min(len(bars) - 1, armed_i + LOOK_BARS)
    for j in range(armed_i + 1, end + 1):
        hi, lo, cl = bars[j][2], bars[j][3], bars[j][4]
        if lo <= entry:
            if cl < entry:
                return "FIRED"
            if hi >= stop:
                return "WICK_STOP"
            wick = True
            if cl > entry:
                return "WICK_FAKE"
        if hi >= stop and not wick:
            return "DEAD_STOP"
    if wick:
        return "WICK_OPEN"
    return "MISS"

def main():
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    start = PUMP_LB + HOLD + 20
    print(f"FAKE-LAB  top{len(symbols)}  walk from bar {start}  look {LOOK_BARS}")
    tally = {}
    n_armed = 0
    seen = set()
    for symbol in symbols:
        try:
            raw = ex.fetch_ohlcv(symbol, "15m", limit=250)
            time.sleep(REQ_SLEEP)
        except Exception as e:
            print(f"{symbol.split('/')[0]}: {str(e)[:60]}")
            if is_rate_limit(e):
                time.sleep(12)
            continue
        bars = raw[:-1] if len(raw) >= 2 else raw
        if len(bars) < start + 10:
            continue
        short = symbol.split("/")[0]
        found = 0
        for last in range(start, len(bars)):
            node = find_node(bars[: last + 1])
            if node is None or node["kind"] != "armed":
                continue
            pid = (symbol, node.get("pump_ts") or node["entry"])
            if pid in seen:
                continue
            seen.add(pid)
            n_armed += 1
            found += 1
            tag = classify(bars, last, node["entry"], node["stop"])
            tally[tag] = tally.get(tag, 0) + 1
            print(f"  {short:<10} {tag}")
        print(f"{short} armed_now {found}")
    print("----")
    print(f"ARMED {n_armed}")
    for k in sorted(tally):
        pct = 100.0 * tally[k] / n_armed if n_armed else 0
        print(f"{k:<12} {tally[k]:3d}  {pct:.0f}%")
    fake = tally.get("WICK_FAKE", 0) + tally.get("WICK_STOP", 0)
    print(f"FILL_THEN_FAIL {fake}  ({100.0*fake/n_armed if n_armed else 0:.0f}%)")

if __name__ == "__main__":
    main()
