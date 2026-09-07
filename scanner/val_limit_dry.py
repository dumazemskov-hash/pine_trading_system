#!/usr/bin/env python3
"""Dry-run: print sell-limit from signals_val/armed.jsonl. No live orders."""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMED = ROOT / "signals_val" / "armed.jsonl"

def main():
    if not ARMED.exists():
        print("нет signals_val/armed.jsonl — сначала старт VAL")
        return
    seen = set()
    rows = []
    for line in ARMED.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        key = (rec.get("symbol"), rec.get("pump_bar"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(rec)
    if not rows:
        print("armed.jsonl пустой")
        return
    print(f"DRY LIMIT  {len(rows)} armed  ордеров нет")
    for rec in rows[-20:]:
        name = str(rec["symbol"]).split("/")[0]
        print(
            f"SELL LIMIT {name:<10}  @{rec['entry']}  sl={rec['stop']}  "
            f"tp={rec['tp04']}  pump {rec.get('pump_pct')}%"
        )
    print("live ордер — отдельный шаг, когда будет ключ")

if __name__ == "__main__":
    main()
