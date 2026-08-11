#!/usr/bin/env python3
"""
DUMP lab | struct max_stop sweep | 60d top-150
Ищем баланс: N сделок vs 1-bar STOP vs Avg R
"""

import ccxt, time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 60
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 80
SLEEP = 0.10
SWEEP_LOOKBACK = 10
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
TP1_RR, TP2_RR = 1.6, 3.0
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN_PER_SYMBOL = 32
STOP_COOLDOWN = 96

BASE = dict(pump_lb=6, pump_min=8.0, min_body=5.0, max_body=9.0, vol_ratio=2.5, risk_pct=0.02)

VARIANTS = {
    "cap_2pct":   {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.02, "mode": "cap"},
    "s035_max08": {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.08, "mode": "struct"},
    "s035_max10": {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.10, "mode": "struct"},
    "s035_max12": {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.12, "mode": "struct"},
    "s035_max15": {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.15, "mode": "struct"},
    "s050_max12": {**BASE, "stop_atr": 0.50, "max_stop_pct": 0.12, "mode": "struct"},
    "s035_max12_r3": {**BASE, "stop_atr": 0.35, "max_stop_pct": 0.12, "mode": "struct", "risk_pct": 0.03},
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
    atr = calc_atr(ohlcv, i)
    entry = c
    raw_stop = h + (atr * cfg["stop_atr"] if atr else h * 0.005)
    if cfg["mode"] == "cap":
        stop = min(raw_stop, entry * (1 + cfg["max_stop_pct"]))
    else:
        stop = raw_stop
        if (stop - entry) / entry > cfg["max_stop_pct"]:
            return None
    risk = stop - entry
    if risk <= 0 or risk / entry < 0.002:
        return None
    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "risk_pct_px": risk / entry * 100,
        "body": body, "bar_index": i,
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


def run_variant(name, cfg, by_sym):
    raw = []
    onebar = 0
    risk_pcts = []
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 100:
            continue
        last_i, stop_ban = -999, -1
        for i in range(70, len(ohlcv) - 8):
            if i - last_i < COOLDOWN_PER_SYMBOL or i < stop_ban:
                continue
            sig = check_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            risk_pcts.append(sig["risk_pct_px"])
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
                if bars == 1:
                    onebar += 1
            raw.append({"ts_ms": sig["ts"], "result": res, "bars": bars, "r": r})
    raw.sort(key=lambda x: x["ts_ms"])
    stats = defaultdict(int)
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
    capital = peak = START_CAPITAL
    max_dd = 0.0
    rp = cfg["risk_pct"]
    for s in raw:
        capital += capital * rp * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    total = stats["TOTAL"]
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    avg_stop = sum(risk_pcts) / len(risk_pcts) if risk_pcts else 0
    lines = [
        "=" * 64,
        f"SUMMARY | DUMP {name}",
        f"mode={cfg['mode']} atr={cfg['stop_atr']} max_stop={cfg['max_stop_pct']*100:.0f}% "
        f"$risk={rp*100:.0f}% | avg_stop={avg_stop:.2f}%",
        "=" * 64,
    ]
    if total == 0:
        lines.append("Сигналов нет")
        return lines
    lines += [
        f"Всего: {total}",
        f"Wins:  {wins} ({wins/total*100:.1f}%)",
        f"BE:    {stats['TP1->BE']}",
        f"STOP:  {losses} ({losses/total*100:.1f}%)  | 1-bar STOP: {onebar}",
        f"OPEN:  {stats['OPEN']}",
    ]
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            lines.append(f"  {r}: {stats[r]}")
    lines += [
        "",
        f"Start ${START_CAPITAL:.2f} → Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%)",
        f"Total R {stats['sum_R']:+.1f} | Avg {stats['sum_R']/total:+.2f}R | MaxDD {max_dd:.1f}%",
        "",
    ]
    return lines


def main():
    print("=" * 64)
    print("DUMP max_stop sweep | 60d | top-150")
    print("=" * 64)
    symbols = top_symbols()
    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 100:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)
    print(f"\nЗагружено: {len(by_sym)}\n")
    all_lines = [
        f"DUMP max_stop sweep | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}", "",
    ]
    for name, cfg in VARIANTS.items():
        print(f">>> {name}")
        part = run_variant(name, cfg, by_sym)
        for line in part:
            print(line)
        all_lines.extend(part)
        all_lines.append("")
    root = Path(__file__).resolve().parent.parent
    out = root / "backtests"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out / f"bt_DUMP_SWEEP_{stamp}.txt"
    latest = out / "latest_dump.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
