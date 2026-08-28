#!/usr/bin/env python3
"""
DUMP TP3 lab — как далеко уходят сделки после TP2, и даёт ли
стоп→TP1 + цель TP3 больше, чем фиксация 3R.

Live dump_scanner.py НЕ трогаем.

base     — как live: 50% TP1 + BE, 50% TP2=3R
tp3_45   — после TP2 стоп остатка на TP1, цель 4.5R
tp3_5    — цель 5R
tp3_6    — цель 6R

R остатка: 0.5*1.6 + 0.5*exit_R
  TP3 hit → exit_R = 4.5/5/6
  возврат в TP1 → exit_R = 1.6  (итого 1.6 вместо текущих 2.3)

Запуск: python scanner/backtest_dump_tp3.py
Отчёт:  backtests/bt_DUMP_TP3_*.txt + latest_dump_tp3.txt
"""

import ccxt
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 60
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 80
SLEEP = 0.10
SWEEP_LOOKBACK = 10
MIN_BODY, MAX_BODY = 6.5, 9.0
VOL_RATIO = 3.0
PUMP_LB, PUMP_MIN = 6, 8.0
CLOSE_POS_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
START_CAPITAL = 300.0
COOLDOWN = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0
IS_FRAC = 2.0 / 3.0
RISK_PCT = 0.02
STOP_ATR_MULT = 0.35
MAX_RISK_PCT = 0.03
MIN_STOP_PCT = 0.004
EXT_LEVELS = (3.5, 4.0, 4.5, 5.0, 6.0, 8.0)

VARIANTS = {
    "base": dict(mode="base"),
    "tp3_45": dict(mode="tp3", tp3=4.5),
    "tp3_5": dict(mode="tp3", tp3=5.0),
    "tp3_6": dict(mode="tp3", tp3=6.0),
}

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def calc_atr(ohlcv, i, period=14):
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        h, l, pc = ohlcv[j][2], ohlcv[j][3], ohlcv[j - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def had_pump(ohlcv, i):
    if i < PUMP_LB + 1:
        return False
    for j in range(i - PUMP_LB, i):
        o, c = ohlcv[j][1], ohlcv[j][4]
        if o > 0 and (c - o) / o * 100 >= PUMP_MIN:
            return True
    window = ohlcv[i - PUMP_LB:i]
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    return lo > 0 and (hi - lo) / lo * 100 >= PUMP_MIN


def signal_bar(ohlcv, i):
    if i < 70 or i >= len(ohlcv) - 3:
        return None
    if not had_pump(ohlcv, i):
        return None
    o, h, l, c, v = ohlcv[i][1], ohlcv[i][2], ohlcv[i][3], ohlcv[i][4], ohlcv[i][5]
    if c >= o:
        return None
    body = (o - c) / o * 100
    if body < MIN_BODY or body > MAX_BODY:
        return None
    rng = h - l
    if rng <= 0:
        return None
    if (c - l) / rng > CLOSE_POS_MAX:
        return None
    if abs(c - o) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
    if vol_ma <= 0 or v < vol_ma * VOL_RATIO:
        return None
    if l > min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]):
        return None
    atr = calc_atr(ohlcv, i)
    struct = h + (atr * STOP_ATR_MULT if atr else h * 0.005)
    stop = min(struct, c * (1 + MAX_RISK_PCT))
    risk = stop - c
    if risk <= 0 or risk / c < MIN_STOP_PCT or risk / c > 0.20:
        return None
    return {
        "i": i, "ts": ohlcv[i][0],
        "c": c, "stop": stop, "risk": risk,
    }


def walk(ohlcv, sig, tp3_rr=None):
    i0 = sig["i"]
    entry, stop, risk = sig["c"], sig["stop"], sig["risk"]
    tp1 = entry - TP1_RR * risk
    tp2 = entry - TP2_RR * risk
    tp3 = entry - tp3_rr * risk if tp3_rr else None
    mfe_r = 0.0
    hit1 = hit2 = False
    bars = 0
    for j in range(i0 + 1, len(ohlcv)):
        high, low = ohlcv[j][2], ohlcv[j][3]
        bars = j - i0
        mfe_r = max(mfe_r, (entry - low) / risk if risk else 0)
        if not hit1:
            if high >= stop:
                return _pack("STOP", bars, -1.0, mfe_r, False, False)
            if low <= tp1:
                hit1 = True
            if low <= tp2:
                hit2 = True
                if tp3 is None:
                    r = (TP1_RR + TP2_RR) / 2 if hit1 else TP2_RR
                    tag = "TP1+TP2" if hit1 else "TP2"
                    return _pack(tag, bars, r, mfe_r, hit1, True)
                if low <= tp3:
                    return _pack("TP3", bars, 0.5 * TP1_RR + 0.5 * tp3_rr, mfe_r, True, True)
            continue
        if not hit2:
            if high >= entry:
                return _pack("BE", bars, TP1_RR * 0.5, mfe_r, True, False)
            if low <= tp2:
                hit2 = True
                if tp3 is None:
                    return _pack("TP1+TP2", bars, (TP1_RR + TP2_RR) / 2, mfe_r, True, True)
                if low <= tp3:
                    return _pack("TP3", bars, 0.5 * TP1_RR + 0.5 * tp3_rr, mfe_r, True, True)
            continue
        if high >= tp1:
            return _pack("TP2->TP1", bars, TP1_RR, mfe_r, True, True)
        if tp3 is not None and low <= tp3:
            return _pack("TP3", bars, 0.5 * TP1_RR + 0.5 * tp3_rr, mfe_r, True, True)
    if hit2 and tp3 is not None:
        return _pack("OPEN2", bars, TP1_RR, mfe_r, True, True)
    if hit1:
        return _pack("TP1", bars, TP1_RR, mfe_r, True, False)
    return _pack("OPEN", bars, 0.0, mfe_r, False, False)


def _pack(result, bars, r, mfe_r, hit1, hit2):
    return {"result": result, "bars": bars, "r": r, "mfe_r": mfe_r, "hit1": hit1, "hit2": hit2}


def top_symbols(n=MAX_SYMBOLS):
    print("markets + volume...")
    markets = exchange.load_markets()
    cands = [s for s, m in markets.items()
             if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")]
    try:
        tickers = exchange.fetch_tickers(cands)
        ranked = sorted(
            ((float((tickers.get(s) or {}).get("quoteVolume") or 0), s) for s in cands),
            reverse=True,
        )
        return [s for _, s in ranked[:n]]
    except Exception as e:
        print("tickers fail", e)
        return cands[:n]


def fetch_ohlcv_full(symbol, need):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=min(need, 1000))
    while len(ohlcv) < need:
        earliest = ohlcv[0][0]
        chunk = exchange.fetch_ohlcv(
            symbol, timeframe=TIMEFRAME, limit=1000,
            since=earliest - 1000 * 15 * 60 * 1000,
        )
        if not chunk:
            break
        chunk = [c for c in chunk if c[0] < earliest]
        if not chunk:
            break
        ohlcv = chunk + ohlcv
        time.sleep(SLEEP)
    return ohlcv[-need:]


def pctile(xs, p):
    if not xs:
        return 0.0
    ys = sorted(xs)
    k = min(len(ys) - 1, max(0, int(round((p / 100) * (len(ys) - 1)))))
    return ys[k]


def collect_trades(by_sym):
    raw = []
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 120:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(70, n - 12):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            sig = signal_bar(ohlcv, i)
            if not sig:
                continue
            last_i = i
            base = walk(ohlcv, sig, tp3_rr=None)
            if base["result"] in ("STOP",):
                stop_ban = sig["i"] + base["bars"] + STOP_COOLDOWN
            paths = {"base": base}
            for name, cfg in VARIANTS.items():
                if name == "base":
                    continue
                paths[name] = walk(ohlcv, sig, tp3_rr=cfg["tp3"])
            raw.append({
                "ts_ms": sig["ts"], "symbol": symbol,
                "split": "IS" if i < is_end else "OOS",
                "mfe_r": base["mfe_r"], "hit2": base["hit2"], "paths": paths,
            })
    raw.sort(key=lambda x: x["ts_ms"])
    return raw


def summarize_equity(rows, path_name, label):
    stats = defaultdict(float)
    onebar = losses = 0
    for s in rows:
        p = s["paths"][path_name]
        stats[p["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += p["r"]
        if p["result"] == "STOP":
            losses += 1
            if p["bars"] == 1:
                onebar += 1
    total = int(stats["TOTAL"])
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сделок нет")
        return lines
    wins = sum(1 for s in rows if s["paths"][path_name]["r"] > 0)
    capital = peak = START_CAPITAL
    max_dd = 0.0
    for s in rows:
        capital += capital * RISK_PCT * s["paths"][path_name]["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    onebar_pct = onebar / losses * 100 if losses else 0
    lines += [
        f"N={total} WR(r>0)={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%)",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
        f"STOP={int(stats['STOP'])} BE={int(stats['BE'])} "
        f"TP1+TP2={int(stats['TP1+TP2'])} TP2={int(stats['TP2'])} "
        f"TP3={int(stats['TP3'])} TP2->TP1={int(stats['TP2->TP1'])} "
        f"OPEN={int(stats['OPEN']+stats['OPEN2'])}",
    ]
    return lines


def extension_report(raw):
    hit = [s for s in raw if s["hit2"]]
    lines = [
        "=" * 64,
        "EXTENSION after reaching 3R (base TP2)",
        f"signals={len(raw)}  reached_TP2={len(hit)}  "
        f"({(len(hit)/len(raw)*100 if raw else 0):.1f}%)",
        "=" * 64,
    ]
    if not hit:
        lines.append("Нет сделок с TP2")
        return lines
    mfes = [s["mfe_r"] for s in hit]
    lines.append(
        f"MFE | TP2-subset: min={min(mfes):.2f}R p50={pctile(mfes,50):.2f}R "
        f"p80={pctile(mfes,80):.2f}R max={max(mfes):.2f}R"
    )
    all_m = [s["mfe_r"] for s in raw]
    lines.append(
        f"MFE | all signals: p50={pctile(all_m,50):.2f}R "
        f"p80={pctile(all_m,80):.2f}R max={max(all_m):.2f}R"
    )
    lines.append("Доля TP2-сделок, у которых MFE >= уровень (уже ушли дальше 3R):")
    for lv in EXT_LEVELS:
        n = sum(1 for x in mfes if x + 1e-9 >= lv)
        lines.append(f"  >= {lv:.1f}R : {n}/{len(hit)} ({n/len(hit)*100:.0f}%)")
    lines.append("Из тех, кто коснулся 3R, чем закончился runner (стоп на TP1 vs цель):")
    for name, lv in (("tp3_45", 4.5), ("tp3_5", 5.0), ("tp3_6", 6.0)):
        ok = sum(1 for s in hit if s["paths"][name]["result"] == "TP3")
        back = sum(1 for s in hit if s["paths"][name]["result"] == "TP2->TP1")
        opn = sum(1 for s in hit if s["paths"][name]["result"] == "OPEN2")
        lines.append(
            f"  цель {lv:.1f}R: TP3={ok}/{len(hit)} ({ok/len(hit)*100:.0f}%)  "
            f"stop@TP1={back}  OPEN={opn}"
        )
    lines.append("Чтобы TP3 бил текущие 2.3R на TP2-сделках, нужна доля TP3:")
    lines.append("  4.5R → ≥48%   5.0R → ≥41%   6.0R → ≥32%")
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP TP3 lab | extension after 3R + stop to TP1")
    print("Live scanner NOT changed")
    print("=" * 64)
    symbols = top_symbols()
    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 4000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 120:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)
    print(f"\nЗагружено: {len(by_sym)}\n")
    raw = collect_trades(by_sym)
    ext = extension_report(raw)
    for line in ext:
        print(line)
    all_lines = [
        f"DUMP tp3 | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)} | signals={len(raw)}",
        "base   = live 50% TP1 + 50% TP2=3R",
        "tp3_*  = после TP2 стоп остатка на TP1, цель 4.5/5/6R",
        "",
    ]
    all_lines.extend(ext)
    for name in VARIANTS:
        print(f">>> {name}")
        raw_is = [x for x in raw if x["split"] == "IS"]
        raw_oos = [x for x in raw if x["split"] == "OOS"]
        part = [
            "=" * 64,
            f"SUMMARY | DUMP-TP3 {name}",
            f"cfg={VARIANTS[name]} | 60d | live untouched",
            "=" * 64,
        ]
        for label, chunk in [("FULL", raw), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
            part.extend(summarize_equity(chunk, name, label))
        part.append("")
        for line in part:
            print(line)
        all_lines.extend(part)
    root = Path(__file__).resolve().parent.parent
    out = root / "backtests"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out / f"bt_DUMP_TP3_{stamp}.txt"
    latest = out / "latest_dump_tp3.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
