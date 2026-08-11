#!/usr/bin/env python3
"""
Проверка live-сигналов из signals/ и signals_dump/ (jsonl).
BE after TP1: после TP1 стоп = entry.
"""

import ccxt
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = [
    (ROOT / "signals", "RAID"),
    (ROOT / "signals_dump", "DUMP"),
]

TP1_RR = 1.6
TP2_RR = 3.0
BE_AFTER_TP1 = True

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def load_signals():
    """Все jsonl → unique by (src, symbol, bar_ts). Prefer higher version / risk."""
    by_key = {}
    for folder, src in DIRS:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.jsonl")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "entry" not in rec or "symbol" not in rec:
                        continue
                    bar_ts = rec.get("bar_ts") or 0
                    key = (src, rec["symbol"], bar_ts)
                    # prefer v8.32 / dump-v0.2 over older, and higher risk_pct
                    ver = str(rec.get("version", ""))
                    score = ("v8.32" in ver or "v0.2" in ver, float(rec.get("risk_pct") or 0))
                    prev = by_key.get(key)
                    if prev is None or score > prev["_score"]:
                        rec["_src"] = src
                        rec["_score"] = score
                        by_key[key] = rec
    out = list(by_key.values())
    out.sort(key=lambda r: (r.get("bar_ts") or 0, r["_src"]))
    return out


def check_one(sig):
    symbol = sig["symbol"]
    entry = float(sig["entry"])
    stop = float(sig["stop"])
    tp1 = float(sig["tp1"])
    tp2 = float(sig["tp2"])
    bar_ts = int(sig.get("bar_ts") or 0)

    try:
        ohlcv = exchange.fetch_ohlcv(
            symbol, timeframe="15m",
            since=bar_ts - 15 * 60 * 1000 if bar_ts else None,
            limit=300,
        )
    except Exception as e:
        return f"ERROR: {e}", None

    if not ohlcv:
        return "NO DATA", None

    bars = [c for c in ohlcv if c[0] > bar_ts] if bar_ts else ohlcv
    if not bars:
        return "OPEN (нет свечей после)", 0.0

    tp1_hit = False
    for i, c in enumerate(bars):
        high, low = c[2], c[3]
        n = i + 1
        eff_stop = entry if (tp1_hit and BE_AFTER_TP1) else stop

        if high >= eff_stop:
            if tp1_hit and BE_AFTER_TP1:
                return f"TP1->BE | через {n} свечей", TP1_RR * 0.5
            if tp1_hit:
                return f"TP1->STOP | через {n} свечей", TP1_RR * 0.5 - 0.5
            return f"STOP | через {n} свечей", -1.0

        if low <= tp2:
            if tp1_hit:
                return f"TP1+TP2 | через {n} свечей", (TP1_RR + TP2_RR) / 2
            return f"TP2 | через {n} свечей", TP2_RR

        if low <= tp1:
            tp1_hit = True

    last = bars[-1][4]
    if tp1_hit:
        return f"TP1 (open, last={last:.6g}) | {len(bars)} св", TP1_RR
    return f"OPEN last={last:.6g} | {len(bars)} св", 0.0


def main():
    signals = load_signals()
    if not signals:
        print("Нет jsonl в signals/ или signals_dump/")
        print(f"Искал: {[str(d) for d, _ in DIRS]}")
        return

    print("=" * 64)
    print(f"Проверка {len(signals)} live-сигналов (jsonl)")
    print("BE after TP1 | 15m Bybit")
    print("=" * 64)

    stats = defaultdict(lambda: defaultdict(int))
    sum_r = defaultdict(float)
    lines = []

    for sig in signals:
        src = sig["_src"]
        name = sig["symbol"].split("/")[0]
        body = float(sig.get("body_pct") or 0)
        ver = sig.get("version", "?")
        bar_ts = sig.get("bar_ts") or 0
        if bar_ts:
            tstr = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        else:
            tstr = (sig.get("logged_at") or "?")[:16]

        result, r = check_one(sig)
        tag = result.split("|")[0].strip()
        stats[src][tag.split()[0] if tag else "?"] += 1
        stats[src]["TOTAL"] += 1
        if r is not None:
            sum_r[src] += r

        line = f"{tstr} | {src:4} | {name:10} | body={body:5.1f}% | {result}"
        print(line)
        lines.append(line)
        time.sleep(0.25)

    print()
    print("=" * 64)
    print("SUMMARY")
    print("=" * 64)
    for src in ("RAID", "DUMP"):
        if stats[src]["TOTAL"] == 0:
            continue
        total = stats[src]["TOTAL"]
        wins = stats[src]["TP2"] + stats[src]["TP1+TP2"] + stats[src]["TP1"]
        # rough: keys that start with those
        wins = 0
        stops = 0
        be = 0
        open_n = 0
        for k, v in stats[src].items():
            if k == "TOTAL":
                continue
            if k in ("TP2", "TP1+TP2", "TP1"):
                wins += v
            elif k in ("STOP", "TP1->STOP"):
                stops += v
            elif k in ("TP1->BE",):
                be += v
            elif k in ("OPEN", "TP1"):
                open_n += v
        print(f"{src}: N={total}  wins≈{wins}  BE={be}  STOP≈{stops}  open≈{open_n}  sumR={sum_r[src]:+.1f}")
        for k, v in sorted(stats[src].items()):
            if k != "TOTAL":
                print(f"  {k}: {v}")
    print("=" * 64)
    print("Готово. Скопируй SUMMARY + строки и пришли.")


if __name__ == "__main__":
    main()
