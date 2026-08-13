#!/usr/bin/env python3
"""
DUMP dynamic-stop + OOS lab
Входы: body 6.5-9% vol>=3x pump>=8% (v0.2b entry, не трогаем)
Стоп: fixed / ATR / struct(high+ATR) / hybrid
Метрики: FULL + IS (первые 2/3) + OOS (последняя 1/3) — не подгоняем под один кусок.
"""

import ccxt, time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 90
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 80
SLEEP = 0.10
SWEEP_LOOKBACK = 10
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN_PER_SYMBOL = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0
IS_FRAC = 2.0 / 3.0  # in-sample = first 2/3 of each series

ENTRY = dict(pump_lb=6, pump_min=8.0, min_body=6.5, max_body=9.0, vol_ratio=3.0, risk_pct=0.02)

# stop_mode:
#   fixed   — entry * (1 + stop_pct)
#   atr     — entry + ATR * atr_mult, cap max_stop_pct
#   struct  — high + ATR * atr_mult, cap max_stop_pct (стоп за high)
#   hybrid  — max(entry*(1+min_pct), min(high+ATR*atr_mult, entry*(1+max_stop_pct)))
VARIANTS = {
    "fixed_3":   {**ENTRY, "stop_mode": "fixed",  "stop_pct": 0.03, "atr_mult": 0.0,  "max_stop_pct": 0.03, "min_pct": 0.03},
    "fixed_25":  {**ENTRY, "stop_mode": "fixed",  "stop_pct": 0.025,"atr_mult": 0.0,  "max_stop_pct": 0.025,"min_pct": 0.025},
    "atr_10_c5": {**ENTRY, "stop_mode": "atr",    "stop_pct": 0.0,  "atr_mult": 1.0,  "max_stop_pct": 0.05, "min_pct": 0.0},
    "atr_15_c5": {**ENTRY, "stop_mode": "atr",    "stop_pct": 0.0,  "atr_mult": 1.5,  "max_stop_pct": 0.05, "min_pct": 0.0},
    "atr_10_c4": {**ENTRY, "stop_mode": "atr",    "stop_pct": 0.0,  "atr_mult": 1.0,  "max_stop_pct": 0.04, "min_pct": 0.0},
    "struct_c5": {**ENTRY, "stop_mode": "struct", "stop_pct": 0.0,  "atr_mult": 0.35, "max_stop_pct": 0.05, "min_pct": 0.0},
    "struct_c6": {**ENTRY, "stop_mode": "struct", "stop_pct": 0.0,  "atr_mult": 0.35, "max_stop_pct": 0.06, "min_pct": 0.0},
    "hybrid_2_5":{**ENTRY, "stop_mode": "hybrid", "stop_pct": 0.0,  "atr_mult": 0.5,  "max_stop_pct": 0.05, "min_pct": 0.02},
    "hybrid_25_5":{**ENTRY,"stop_mode": "hybrid", "stop_pct": 0.0,  "atr_mult": 0.5,  "max_stop_pct": 0.05, "min_pct": 0.025},
}

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}})


def calc_atr(ohlcv, i, period=14):
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        h, l, pc = ohlcv[j][2], ohlcv[j][3], ohlcv[j - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def had_pump(ohlcv, i, pump_lb, pump_min):
    if i < pump_lb + 1:
        return False
    for j in range(i - pump_lb, i):
        o, c = ohlcv[j][1], ohlcv[j][4]
        if o > 0 and (c - o) / o * 100 >= pump_min:
            return True
    window = ohlcv[i - pump_lb:i]
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    return lo > 0 and (hi - lo) / lo * 100 >= pump_min


def make_stop(entry, high, atr, cfg):
    mode = cfg["stop_mode"]
    max_s = entry * (1 + cfg["max_stop_pct"])
    atr_v = atr if atr else entry * 0.01

    if mode == "fixed":
        stop = entry * (1 + cfg["stop_pct"])
    elif mode == "atr":
        stop = entry + atr_v * cfg["atr_mult"]
        stop = min(stop, max_s)
    elif mode == "struct":
        stop = high + atr_v * cfg["atr_mult"]
        if stop > max_s:
            return None  # слишком широкий — skip trade
    elif mode == "hybrid":
        floor = entry * (1 + cfg["min_pct"])
        struct = high + atr_v * cfg["atr_mult"]
        stop = max(floor, min(struct, max_s))
    else:
        return None

    if stop <= entry:
        return None
    if (stop - entry) / entry > cfg["max_stop_pct"] + 1e-9 and mode != "struct":
        stop = max_s
    if stop <= entry:
        return None
    return stop


def check_at(ohlcv, i, cfg):
    if i < 70 or i >= len(ohlcv) - 1:
        return None
    if not had_pump(ohlcv, i, cfg["pump_lb"], cfg["pump_min"]):
        return None
    last = ohlcv[i]
    o, h, l, c, v = last[1], last[2], last[3], last[4], last[5]
    if c >= o:
        return None
    body = (o - c) / o * 100
    if body < cfg["min_body"] or body > cfg["max_body"]:
        return None
    rng = h - l
    if rng <= 0:
        return None
    if (c - l) / rng > CLOSE_IN_RANGE_MAX:
        return None
    if abs(c - o) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
    if v < vol_ma * cfg["vol_ratio"]:
        return None
    if l > min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]):
        return None

    entry = c
    atr = calc_atr(ohlcv, i)
    stop = make_stop(entry, h, atr, cfg)
    if stop is None:
        return None
    risk = stop - entry
    if risk <= 0 or risk / entry < 0.005:
        return None
    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body": body, "bar_index": i,
        "stop_pct_px": risk / entry * 100,
    }


def outcome(ohlcv, sig):
    i = sig["bar_index"]
    entry, stop, tp1, tp2 = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"]
    tp1_hit = False
    for j in range(i + 1, len(ohlcv)):
        high, low = ohlcv[j][2], ohlcv[j][3]
        bars = j - i
        eff = entry if (tp1_hit and BE_AFTER_TP1) else stop
        if high >= eff:
            if tp1_hit and BE_AFTER_TP1:
                return "TP1->BE", bars, TP1_RR * 0.5
            if tp1_hit:
                return "TP1->STOP", bars, TP1_RR * 0.5 - 0.5
            return "STOP", bars, -1.0
        if low <= tp2:
            return ("TP1+TP2", bars, (TP1_RR + TP2_RR) / 2) if tp1_hit else ("TP2", bars, TP2_RR)
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", len(ohlcv) - i - 1, TP1_RR
    return "OPEN", len(ohlcv) - i - 1, 0.0


def top_symbols(n=MAX_SYMBOLS):
    print("markets + volume...")
    markets = exchange.load_markets()
    cands = [s for s, m in markets.items()
             if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")]
    try:
        tickers = exchange.fetch_tickers(cands)
        ranked = sorted(((float((tickers.get(s) or {}).get("quoteVolume") or 0), s) for s in cands), reverse=True)
        return [s for _, s in ranked[:n]]
    except Exception as e:
        print("tickers fail", e)
        return cands[:n]


def fetch_ohlcv_full(symbol, need):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=min(need, 1000))
    while len(ohlcv) < need:
        earliest = ohlcv[0][0]
        chunk = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=1000,
                                    since=earliest - 1000 * 15 * 60 * 1000)
        if not chunk:
            break
        chunk = [c for c in chunk if c[0] < earliest]
        if not chunk:
            break
        ohlcv = chunk + ohlcv
        time.sleep(SLEEP)
    return ohlcv[-need:]


def summarize(raw, rp, label):
    stats = defaultdict(int)
    onebar = 0
    stops_pct = []
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s["result"] in ("STOP", "TP1->STOP") and s["bars"] == 1:
            onebar += 1
        stops_pct.append(s.get("stop_pct_px", 0))
    total = stats["TOTAL"]
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сигналов нет")
        return lines, 0.0, 0.0
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    capital = peak = START_CAPITAL
    max_dd = 0.0
    for s in raw:
        capital += capital * rp * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    avg_stop = sum(stops_pct) / len(stops_pct) if stops_pct else 0
    onebar_pct = onebar / losses * 100 if losses else 0
    lines += [
        f"N={total}  WR={wins/total*100:.1f}%  1-bar={onebar}({onebar_pct:.0f}%)  avg_stop={avg_stop:.2f}%",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%)  "
        f"AvgR {stats['sum_R']/total:+.2f}  MaxDD {max_dd:.1f}%",
    ]
    return lines, stats["sum_R"] / total, capital


def run_variant(name, cfg, by_sym):
    raw_all = []
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 120:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(70, n - 8):
            if i - last_i < COOLDOWN_PER_SYMBOL or i < stop_ban:
                continue
            sig = check_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
            split = "IS" if i < is_end else "OOS"
            raw_all.append({
                "ts_ms": sig["ts"], "result": res, "bars": bars, "r": r,
                "stop_pct_px": sig["stop_pct_px"], "split": split,
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    mode = cfg["stop_mode"]
    desc = f"mode={mode}"
    if mode == "fixed":
        desc += f" stop={cfg['stop_pct']*100:.1f}%"
    elif mode == "atr":
        desc += f" ATR×{cfg['atr_mult']} cap={cfg['max_stop_pct']*100:.0f}%"
    elif mode == "struct":
        desc += f" high+ATR×{cfg['atr_mult']} cap={cfg['max_stop_pct']*100:.0f}%"
    else:
        desc += f" floor={cfg['min_pct']*100:.1f}% ATR×{cfg['atr_mult']} cap={cfg['max_stop_pct']*100:.0f}%"

    lines = [
        "=" * 64,
        f"SUMMARY | DUMP {name}",
        f"{desc} | entry body6.5-9 vol>=3 | TP 1.6/3R | 90d IS/OOS",
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        part, _, _ = summarize(chunk, cfg["risk_pct"], label)
        lines.extend(part)
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP dynamic-stop + OOS lab | 90d top-150")
    print("IS = first 2/3 bars | OOS = last 1/3 | entry = v0.2b filters")
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
    all_lines = [
        f"DUMP dyn-stop OOS | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)} | IS_FRAC={IS_FRAC:.2f}",
        "",
    ]
    for name, cfg in VARIANTS.items():
        print(f">>> {name}")
        part = run_variant(name, cfg, by_sym)
        for line in part:
            print(line)
        all_lines.extend(part)
    root = Path(__file__).resolve().parent.parent
    out = root / "backtests"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out / f"bt_DUMP_DYNSTOP_{stamp}.txt"
    latest = out / "latest_dump.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
