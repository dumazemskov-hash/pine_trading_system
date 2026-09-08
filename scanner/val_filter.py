#!/usr/bin/env python3
"""Honest VAL + filters: FIRED + next bar holds under node. No repeat pump. Pump cap 100%."""
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
PUMP_CAP = 100.0

def fee_r(entry, stop):
    risk_pct = (stop - entry) / entry if entry else 0
    if risk_pct <= 0:
        return 0.0
    return (2.0 * TAKER) / risk_pct

def walk(bars, start_i, entry, stop, tp2):
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

def first_fired(bars, armed_i, entry, stop):
    end = min(len(bars) - 1, armed_i + LOOK_BARS)
    for j in range(armed_i + 1, end + 1):
        hi, lo, cl = bars[j][2], bars[j][3], bars[j][4]
        if lo <= entry and cl < entry:
            return j
        if hi >= stop:
            return None
    return None

def main():
    ex = make_exchange()
    if ex is None:
        raise SystemExit("need ccxt")
    symbols = top_symbols(ex)
    start = PUMP_LB + HOLD + 20
    print(f"VAL FILTER  FIRED+hold  pump<={PUMP_CAP:.0f}%  one pump_ts")
    cap = START
    peak = START
    dd = 0.0
    taken = []
    tally = {}
    seen_pump = set()
    lines = []
    n_armed = 0
    for n, symbol in enumerate(symbols, 1):
        short = symbol.split("/")[0]
        try:
            raw = ex.fetch_ohlcv(symbol, "15m", limit=FETCH)
            time.sleep(REQ_SLEEP)
        except Exception as e:
            print(f"{short}: {str(e)[:70]}")
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
            if pid in seen_pump:
                continue
            n_armed += 1
            seen_pump.add(pid)
            if node["pump_pct"] > PUMP_CAP:
                tally["SKIP_PUMP"] = tally.get("SKIP_PUMP", 0) + 1
                continue
            fire = first_fired(bars, last, node["entry"], node["stop"])
            if fire is None:
                tally["NO_FIRE"] = tally.get("NO_FIRE", 0) + 1
                continue
            if fire + 1 >= len(bars):
                tally["NO_NEXT"] = tally.get("NO_NEXT", 0) + 1
                continue
            nxt = bars[fire + 1]
            if nxt[4] >= node["entry"]:
                tally["RECLAIM"] = tally.get("RECLAIM", 0) + 1
                continue
            tag, r = walk(bars, fire + 1, node["entry"], node["stop"], node["tp04"])
            name = "TAKE_" + tag
            tally[name] = tally.get(name, 0) + 1
            found += 1
            if tag not in ("OPEN", "ERR"):
                cap += cap * RISK_PCT * r
                peak = max(peak, cap)
                dd = max(dd, (peak - cap) / peak if peak else 0.0)
                taken.append(r)
            lines.append(f"{short:<10} {name:<14} {r:+5.2f}R  pump {node['pump_pct']:.0f}%")
        print(f"[{n}/{len(symbols)}] {short:<10} take {found}  taken {len(taken)}  armed {n_armed}")
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
        f"FILTER  ${cap:.0f} ({(cap / START - 1) * 100:+.1f}%)  "
        f"taken {n}  ARMED {n_armed}  WR {wr:.0f}%  AvgR {avgr:+.2f}  DD {dd * 100:.0f}%"
    )

if __name__ == "__main__":
    main()
