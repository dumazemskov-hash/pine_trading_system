#!/usr/bin/env python3
"""VAL-FADE paper $300 / 1%. Not DUMP latest.txt."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / "paper"
START = 300.0
RISK_PCT = 0.01
BOOK = PAPER / "val_book.jsonl"
LATEST = PAPER / "val_latest.txt"

def load_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows

def main():
    PAPER.mkdir(parents=True, exist_ok=True)
    book = load_jsonl(BOOK)
    book.sort(key=lambda r: str(r.get("exit_ts") or r.get("entry_ts") or ""))
    cap = START
    peak = START
    dd = 0.0
    taken = []
    rows = []
    for rec in book:
        tag = rec.get("tag") or "-"
        r = float(rec.get("r") or 0)
        sym = (rec.get("symbol") or rec.get("id") or "?").replace("USDT", "")
        if tag in ("SKIP", "IDEA", "OPEN"):
            rows.append(f"{sym:<10}  {tag.lower()}")
            continue
        cap += cap * RISK_PCT * r
        peak = max(peak, cap)
        dd = max(dd, (peak - cap) / peak if peak else 0.0)
        taken.append(r)
        rows.append(f"{sym:<10}  {r:+5.1f}R  {tag}")
    n = len(taken)
    wr = 100.0 * sum(1 for x in taken if x > 0) / n if n else 0.0
    avgr = sum(taken) / n if n else 0.0
    head = (
        f"VAL  ${cap:.0f} ({(cap/START-1)*100:+.1f}%)  "
        f"N={n}  WR {wr:.0f}%  AvgR {avgr:+.2f}  DD {dd*100:.0f}%"
    )
    text = head + "\n" + ("\n".join(rows) if rows else "empty") + "\n"
    LATEST.write_text(text, encoding="utf-8")
    print(text, end="")

if __name__ == "__main__":
    main()
