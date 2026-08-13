#!/usr/bin/env python3
"""
DUMP-STRUCT v3 | 90d top-150 | IS/OOS
Идея: памп → проторговка (range) → снос лоя range → шорт.

v3 изменения vs v2:
- 90d + IS (2/3) / OOS (1/3) — не один кусок
- stop: fixed 3% | hybrid min(range_high, cap) | sweep_high+cap
- качество sweep-бара: min body / vol ratio
- require close below range low (reclaim-style rejection optional)
"""

import ccxt, time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 90
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 120
SLEEP = 0.10
TP1_RR, TP2_RR = 1.6, 3.0
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN_PER_SYMBOL = 48
STOP_COOLDOWN = 96
IS_FRAC = 2.0 / 3.0

BASE = dict(
    pump_lb=12,
    pump_min=10.0,
    range_min=8,
    range_max=24,
    range_h_min=1.2,
    range_h_max=5.0,
    min_touches_low=2,
    vol_ratio=1.5,          # sweep bar vs MA20
    min_sweep_body=0.0,     # % body on sweep candle
    stop_mode="fixed",      # fixed | hybrid | sweep_high | range_high
    stop_pct=0.03,
    max_stop_pct=0.05,
    risk_pct=0.02,
    require_close_below=True,
)

VARIANTS = {
    # baseline-ish with fixed stop (lessons from impulse DUMP)
    "v3_fix3": {
        **BASE, "stop_mode": "fixed", "stop_pct": 0.03,
        "min_sweep_body": 3.0, "vol_ratio": 2.0,
    },
    "v3_fix3_body5": {
        **BASE, "stop_mode": "fixed", "stop_pct": 0.03,
        "min_sweep_body": 5.0, "vol_ratio": 2.5,
    },
    "v3_fix3_strict": {
        **BASE, "stop_mode": "fixed", "stop_pct": 0.03,
        "pump_min": 12.0, "min_touches_low": 3, "vol_ratio": 2.5,
        "min_sweep_body": 4.0, "range_min": 10, "range_h_max": 4.0,
    },
    # hybrid: stop = min(range_high, entry*(1+cap)) but at least entry*(1+floor)
    "v3_hyb": {
        **BASE, "stop_mode": "hybrid", "stop_pct": 0.02, "max_stop_pct": 0.045,
        "min_sweep_body": 3.0, "vol_ratio": 2.0,
    },
    "v3_hyb_strict": {
        **BASE, "stop_mode": "hybrid", "stop_pct": 0.025, "max_stop_pct": 0.04,
        "pump_min": 12.0, "min_touches_low": 3, "vol_ratio": 2.5,
        "min_sweep_body": 4.0, "range_min": 10,
    },
    # classic struct stops (often wide)
    "v3_range_hi": {
        **BASE, "stop_mode": "range_high", "max_stop_pct": 0.05,
        "min_sweep_body": 3.0, "vol_ratio": 2.0,
    },
    "v3_sweep_hi": {
        **BASE, "stop_mode": "sweep_high", "max_stop_pct": 0.04,
        "min_sweep_body": 3.0, "vol_ratio": 2.0,
    },
    # tighter range definition
    "v3_tight_rng": {
        **BASE, "stop_mode": "fixed", "stop_pct": 0.03,
        "range_min": 10, "range_max": 18, "range_h_min": 1.5, "range_h_max": 3.5,
        "min_touches_low": 3, "vol_ratio": 2.0, "min_sweep_body": 3.5,
    },
}

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}})


def had_pump_before(ohlcv, range_start, pump_lb, pump_min):
    a = range_start - pump_lb
    if a < 1:
        return False
    window = ohlcv[a:range_start]
    if not window:
        return False
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    if lo > 0 and (hi - lo) / lo * 100 >= pump_min:
        return True
    for c in window:
        o, cl = c[1], c[4]
        if o > 0 and (cl - o) / o * 100 >= pump_min:
            return True
    return False


def count_low_touches(rng_bars, rl):
    if rl <= 0:
        return 0
    n = 0
    for c in rng_bars:
        # low within 0.25% of range low
        if c[3] <= rl * 1.0025:
            n += 1
    return n


def make_stop(entry, high, rh, cfg):
    mode = cfg["stop_mode"]
    if mode == "fixed":
        stop = entry * (1 + cfg["stop_pct"])
    elif mode == "sweep_high":
        stop = high * 1.001
        if (stop - entry) / entry > cfg["max_stop_pct"]:
            return None
    elif mode == "range_high":
        stop = rh * 1.001
        if (stop - entry) / entry > cfg["max_stop_pct"]:
            return None
    elif mode == "hybrid":
        floor = entry * (1 + cfg["stop_pct"])
        cap = entry * (1 + cfg["max_stop_pct"])
        struct = rh * 1.001
        stop = max(floor, min(struct, cap))
    else:
        return None
    if stop <= entry:
        return None
    return stop


def find_signal_at(ohlcv, i, cfg):
    if i < 50 or i >= len(ohlcv) - 1:
        return None

    bar = ohlcv[i]
    o, h, l, c, v = bar[1], bar[2], bar[3], bar[4], bar[5]
    if c >= o:
        return None  # need red sweep candle

    body = (o - c) / o * 100 if o > 0 else 0
    if body < cfg["min_sweep_body"]:
        return None

    if i >= 21:
        vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
        if vol_ma > 0 and v < vol_ma * cfg["vol_ratio"]:
            return None

    for L in range(cfg["range_min"], cfg["range_max"] + 1):
        range_start = i - L
        if range_start < 2:
            continue
        if not had_pump_before(ohlcv, range_start, cfg["pump_lb"], cfg["pump_min"]):
            continue

        rng_bars = ohlcv[range_start:i]  # exclude current sweep bar
        if len(rng_bars) < cfg["range_min"]:
            continue

        rh = max(x[2] for x in rng_bars)
        rl = min(x[3] for x in rng_bars)
        mid = (rh + rl) / 2
        if mid <= 0:
            continue
        height_pct = (rh - rl) / mid * 100
        if height_pct < cfg["range_h_min"] or height_pct > cfg["range_h_max"]:
            continue

        touches = count_low_touches(rng_bars, rl)
        if touches < cfg["min_touches_low"]:
            continue

        # sweep: low breaks range low
        if l >= rl:
            continue
        if cfg["require_close_below"] and c >= rl:
            continue

        entry = c
        stop = make_stop(entry, h, rh, cfg)
        if stop is None:
            continue
        risk = stop - entry
        if risk <= 0:
            continue
        risk_pct = risk / entry
        if risk_pct < 0.005:
            continue

        return {
            "ts": bar[0],
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk * TP1_RR,
            "tp2": entry - risk * TP2_RR,
            "risk": risk,
            "risk_pct_px": risk_pct * 100,
            "range_h": round(height_pct, 2),
            "range_bars": L,
            "touches": touches,
            "body": body,
            "bar_index": i,
        }
    return None


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
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s["result"] in ("STOP", "TP1->STOP") and s["bars"] == 1:
            onebar += 1
    total = stats["TOTAL"]
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сигналов нет")
        return lines
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    capital = peak = START_CAPITAL
    max_dd = 0.0
    for s in raw:
        capital += capital * rp * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    avg_stop = sum(s.get("stop_pct", 0) for s in raw) / total
    onebar_pct = onebar / losses * 100 if losses else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%) avg_stop={avg_stop:.2f}%",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
    ]
    return lines


def run_variant(name, cfg, by_sym):
    raw_all = []
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 120:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(50, n - 8):
            if i - last_i < COOLDOWN_PER_SYMBOL or i < stop_ban:
                continue
            sig = find_signal_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": sig["ts"], "result": res, "bars": bars, "r": r,
                "stop_pct": sig["risk_pct_px"],
                "split": "IS" if i < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-STRUCT {name}",
        (
            f"stop={cfg['stop_mode']} pump>={cfg['pump_min']}% "
            f"range={cfg['range_min']}-{cfg['range_max']}b "
            f"touches>={cfg['min_touches_low']} vol>={cfg['vol_ratio']}x body>={cfg['min_sweep_body']}%"
        ),
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, cfg["risk_pct"], label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP-STRUCT v3 | pump→range→sweep | 90d IS/OOS")
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
        f"DUMP-STRUCT v3 | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
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
    path = out / f"bt_DUMP_STRUCT_v3_{stamp}.txt"
    latest = out / "latest_dump_struct.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
