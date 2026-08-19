#!/usr/bin/env python3
"""
DUMP tools lab — 60d IS/OOS
База entry v0.2b (body 6.5-9, vol>=3, pump>=8, stop 3%, TP 1.6/3R).
Поверх — инструменты из SMC / volume-profile / regime (GitHub synthesis):

  atr_cap      — skip если ATR/entry > X% (шум)
  cpos         — close в нижней части range
  btc_regime   — not_up / red / down
  vp_proxy     — dump с high vol-bar; skip если vol_ratio < X (уже есть) +
                 require signal range >= k * ATR (initiative)
  prior_green  — бар до сигнала зелёный (pump-leg рядом)
  no_cascade   — не 2-й сильный red подряд (body prev < 4%)
  score        — min composite: touches-like proxies (vol + cpos + pump fresh)
  wick_stop    — stop = high * 1.001, skip if risk > max%

Цель: найти что держит OOS, не подгонять IS.
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
MIN_BODY_TO_RANGE = 0.50
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0
IS_FRAC = 2.0 / 3.0
RISK_PCT = 0.02
BTC_SYM = "BTC/USDT:USDT"

BASE = dict(
    pump_lb=6, pump_min=8.0, min_body=6.5, max_body=9.0, vol_ratio=3.0,
    close_pos_max=0.35,
    max_atr_pct=None,          # e.g. 6.0
    require_prior_green=False,
    no_cascade=False,          # skip if prev red body > 4%
    min_rng_atr=None,          # signal range / ATR >= k
    btc_mode=None,             # None | not_up | red | down
    btc_not_up_pct=1.5,
    stop_mode="fixed",         # fixed | wick
    stop_pct=0.03,
    max_stop_pct=0.05,
    min_score=None,            # composite 0-5
)

VARIANTS = {
    "base":           {**BASE},
    "atr6":           {**BASE, "max_atr_pct": 6.0},
    "atr5":           {**BASE, "max_atr_pct": 5.0},
    "cpos20":         {**BASE, "close_pos_max": 0.20},
    "cpos25":         {**BASE, "close_pos_max": 0.25},
    "prior_green":    {**BASE, "require_prior_green": True},
    "no_cascade":     {**BASE, "no_cascade": True},
    "rng_atr12":      {**BASE, "min_rng_atr": 1.2},
    "rng_atr15":      {**BASE, "min_rng_atr": 1.5},
    "btc_red":        {**BASE, "btc_mode": "red"},
    "btc_not_up":     {**BASE, "btc_mode": "not_up"},
    "btc_down":       {**BASE, "btc_mode": "down"},
    "wick_cap5":      {**BASE, "stop_mode": "wick", "max_stop_pct": 0.05},
    "wick_cap4":      {**BASE, "stop_mode": "wick", "max_stop_pct": 0.04},
    "score3":         {**BASE, "min_score": 3},
    "score4":         {**BASE, "min_score": 4},
    # combos that looked least bad earlier
    "cpos20_atr6":    {**BASE, "close_pos_max": 0.20, "max_atr_pct": 6.0},
    "cpos20_btc_red": {**BASE, "close_pos_max": 0.20, "btc_mode": "red"},
    "cpos20_nocasc":  {**BASE, "close_pos_max": 0.20, "no_cascade": True},
    "pack_a":         {**BASE, "close_pos_max": 0.20, "max_atr_pct": 6.0, "no_cascade": True},
    "pack_b":         {**BASE, "close_pos_max": 0.20, "btc_mode": "red", "no_cascade": True},
    "pack_c":         {**BASE, "close_pos_max": 0.25, "max_atr_pct": 5.0, "min_rng_atr": 1.2, "btc_mode": "red"},
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


def btc_ok(btc, ts, cfg):
    mode = cfg.get("btc_mode")
    if not mode or not btc:
        return True
    idx = None
    for j in range(len(btc) - 1, -1, -1):
        if btc[j][0] <= ts:
            idx = j
            break
    if idx is None or idx < 8:
        return True
    o, c = btc[idx][1], btc[idx][4]
    n = 6
    prev = btc[idx - n][4]
    chg = (c - prev) / prev * 100 if prev else 0
    if mode == "down":
        return c < prev
    if mode == "not_up":
        return chg <= cfg.get("btc_not_up_pct", 1.5)
    if mode == "red":
        return c < o
    return True


def composite_score(body, close_pos, vol_ratio, atr_pct, prior_green, pump_ok):
    """0..5 rough quality."""
    s = 0
    if close_pos <= 0.20:
        s += 1
    if vol_ratio >= 4.0:
        s += 1
    if atr_pct is not None and atr_pct <= 5.0:
        s += 1
    if prior_green:
        s += 1
    if body >= 7.0:
        s += 1
    return s


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
    if vol_ma <= 0 or v < vol_ma * cfg["vol_ratio"]:
        return None
    vol_ratio = v / vol_ma
    if l > min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]):
        return None

    atr = calc_atr(ohlcv, i)
    atr_pct = (atr / c * 100) if atr and c else None
    if cfg.get("max_atr_pct") is not None and atr_pct is not None:
        if atr_pct > cfg["max_atr_pct"]:
            return None
    if cfg.get("min_rng_atr") is not None and atr and atr > 0:
        if rng / atr < cfg["min_rng_atr"]:
            return None

    prev = ohlcv[i - 1]
    prior_green = prev[4] > prev[1]
    if cfg.get("require_prior_green") and not prior_green:
        return None
    if cfg.get("no_cascade"):
        if prev[4] < prev[1]:
            pb = (prev[1] - prev[4]) / prev[1] * 100 if prev[1] else 0
            if pb > 4.0:
                return None

    if not btc_ok(btc, last[0], cfg):
        return None

    if cfg.get("min_score") is not None:
        sc = composite_score(body, close_pos, vol_ratio, atr_pct, prior_green, True)
        if sc < cfg["min_score"]:
            return None

    entry = c
    if cfg["stop_mode"] == "wick":
        stop = h * 1.001
        if (stop - entry) / entry > cfg["max_stop_pct"]:
            return None
    else:
        stop = entry * (1 + cfg["stop_pct"])
    risk = stop - entry
    if risk <= 0 or risk / entry < 0.005:
        return None

    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body": body, "bar_index": i,
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
        if symbol == BTC_SYM or len(ohlcv) < 120:
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

    bits = [f"cpos<={cfg['close_pos_max']}", f"stop={cfg['stop_mode']}"]
    if cfg.get("max_atr_pct"):
        bits.append(f"atr<{cfg['max_atr_pct']}%")
    if cfg.get("btc_mode"):
        bits.append(f"btc={cfg['btc_mode']}")
    if cfg.get("require_prior_green"):
        bits.append("prior_green")
    if cfg.get("no_cascade"):
        bits.append("no_cascade")
    if cfg.get("min_rng_atr"):
        bits.append(f"rng/atr>={cfg['min_rng_atr']}")
    if cfg.get("min_score"):
        bits.append(f"score>={cfg['min_score']}")

    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-TOOLS {name}",
        " | ".join(bits) + f" | 60d",
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP tools lab | SMC/VP/BTC/ATR | 60d IS/OOS")
    print("=" * 64)
    symbols = top_symbols()
    if BTC_SYM not in symbols:
        symbols = [BTC_SYM] + symbols

    print(f"BTC load...")
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
        f"DUMP tools 60d | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
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
    path = out / f"bt_DUMP_TOOLS_{stamp}.txt"
    latest = out / "latest_dump_tools.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
