#!/usr/bin/env python3
"""
Paper Engine — виртуальная торговля по live-сигналам.

Читает signals/*.jsonl + signals_dump/*.jsonl
Считает позиции на истории 15m, equity с $300.
RAID risk 4%, DUMP risk 2%, BE after TP1.
Сохраняет paper/latest.txt и paper/state.json

Запуск: python scanner/paper_engine.py
"""

import ccxt
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = [
    (ROOT / "signals", "RAID", 0.04),
    (ROOT / "signals_dump", "DUMP", 0.02),
]
OUT_DIR = ROOT / "paper"
START_CAPITAL = 300.0
TP1_RR = 1.6
TP2_RR = 3.0
BE_AFTER_TP1 = True

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def load_signals():
    by_key = {}
    for folder, src, risk in DIRS:
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
                    bar_ts = int(rec.get("bar_ts") or 0)
                    key = (src, rec["symbol"], bar_ts)
                    ver = str(rec.get("version", ""))
                    score = ("v8.32" in ver or "v0.2" in ver, float(rec.get("risk_pct") or 0))
                    prev = by_key.get(key)
                    if prev is None or score > prev["_score"]:
                        rec["_src"] = src
                        rec["_risk"] = risk
                        rec["_score"] = score
                        by_key[key] = rec
    out = list(by_key.values())
    out.sort(key=lambda r: (r.get("bar_ts") or 0, r["_src"]))
    return out


def resolve(sig):
    """Прогнать сигнал по свечам → result, bars, R."""
    symbol = sig["symbol"]
    entry = float(sig["entry"])
    stop = float(sig["stop"])
    tp1 = float(sig["tp1"])
    tp2 = float(sig["tp2"])
    bar_ts = int(sig.get("bar_ts") or 0)

    try:
        ohlcv = exchange.fetch_ohlcv(
            symbol, "15m",
            since=bar_ts - 15 * 60 * 1000 if bar_ts else None,
            limit=300,
        )
    except Exception as e:
        return "ERROR", 0, 0.0, str(e)[:60]

    bars = [c for c in ohlcv if c[0] > bar_ts] if bar_ts else ohlcv
    if not bars:
        return "OPEN", 0, 0.0, "нет свечей"

    tp1_hit = False
    for i, c in enumerate(bars):
        high, low = c[2], c[3]
        n = i + 1
        eff = entry if (tp1_hit and BE_AFTER_TP1) else stop
        if high >= eff:
            if tp1_hit and BE_AFTER_TP1:
                return "TP1->BE", n, TP1_RR * 0.5, ""
            if tp1_hit:
                return "TP1->STOP", n, TP1_RR * 0.5 - 0.5, ""
            return "STOP", n, -1.0, ""
        if low <= tp2:
            if tp1_hit:
                return "TP1+TP2", n, (TP1_RR + TP2_RR) / 2, ""
            return "TP2", n, TP2_RR, ""
        if low <= tp1:
            tp1_hit = True

    last = bars[-1][4]
    if tp1_hit:
        return "TP1", len(bars), TP1_RR, f"last={last}"
    return "OPEN", len(bars), 0.0, f"last={last}"


def main():
    signals = load_signals()
    print("=" * 64)
    print("PAPER ENGINE | $300 start | BE after TP1")
    print(f"Сигналов: {len(signals)}")
    print("=" * 64)

    if not signals:
        print("Нет сигналов в signals/ / signals_dump/")
        return

    trades = []
    for sig in signals:
        src = sig["_src"]
        risk_pct = sig["_risk"]
        name = sig["symbol"].split("/")[0]
        body = float(sig.get("body_pct") or 0)
        bar_ts = int(sig.get("bar_ts") or 0)
        tstr = (
            datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if bar_ts else "?"
        )
        result, bars, r, extra = resolve(sig)
        trades.append({
            "time": tstr,
            "src": src,
            "symbol": name,
            "body": round(body, 1),
            "result": result,
            "bars": bars,
            "r": r,
            "risk_pct": risk_pct,
            "extra": extra,
            "entry": float(sig["entry"]),
            "stop": float(sig["stop"]),
            "tp1": float(sig["tp1"]),
            "tp2": float(sig["tp2"]),
        })
        print(f"{tstr} | {src:4} | {name:10} | {result:10} | {bars:3}б | {r:+.1f}R  {extra}")
        time.sleep(0.2)

    # equity — по времени, compound
    trades_sorted = sorted(trades, key=lambda t: t["time"])
    capital = START_CAPITAL
    peak = capital
    max_dd = 0.0
    closed = 0
    open_n = 0
    lines = [
        f"PAPER report | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        f"Start ${START_CAPITAL:.0f} | RAID risk 4% | DUMP risk 2% | BE after TP1",
        "",
        f"{'time':16} {'src':4} {'sym':10} {'result':10} {'R':>6} {'risk$':>8} {'pnl$':>8} {'equity':>10}",
        "-" * 72,
    ]

    for t in trades_sorted:
        risk_usd = capital * t["risk_pct"]
        # OPEN / TP1 still open: считаем unrealized 0 для equity closed-path;
        # для OPEN r=0; для TP1 open даём текущий r как бумажный
        if t["result"] == "OPEN":
            open_n += 1
            pnl = 0.0
            r_use = 0.0
        elif t["result"] == "TP1":
            open_n += 1
            r_use = t["r"]  # paper mark at TP1
            pnl = risk_usd * r_use
            capital += pnl
        elif t["result"] == "ERROR":
            r_use = 0.0
            pnl = 0.0
        else:
            closed += 1
            r_use = t["r"]
            pnl = risk_usd * r_use
            capital += pnl

        peak = max(peak, capital)
        dd = (peak - capital) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

        lines.append(
            f"{t['time']:16} {t['src']:4} {t['symbol']:10} {t['result']:10} "
            f"{r_use:+5.1f}R {risk_usd:8.2f} {pnl:+8.2f} {capital:10.2f}"
        )

    total_r = sum(t["r"] for t in trades_sorted if t["result"] not in ("OPEN", "ERROR"))
    n_count = sum(1 for t in trades_sorted if t["result"] not in ("OPEN", "ERROR"))
    lines += [
        "",
        "=" * 64,
        f"Сигналов:     {len(trades)}",
        f"Закрыто:      {closed}",
        f"Ещё open:     {open_n}",
        f"Start:        ${START_CAPITAL:.2f}",
        f"Final:        ${capital:.2f}  ({(capital/START_CAPITAL-1)*100:+.1f}%)",
        f"Total R:      {total_r:+.1f}R",
        f"Avg R:        {(total_r/n_count if n_count else 0):+.2f}R",
        f"Max DD:       {max_dd:.1f}%",
        "=" * 64,
        "Бумажная торговля. Реальных ордеров нет.",
    ]

    report = "\n".join(lines)
    print()
    print(report)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    (OUT_DIR / f"paper_{stamp}.txt").write_text(report + "\n", encoding="utf-8")
    (OUT_DIR / "latest.txt").write_text(report + "\n", encoding="utf-8")
    state = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "start": START_CAPITAL,
        "final": round(capital, 2),
        "max_dd_pct": round(max_dd, 2),
        "trades": trades_sorted,
    }
    (OUT_DIR / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено: paper/latest.txt  paper/state.json")
    print("Готово. Push all")


if __name__ == "__main__":
    main()
