#!/usr/bin/env python3
"""VAL-FADE paper $300 / 1%. Ne chitaet paper/latest.txt DUMP."""

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


def load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def walk(rows):
    cap = START
    peak = START
    max_dd = 0.0
    out = []
    for rec in rows:
        tag = rec.get("tag") or ""
        r = float(rec.get("r") or 0)
        if tag in ("SKIP", "IDEA", "OPEN"):
            rec = dict(rec)
            rec["risk_usd"] = 0.0
            rec["pnl_usd"] = 0.0
            rec["equity"] = round(cap, 2)
            out.append(rec)
            continue
        risk = cap * RISK_PCT
        pnl = risk * r
        cap += pnl
        peak = max(peak, cap)
        dd = (peak - cap) / peak if peak else 0.0
        max_dd = max(max_dd, dd)
        rec = dict(rec)
        rec["risk_usd"] = round(risk, 2)
        rec["pnl_usd"] = round(pnl, 2)
        rec["equity"] = round(cap, 2)
        out.append(rec)
    return out, cap, max_dd


def main():
    PAPER.mkdir(parents=True, exist_ok=True)
    book = load_jsonl(BOOK)
    book.sort(key=lambda r: str(r.get("exit_ts") or r.get("entry_ts") or ""))
    walked, cap, dd = walk(book)
    taken = [r for r in walked if r.get("tag") not in ("SKIP", "IDEA", "OPEN")]
    wins = [r for r in taken if float(r.get("r") or 0) > 0]
    n = len(taken)
    wr = 100.0 * len(wins) / n if n else 0.0
    avgr = sum(float(r.get("r") or 0) for r in taken) / n if n else 0.0
    lines = [
        "=" * 64,
        "VAL-FADE PAPER  kniga A",
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        "desk=VAL-FADE  start=$300  risk=1%  NOT DUMP",
        "=" * 64,
        f"N taken {n}  open/skip {len(walked) - n}  WR {wr:.1f}%  AvgR {avgr:+.2f}",
        f"Start ${START:.2f} -> Final ${cap:.2f} ({(cap/START-1)*100:+.1f}%)",
        f"MaxDD {dd*100:.1f}%",
        "",
        f"{'id':<22} {'sym':<12} {'tag':<8} {'R':>6} {'eq':>8}",
    ]
    for r in walked:
        lines.append(
            f"{str(r.get('id') or '-'):<22} "
            f"{str(r.get('symbol') or '-'):<12} "
            f"{str(r.get('tag') or '-'):<8} "
            f"{float(r.get('r') or 0):+6.2f} "
            f"{float(r.get('equity') or 0):8.2f}"
        )
    text = "\n".join(lines) + "\n"
    LATEST.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {LATEST}")


if __name__ == "__main__":
    main()
