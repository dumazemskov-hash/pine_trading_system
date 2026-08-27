#!/usr/bin/env python3
"""
Replay confirm-gate на живых paper-сигналах signals_dump/*.jsonl.

Для каждого сигнала смотрим первую закрытую 15m свечу ПОСЛЕ bar_ts:
  CANCEL если high1 >= original stop.

Считает три учёта:
  live     — как сейчас (без гейта)
  gate_opt — skip bounce, исходный R сделки (оптимистичный)
  gate_f   — skip bounce, вход по close1, стоп исходный, TP 1.6/3R от нового риска

Live dump_scanner НЕ трогаем.
"""

import ccxt
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_DIR = ROOT / "signals_dump"
OUT_DIR = ROOT / "backtests"
START = 300.0
RISK_PCT = 0.02
TP1_RR, TP2_RR = 1.6, 3.0
BE_AFTER_TP1 = True

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def load_signals():
    rows = []
    if not SIGNALS_DIR.exists():
        return rows
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
                rows.append(rec)
    rows.sort(key=lambda r: int(r.get("bar_ts") or 0))
    return rows


def resolve_live(entry, stop, tp1, tp2, bars):
    tp1_hit = False
    for i, c in enumerate(bars):
        high, low = c[2], c[3]
        n = i + 1
        eff = entry if (tp1_hit and BE_AFTER_TP1) else stop
        if high >= eff:
            if tp1_hit and BE_AFTER_TP1:
                return "TP1->BE", n, TP1_RR * 0.5
            if tp1_hit:
                return "TP1->STOP", n, TP1_RR * 0.5 - 0.5
            return "STOP", n, -1.0
        if low <= tp2:
            return ("TP1+TP2", n, (TP1_RR + TP2_RR) / 2) if tp1_hit else ("TP2", n, TP2_RR)
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", len(bars), TP1_RR
    return "OPEN", len(bars), 0.0


def equity(trades):
    cap = peak = START
    dd = 0.0
    total_r = 0.0
    n = 0
    for t in trades:
        if t["result"] in ("OPEN", "SKIP", "ERROR"):
            continue
        cap += cap * RISK_PCT * t["r"]
        peak = max(peak, cap)
        dd = max(dd, (peak - cap) / peak * 100 if peak else 0)
        total_r += t["r"]
        n += 1
    return cap, total_r, n, dd


def main():
    signals = load_signals()
    print("=" * 64)
    print("PAPER GATE REPLAY | cancel if high1 >= orig stop")
    print(f"signals_dump: {len(signals)}")
    print("=" * 64)
    live, opt, honest = [], [], []
    lines = [
        f"PAPER GATE REPLAY | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        "CANCEL if high(bar+1) >= original stop",
        "",
        f"{'time':16} {'sym':8} {'live':10} {'h1>=st':6} {'opt':10} {'F':10} {'h1%':6}",
        "-" * 72,
    ]

    for sig in signals:
        symbol = sig["symbol"]
        name = symbol.split("/")[0]
        entry = float(sig["entry"])
        stop = float(sig["stop"])
        tp1 = float(sig["tp1"])
        tp2 = float(sig["tp2"])
        bar_ts = int(sig.get("bar_ts") or 0)
        tstr = (
            datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if bar_ts else "?"
        )
        try:
            ohlcv = exchange.fetch_ohlcv(
                symbol, "15m",
                since=bar_ts - 15 * 60 * 1000 if bar_ts else None,
                limit=300,
            )
        except Exception as e:
            print(f"{tstr} {name} ERROR {e}")
            continue
        after = [c for c in ohlcv if c[0] > bar_ts]
        if not after:
            print(f"{tstr} {name} no bars")
            continue
        live_res, live_bars, live_r = resolve_live(entry, stop, tp1, tp2, after)
        bar1 = after[0]
        high1, close1 = bar1[2], bar1[4]
        bounce = high1 >= stop
        h1pct = (high1 - entry) / entry * 100 if entry else 0

        live.append({"result": live_res, "r": live_r})

        if bounce:
            opt.append({"result": "SKIP", "r": 0.0})
            honest.append({"result": "SKIP", "r": 0.0})
            opt_s, f_s = "SKIP", "SKIP"
        else:
            opt.append({"result": live_res, "r": live_r})
            risk_f = stop - close1
            if risk_f <= 0:
                honest.append({"result": "SKIP", "r": 0.0})
                opt_s, f_s = live_res, "SKIP"
            else:
                ftp1 = close1 - risk_f * TP1_RR
                ftp2 = close1 - risk_f * TP2_RR
                f_res, f_bars, f_r = resolve_live(close1, stop, ftp1, ftp2, after[1:])
                honest.append({"result": f_res, "r": f_r})
                opt_s, f_s = live_res, f_res

        line = (
            f"{tstr:16} {name:8} {live_res:10} {'YES' if bounce else 'no':6} "
            f"{opt_s:10} {f_s:10} {h1pct:5.1f}%"
        )
        lines.append(line)
        print(line)
        time.sleep(0.12)

    def block(title, trades):
        cap, total_r, n, dd = equity(trades)
        skipped = sum(1 for t in trades if t["result"] == "SKIP")
        stops = sum(1 for t in trades if t["result"] == "STOP")
        wins = sum(1 for t in trades if t["result"] in ("TP2", "TP1+TP2", "TP1"))
        be = sum(1 for t in trades if t["result"] == "TP1->BE")
        return [
            "",
            f"--- {title} ---",
            f"taken={n} skip={skipped} STOP={stops} TP={wins} BE={be}",
            f"TotalR {total_r:+.1f}  Final ${cap:.2f} ({(cap/START-1)*100:+.1f}%)  MaxDD {dd:.1f}%",
        ]

    lines += block("LIVE (как paper сейчас)", live)
    lines += block("GATE OPT (skip bounce, исходная сделка)", opt)
    lines += block("GATE F (skip bounce, entry=close1, stop=orig)", honest)
    lines += [
        "",
        "gate_opt = то, что звучит как таблетка: не берём сделку, если bar+1 вынес стоп.",
        "Fill оптимистичный: R исходной сделки. Честный вход — колонка F.",
        "Live scanner не изменён.",
        "",
    ]
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    (OUT_DIR / f"bt_DUMP_GATE_PAPER_{stamp}.txt").write_text(text, encoding="utf-8")
    (OUT_DIR / "latest_dump_gate_paper.txt").write_text(text, encoding="utf-8")
    print("Сохранено backtests/latest_dump_gate_paper.txt")


if __name__ == "__main__":
    main()
