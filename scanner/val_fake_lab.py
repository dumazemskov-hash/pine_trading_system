#!/usr/bin/env python3
"""Count ARMED -> wick / FIRED / miss on last 14d top50. No live orders."""
from __future__ import annotations
import os, time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from val_scanner import (  # noqa: E402
    find_node, top_symbols, make_exchange, fetch_ohlcv, is_rate_limit, REQ_SLEEP
)

LOOK_BARS = 16  # 4h after ARMED close

def classify(bars, armed_i, entry, stop):
    """armed_i = index of last closed bar when ARMED. Look forward."""
    wick = fired = miss = False
    end = min(len(bars) - 1, armed_i + LOOK_BARS)
    for j in range(armed_i + 1, end + 1):
        hi, lo, cl = bars[j][2], bars[j][3], bars[j][4]
        if lo <= entry:
            if cl < entry:
                return "FIRED"
            if hi >= stop:
                return "WICK_STOP"  # filled then stop same bar
            wick = True
            if cl > entry:
                return "WICK_FAKE"  # filled, close back above node
        if hi >= stop and not wick:
            return "DEAD_STOP"  # stop taken without fill
    if wick:
        return "WICK_OPEN"
    return "MISS"

def main():
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    print(f"FAKE-LAB  symbols {len(symbols)}  look {LOOK_BARS} bars")
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
        if len(bars) < 220:
            continue
        # walk closed bars, detect first ARMED per pump_ts
        for last in range(200, len(bars)):
            window = bars[: last + 1]
            node = find_node(window)
            if node is None or node["kind"] != "armed":
                continue
            pid = (symbol, node.get("pump_ts") or node["pump_bar"])
            if pid in seen:
                continue
            seen.add(pid)
            n_armed += 1
            tag = classify(bars, last, node["entry"], node["stop"])
            tally[tag] = tally.get(tag, 0) + 1
            print(f"  {symbol.split('/')[0]:<10} {tag}")
    print("----")
    print(f"ARMED {n_armed}")
    for k in sorted(tally):
        pct = 100.0 * tally[k] / n_armed if n_armed else 0
        print(f"{k:<12} {tally[k]:3d}  {pct:.0f}%")
    print("WICK_FAKE+WICK_STOP = fill then fail, not in FIRED book")

if __name__ == "__main__":
    main()
