#!/usr/bin/env python3
"""
DUMP v0.2b — filter lab (close_pos / trend / BTC regime)
Стоп fixed 3%, TP 1.6/3R, risk 2%. Entry base = body 6.5-9 vol>=3 pump>=8.
90d IS/OOS. Live не трогаем — только BT.

BTC фильтры (идея корреляции):
  btc_down     — BTC close[-1] < close[-7]  (15m, alt short только когда BTC уже слабый)
  btc_not_up   — BTC 6-bar change <= +1.5% (не шортим альты когда BTC резко растёт)
  btc_red      — последняя закрытая BTC свеча красная
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
MIN_BODY_TO_RANGE = 0.50
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0
IS_FRAC = 2.0 / 3.0
STOP_PCT = 0.03
RISK_PCT = 0.02
BTC_SYM = "BTC/USDT:USDT"

BASE = dict(
    pump_lb=6, pump_min=8.0, min_body=6.5, max_body=9.0, vol_ratio=3.0,
    close_pos_max=0.35,   # live default
    trend_bars=6,         # close[i-1] vs close[i-1-trend_bars]
    require_trend_up=False,
    btc_mode=None,        # None | down | not_up | red
    btc_not_up_pct=1.5,
)

VARIANTS = {
    "v02b_base":     {**BASE},
    "close20":       {**BASE, "close_pos_max": 0.20},
    "close25":       {**BASE, "close_pos_max": 0.25},
    "trend_up":      {**BASE, "require_trend_up": True},
    "close20_trend": {**BASE, "close_pos_max": 0.20, "require_trend_up": True},
    "btc_down":      {**BASE, "btc_mode": "down"},
    "btc_not_up":    {**BASE, "btc_mode": "not_up"},
    "btc_red":       {**BASE, "btc_mode": "red"},
    "close20_btc":   {**BASE, "close_pos_max": 0.20, "btc_mode": "not_up"},
    "close20_trend_btc": {**BASE, "close_pos_max": 0.20, "require_trend_up": True, "btc_mode": "not_up"},
}

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap", "fetchMarkets": ["linear"]}})


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


def btc_ok(btc, ts, cfg):
    """BTC regime at signal bar timestamp (same 15m bar)."""
    mode = cfg.get("btc_mode")
    if not mode or not btc:
        return True
    # find bar with same ts or last bar <= ts
    idx = None
    for j in range(len(btc) - 1, -1, -1):
        if btc[j][0] <= ts:
            idx = j
            break
    if idx is None or idx < 8:
        return True  # no data — don't block

    o, h, l, c = btc[idx][1], btc[idx][2], btc[idx][3], btc[idx][4]
    n = cfg.get("trend_bars", 6)
    prev = btc[idx - n][4] if idx >= n else btc[0][4]
    chg = (c - prev) / prev * 100 if prev else 0

    if mode == "down":
        return c < prev
    if mode == "not_up":
        return chg <= cfg.get("btc_not_up_pct", 1.5)
    if mode == "red":
        return c < o
    return True


def check_at(ohlcv, i, cfg, btc):
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
    close_pos = (c - l) / rng
    if close_pos > cfg["close_pos_max"]:
        return None
    if abs(c - o) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
    if v < vol_ma * cfg["vol_ratio"]:
        return None
    if l > min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]):
        return None

    if cfg.get("require_trend_up"):
        n = cfg["trend_bars"]
        if i < n + 1:
            return None
        # trend into dump: close of bar before signal higher than n bars earlier
        if ohlcv[i - 1][4] <= ohlcv[i - 1 - n][4]:
            return None

    if not btc_ok(btc, last[0], cfg):
        return None

    entry = c
    stop = entry * (1 + STOP_PCT)
    risk = stop - entry
    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body": body, "bar_index": i,
        "close_pos": close_pos,
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


def summarize(raw, label):
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
        capital += capital * RISK_PCT * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    onebar_pct = onebar / losses * 100 if losses else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%)",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
    ]
    return lines


def run_variant(name, cfg, by_sym, btc):
    raw_all = []
    for symbol, ohlcv in by_sym.items():
        if symbol == BTC_SYM:
            continue
        if len(ohlcv) < 120:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(70, n - 8):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            sig = check_at(ohlcv, i, cfg, btc)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": sig["ts"], "result": res, "bars": bars, "r": r,
                "split": "IS" if i < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    desc = (
        f"cpos<={cfg['close_pos_max']} trend_up={cfg['require_trend_up']} "
        f"btc={cfg.get('btc_mode') or 'off'} | stop 3% body6.5-9 vol>=3"
    )
    lines = [
        "=" * 64,
        f"SUMMARY | DUMP {name}",
        desc,
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP filter lab | close_pos / trend / BTC | 90d IS/OOS")
    print("=" * 64)
    symbols = top_symbols()
    if BTC_SYM not in symbols:
        symbols = [BTC_SYM] + symbols

    print(f"BTC 15m load ({BTC_SYM})...")
    try:
        btc = fetch_ohlcv_full(BTC_SYM, min(BARS_LIMIT, 4000))
        print(f"BTC bars={len(btc)}")
    except Exception as e:
        print("BTC fail", e)
        btc = []

    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        if symbol == BTC_SYM:
            continue
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 4000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 120:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)
    print(f"\nЗагружено: {len(by_sym)} + BTC\n")

    all_lines = [
        f"DUMP filters | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)} | BTC bars={len(btc)}",
        "",
    ]
    for name, cfg in VARIANTS.items():
        print(f">>> {name}")
        part = run_variant(name, cfg, by_sym, btc)
        for line in part:
            print(line)
        all_lines.extend(part)

    root = Path(__file__).resolve().parent.parent
    out = root / "backtests"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out / f"bt_DUMP_FILTERS_{stamp}.txt"
    latest = out / "latest_dump_filters.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
