#!/usr/bin/env python3
"""
VAL-FADE v2 — отдельная стратегия, НЕ DUMP.
Live dump_scanner.py НЕ трогаем.

Отличие от v1: нет окна 8/16.
  слой 0: vol последних 96 баров / предыдущих 96 ≥ SHOCK
  импульс: swing high-low на lookback 96 или 192, range ≥ MIN_RANGE
  high импульса не в последних HOLD барах
  профиль 20 корзин, ≥2 HVN
  вход: медвежий close в премиуме над нижним HVN

Варианты: lb96_tight / lb96_struct / lb192_tight / lb192_struct
Риск 1%.
Запуск: python scanner/backtest_val_fade2.py
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
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN = 32
STOP_COOLDOWN = 96
IS_FRAC = 2.0 / 3.0
RISK_PCT = 0.01
N_BINS = 20
HVN_FRAC = 0.70
VOL_FAT = 2.0
SHOCK = 1.5
SHOCK_WIN = 96
HOLD = 6
PREMIUM = 0.55
TP1_RR_MIN = 0.8
MIN_STOP_PCT = 0.003
MAX_STOP_TIGHT = 0.08
MAX_STOP_STRUCT = 0.18

VARIANTS = {
    "lb96_tight": dict(lb=96, min_range=0.10, stop="tight"),
    "lb96_struct": dict(lb=96, min_range=0.10, stop="struct"),
    "lb192_tight": dict(lb=192, min_range=0.15, stop="tight"),
    "lb192_struct": dict(lb=192, min_range=0.15, stop="struct"),
}

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


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
        chunk = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=1000, since=earliest - 1000 * 15 * 60 * 1000)
        if not chunk:
            break
        chunk = [c for c in chunk if c[0] < earliest]
        if not chunk:
            break
        ohlcv = chunk + ohlcv
        time.sleep(SLEEP)
    return ohlcv[-need:]


def vol_shock(ohlcv, i):
    if i < 2 * SHOCK_WIN:
        return False
    a = sum(x[5] for x in ohlcv[i - SHOCK_WIN:i])
    b = sum(x[5] for x in ohlcv[i - 2 * SHOCK_WIN:i - SHOCK_WIN])
    return b > 0 and a / b >= SHOCK


def build_profile(window):
    if len(window) < 8:
        return None
    lo = min(x[3] for x in window)
    hi = max(x[2] for x in window)
    if lo <= 0 or hi <= lo:
        return None
    vols = [x[5] for x in window]
    med = sorted(vols)[len(vols) // 2]
    if med <= 0 or max(vols) < med * VOL_FAT:
        return None
    width = (hi - lo) / N_BINS
    bins = [0.0] * N_BINS
    for bar in window:
        bhi, blo, v = bar[2], bar[3], bar[5]
        span = bhi - blo
        for k in range(N_BINS):
            a = lo + k * width
            b = a + width
            if span <= 0:
                if a <= blo < b:
                    bins[k] += v
                continue
            overlap = max(0.0, min(bhi, b) - max(blo, a))
            if overlap > 0:
                bins[k] += v * (overlap / span)
    peak = max(bins) if bins else 0.0
    if peak <= 0:
        return None
    clusters = []
    k = 0
    while k < N_BINS:
        if bins[k] >= peak * HVN_FRAC:
            s = k
            while k + 1 < N_BINS and bins[k + 1] >= peak * HVN_FRAC:
                k += 1
            e = k
            a = lo + s * width
            b = lo + (e + 1) * width
            clusters.append({"lo": a, "hi": b, "mid": (a + b) / 2})
        k += 1
    if len(clusters) < 2:
        return None
    clusters.sort(key=lambda c: c["mid"])
    return {"lo": lo, "hi": hi, "upper": clusters[-1], "lower": clusters[0], "clusters": clusters}


def impulse_ok(ohlcv, i, lb, min_range):
    start = i - lb
    window = ohlcv[start:i]
    lo = min(x[3] for x in window)
    hi = max(x[2] for x in window)
    if lo <= 0 or (hi - lo) / lo < min_range:
        return None
    hi_idx = max(range(len(window)), key=lambda k: window[k][2])
    if hi_idx >= len(window) - HOLD:
        return None
    return {"lo": lo, "hi": hi, "start": start}


def fade_bar(ohlcv, i, impl, prof):
    o, h, l, c = ohlcv[i][1], ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
    if c >= o:
        return False
    rng = h - l
    if rng <= 0 or (c - l) / rng > 0.50:
        return False
    if h > impl["hi"] * 1.001:
        return False
    span = impl["hi"] - impl["lo"]
    if span <= 0 or (c - impl["lo"]) / span < PREMIUM:
        return False
    if c <= prof["lower"]["mid"]:
        return False
    return True


def build_trade(ohlcv, i, cfg):
    if i < cfg["lb"] + 2 * SHOCK_WIN or i >= len(ohlcv) - 3:
        return None, "NOBAR"
    if not vol_shock(ohlcv, i):
        return None, "NOSHOCK"
    impl = impulse_ok(ohlcv, i, cfg["lb"], cfg["min_range"])
    if not impl:
        return None, "NOIMP"
    prof = build_profile(ohlcv[impl["start"]:i])
    if not prof:
        return None, "NOPROF"
    if not fade_bar(ohlcv, i, impl, prof):
        return None, "NOFADE"
    entry = ohlcv[i][4]
    if cfg["stop"] == "tight":
        stop = ohlcv[i][2] * 1.001
        max_sp = MAX_STOP_TIGHT
    else:
        stop = impl["hi"] * 1.001
        max_sp = MAX_STOP_STRUCT
    if stop <= entry:
        return None, "WIDE"
    risk = stop - entry
    if risk / entry < MIN_STOP_PCT or risk / entry > max_sp:
        return None, "WIDE"
    below = [c for c in prof["clusters"] if c["mid"] < entry]
    if not below:
        return None, "NOTP"
    below.sort(key=lambda c: -c["mid"])
    tp1 = below[0]["mid"]
    tp2 = below[-1]["mid"] if len(below) > 1 else prof["lower"]["mid"]
    if tp2 >= tp1:
        tp2 = impl["lo"] + 0.15 * (impl["hi"] - impl["lo"])
    if tp1 >= entry or tp2 >= tp1:
        return None, "NOTP"
    r1 = (entry - tp1) / risk
    r2 = (entry - tp2) / risk
    if r1 < TP1_RR_MIN:
        return None, "TINY"
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "risk": risk,
            "bar_index": i, "stop_pct_px": risk / entry * 100, "r1": r1, "r2": r2}, "TAKE"


def outcome(ohlcv, trade):
    i = trade["bar_index"]
    entry, stop, tp1, tp2 = trade["entry"], trade["stop"], trade["tp1"], trade["tp2"]
    r1, r2 = trade["r1"], trade["r2"]
    tp1_hit = False
    for j in range(i + 1, len(ohlcv)):
        high, low = ohlcv[j][2], ohlcv[j][3]
        bars = j - i
        eff = entry if (tp1_hit and BE_AFTER_TP1) else stop
        if high >= eff:
            if tp1_hit and BE_AFTER_TP1:
                return "TP1->BE", bars, r1 * 0.5
            return "STOP", bars, -1.0
        if low <= tp2:
            return ("TP1+TP2", bars, (r1 + r2) / 2) if tp1_hit else ("TP2", bars, r2)
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", len(ohlcv) - i - 1, r1
    return "OPEN", len(ohlcv) - i - 1, 0.0


def summarize(raw, label):
    stats = defaultdict(float)
    onebar = 0
    stops_pct, r1s, r2s = [], [], []
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s["result"] == "STOP" and s["bars"] == 1:
            onebar += 1
        if s.get("stop_pct_px") is not None:
            stops_pct.append(s["stop_pct_px"])
        if s.get("r1") is not None:
            r1s.append(s["r1"]); r2s.append(s["r2"])
    total = int(stats["TOTAL"])
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сделок нет")
        return lines
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"]
    capital = peak = START_CAPITAL
    max_dd = 0.0
    for s in raw:
        capital += capital * RISK_PCT * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    onebar_pct = onebar / losses * 100 if losses else 0
    avg_stop = sum(stops_pct) / len(stops_pct) if stops_pct else 0
    avg_r1 = sum(r1s) / len(r1s) if r1s else 0
    avg_r2 = sum(r2s) / len(r2s) if r2s else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%) avg_stop={avg_stop:.2f}% avg_TP1={avg_r1:.2f}R avg_TP2={avg_r2:.2f}R",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
        f"STOP={int(stats['STOP'])} TP1+TP2={int(stats['TP1+TP2'])} TP2={int(stats['TP2'])} TP1={int(stats['TP1'])} BE={int(stats['TP1->BE'])} OPEN={int(stats['OPEN'])}",
    ]
    return lines


def run_variant(name, cfg, by_sym):
    raw_all = []
    funnel = defaultdict(int)
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < cfg["lb"] + 2 * SHOCK_WIN + 20:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(cfg["lb"] + 2 * SHOCK_WIN, n - 12):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            funnel["scan"] += 1
            trade, why = build_trade(ohlcv, i, cfg)
            if not trade:
                funnel[why] += 1
                continue
            last_i = i
            funnel["TAKE"] += 1
            res, bars, r = outcome(ohlcv, trade)
            if res == "STOP":
                stop_ban = trade["bar_index"] + STOP_COOLDOWN
            raw_all.append({"ts_ms": ohlcv[i][0], "result": res, "bars": bars, "r": r,
                            "stop_pct_px": trade["stop_pct_px"], "r1": trade["r1"], "r2": trade["r2"],
                            "split": "IS" if i < is_end else "OOS"})
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]
    keys = ("NOSHOCK", "NOIMP", "NOPROF", "NOFADE", "WIDE", "NOTP", "TINY")
    skip_bits = " ".join(f"{k}={funnel[k]}" for k in keys if funnel[k])
    lines = ["=" * 64, f"SUMMARY | VAL-FADE2 {name}",
             f"cfg={cfg} shock={SHOCK} hold={HOLD} | 60d | risk 1% | DUMP live untouched",
             f"funnel scan={funnel['scan']} TAKE={funnel['TAKE']} {skip_bits}", "=" * 64]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("VAL-FADE v2 | swing impulse + vol shock | NOT dump")
    print("Live DUMP scanner NOT changed")
    print("=" * 64)
    symbols = top_symbols()
    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 4000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 220:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)
    print(f"\nЗагружено: {len(by_sym)}\n")
    all_lines = [f"VAL-FADE2 | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
                 f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)} | risk={RISK_PCT*100:.0f}%",
                 "swing lookback 96/192 + vol shock 1.5x. dump_scanner.py не менялся.", ""]
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
    path = out / f"bt_VAL_FADE2_{stamp}.txt"
    latest = out / "latest_val_fade2.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
