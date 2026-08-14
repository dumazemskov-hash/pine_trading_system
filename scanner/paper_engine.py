#!/usr/bin/env python3
"""
Paper Engine — DUMP-only (основная стратегия v0.2b).

Читает только signals_dump/*.jsonl
Приоритет equity: version dump-v0.2b
Старые v0.2 (без b) — отдельно в отчёте, не в основной equity.
Risk 2%, BE after TP1, start $300.

Запуск: python scanner/paper_engine.py
"""

import ccxt
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump"
OUT_DIR = ROOT / "paper"
START_CAPITAL = 300.0
RISK_PCT = 0.02
TP1_RR = 1.6
TP2_RR = 3.0
BE_AFTER_TP1 = True

PRIMARY_VERSIONS = ("v0.2b", "dump-v0.2b")

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def is_primary(ver: str) -> bool:
    v = (ver or "").lower()
    return any(p in v for p in PRIMARY_VERSIONS)


def load_signals():
    by_key = {}
    if not SIGNALS_DIR.exists():
        return []
    for path in sorted(SIGNALS_DIR.glob("*.jsonl")):
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
                key = (rec["symbol"], bar_ts)
                ver = str(rec.get("version", ""))
                score = (1 if is_primary(ver) else 0, ver)
                prev = by_key.get(key)
                if prev is None or score > prev["_score"]:
                    rec["_version"] = ver
                    rec["_primary"] = is_primary(ver)
                    rec["_score"] = score
                    by_key[key] = rec
    out = list(by_key.values())
    out.sort(key=lambda r: (r.get("bar_ts") or 0))
    return out


def resolve(sig):
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


def run_equity(trades, label):
    capital = START_CAPITAL
    peak = capital
    max_dd = 0.0
    closed = open_n = 0
    lines = [
        f"--- Equity | {label} | risk {RISK_PCT*100:.0f}% | BE after TP1 ---",
        f"{'time':16} {'sym':10} {'ver':12} {'result':10} {'R':>6} {'risk$':>8} {'pnl$':>8} {'equity':>10}",
        "-" * 78,
    ]
    for t in trades:
        risk_usd = capital * RISK_PCT
        if t["result"] == "OPEN":
            open_n += 1
            pnl, r_use = 0.0, 0.0
        elif t["result"] == "TP1":
            open_n += 1
            r_use = t["r"]
            pnl = risk_usd * r_use
            capital += pnl
        elif t["result"] == "ERROR":
            r_use, pnl = 0.0, 0.0
        else:
            closed += 1
            r_use = t["r"]
            pnl = risk_usd * r_use
            capital += pnl

        peak = max(peak, capital)
        dd = (peak - capital) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        ver = (t.get("version") or "?")[:12]
        lines.append(
            f"{t['time']:16} {t['symbol']:10} {ver:12} {t['result']:10} "
            f"{r_use:+5.1f}R {risk_usd:8.2f} {pnl:+8.2f} {capital:10.2f}"
        )

    counted = [t for t in trades if t["result"] not in ("OPEN", "ERROR")]
    total_r = sum(t["r"] for t in counted)
    n = len(counted)
    lines += [
        "",
        f"Сигналов:  {len(trades)}  | закрыто: {closed}  | open: {open_n}",
        f"Start:     ${START_CAPITAL:.2f}",
        f"Final:     ${capital:.2f}  ({(capital/START_CAPITAL-1)*100:+.1f}%)",
        f"Total R:   {total_r:+.1f}R",
        f"Avg R:     {(total_r/n if n else 0):+.2f}R",
        f"Max DD:    {max_dd:.1f}%",
        "",
    ]
    return lines, capital, total_r, n, max_dd


def main():
    signals = load_signals()
    print("=" * 64)
    print("PAPER ENGINE | DUMP-only | $300 | BE after TP1")
    print(f"Сигналов в signals_dump: {len(signals)}")
    print("=" * 64)

    if not signals:
        print("Нет сигналов в signals_dump/")
        return

    trades = []
    for sig in signals:
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
            "symbol": name,
            "body": round(body, 1),
            "result": result,
            "bars": bars,
            "r": r,
            "extra": extra,
            "version": sig.get("_version", ""),
            "primary": sig.get("_primary", False),
            "entry": float(sig["entry"]),
            "stop": float(sig["stop"]),
            "tp1": float(sig["tp1"]),
            "tp2": float(sig["tp2"]),
        })
        tag = "v0.2b" if sig.get("_primary") else "old"
        print(f"{tstr} | DUMP | {name:10} | {tag:5} | {result:10} | {bars:3}б | {r:+.1f}R  {extra}")
        time.sleep(0.15)

    trades_sorted = sorted(trades, key=lambda t: t["time"])
    primary = [t for t in trades_sorted if t["primary"]]
    legacy = [t for t in trades_sorted if not t["primary"]]

    header = [
        f"PAPER report | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        f"DUMP-only | focus v0.2b | risk {RISK_PCT*100:.0f}% | BE after TP1 | start ${START_CAPITAL:.0f}",
        f"Всего в jsonl: {len(trades_sorted)}  | v0.2b: {len(primary)}  | legacy dump: {len(legacy)}",
        "",
    ]

    lines = list(header)
    if primary:
        eq_lines, final, total_r, n, max_dd = run_equity(primary, "DUMP v0.2b (PRIMARY)")
        lines.extend(eq_lines)
    else:
        lines += [
            "--- PRIMARY v0.2b ---",
            "Пока нет сигналов dump-v0.2b в signals_dump/.",
            "Запусти DUMP-сканер на ветке dump и копи сигналы.",
            "",
        ]
        final, total_r, n, max_dd = START_CAPITAL, 0.0, 0, 0.0

    if legacy:
        leg_lines, _, _, _, _ = run_equity(legacy, "legacy DUMP (не v0.2b, справочно)")
        lines.extend(leg_lines)

    lines += [
        "=" * 64,
        "PRIMARY = только dump-v0.2b (body 6.5-9, vol>=3, stop 3%).",
        "RAID в paper больше не считается.",
        "Бумажная торговля. Реальных ордеров нет.",
        "=" * 64,
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
        "mode": "DUMP-only",
        "primary_version": "dump-v0.2b",
        "start": START_CAPITAL,
        "final_primary": round(final, 2),
        "total_r_primary": round(total_r, 2),
        "n_primary": n,
        "max_dd_pct": round(max_dd, 2),
        "trades_primary": primary,
        "trades_legacy": legacy,
    }
    (OUT_DIR / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nСохранено: paper/latest.txt  paper/state.json")
    print("Готово. Push all")


if __name__ == "__main__":
    main()
