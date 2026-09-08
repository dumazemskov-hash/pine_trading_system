#!/usr/bin/env python3
"""ARMED outcomes + R on WICK_FAKE (exit = close of touch bar)."""
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
FETCH = 1000

def classify(bars, armed_i, entry, stop):
    risk = stop - entry
    if risk <= 0:
        return "ERR", None
    wick = False
    end = min(len(bars) - 1, armed_i + LOOK_BARS)
    for j in range(armed_i + 1, end + 1):
        hi, lo, cl = bars[j][2], bars[j][3], bars[j][4]
        if lo <= entry:
            if cl < entry:
                return "FIRED", None
            # fill at entry on this bar
            r_close = (entry - cl) / risk  # short; close above entry => negative
            r_mae = (entry - hi) / risk    # worst high of the bar
            if hi >= stop:
                return "WICK_STOP", -1.0
            wick = True
            if cl > entry:
                return "WICK_FAKE", (r_close, r_mae, cl, hi)
        if hi >= stop and not wick:
            return "DEAD_STOP", None
    if wick:
        return "WICK_OPEN", None
    return "MISS", None

def main():
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    start = PUMP_LB + HOLD + 20
    print(f"FAKE-LAB  top{len(symbols)}  fetch {FETCH}")
    tally = {}
    n_armed = 0
    seen = set()
    fakes = []
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
            n_armed += 1
            found += 1
            tag, extra = classify(bars, last, node["entry"], node["stop"])
            tally[tag] = tally.get(tag, 0) + 1
            if tag == "WICK_FAKE" and extra:
                r_close, r_mae, cl, hi = extra
                fakes.append((short, r_close, r_mae, node["entry"], cl, node["stop"]))
                print(f"  FAKE {short:<10} R_close {r_close:+.2f}  MAE {r_mae:+.2f}")
        print(f"[{n}/{len(symbols)}] {short:<10} armed {found}  total {n_armed}")
    print("----")
    print(f"ARMED {n_armed}")
    for k in sorted(tally):
        pct = 100.0 * tally[k] / n_armed if n_armed else 0
        print(f"{k:<12} {tally[k]:3d}  {pct:.0f}%")
    fake_n = tally.get("WICK_FAKE", 0) + tally.get("WICK_STOP", 0)
    print(f"FILL_THEN_FAIL {fake_n}  ({100.0 * fake_n / n_armed if n_armed else 0:.0f}%)")
    if fakes:
        avg = sum(x[1] for x in fakes) / len(fakes)
        avg_m = sum(x[2] for x in fakes) / len(fakes)
        print(f"WICK_FAKE N={len(fakes)}  avg R_close {avg:+.2f}  avg MAE {avg_m:+.2f}")
        print("R_close = выход по close бара касания. MAE = хай того же бара / риск.")

if __name__ == "__main__":
    main()
