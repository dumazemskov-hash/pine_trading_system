#!/usr/bin/env python3
"""
RAID lab v1 | 90d top-150 | IS/OOS
База: equal lows zone + dump candle (как v8.32)
Эксперименты:
  - stop: cap% vs wick (raid high + buffer)
  - close-back: close must reclaim below zone low (SMC sweep)
  - risk 2% vs 4%
"""

import ccxt, time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 90
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 100
SLEEP = 0.10
IS_FRAC = 2.0 / 3.0
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0

EQL_LOOKBACK = 20
SWING_N = 2
MIN_TOUCH_DISTANCE = 3
MAX_CLUSTER_SPAN = 12
EQL_TOLERANCE_ATR = 0.30
EQL_TOLERANCE_PCT = 0.0025
MIN_ZONE_SCORE = 2
BREAK_ATR_MULT = 0.25
SWEEP_LOOKBACK = 10
FRESH_ZONE_BARS = 8

BASE = dict(
    min_body=4.0, max_body=9.0, max_prev_body=3.0,
    volume_ratio=1.7, prior_vol_mult=1.35, condition_d=0.60,
    impulse_strength=1.25,
    close_in_range_max=0.35, min_body_to_range=0.50,
    stop_mode="cap",          # cap | wick | wick_cap
    stop_pct=0.04,
    wick_buf=0.001,           # 0.1% above raid high
    max_stop_pct=0.05,
    require_close_back=False, # SMC: close back below zone_low
    risk_pct=0.04,
)

VARIANTS = {
    "base_cap4":     {**BASE},
    "base_cap3":     {**BASE, "stop_pct": 0.03, "max_stop_pct": 0.03},
    "wick":          {**BASE, "stop_mode": "wick", "max_stop_pct": 0.06},
    "wick_cap4":     {**BASE, "stop_mode": "wick_cap", "stop_pct": 0.04, "max_stop_pct": 0.04},
    "wick_cap5":     {**BASE, "stop_mode": "wick_cap", "stop_pct": 0.05, "max_stop_pct": 0.05},
    "closeback_cap4":{**BASE, "require_close_back": True},
    "closeback_wick":{**BASE, "require_close_back": True, "stop_mode": "wick_cap",
                      "stop_pct": 0.04, "max_stop_pct": 0.04},
    "body55_vol2":   {**BASE, "min_body": 5.5, "volume_ratio": 2.0},
    "risk2_cap4":    {**BASE, "risk_pct": 0.02},
    "risk2_wick_cap":{**BASE, "risk_pct": 0.02, "stop_mode": "wick_cap",
                      "stop_pct": 0.04, "max_stop_pct": 0.04},
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


def is_swing_low(ohlcv, i, n=SWING_N):
    if i < n or i + n >= len(ohlcv):
        return False
    low = ohlcv[i][3]
    for k in range(1, n + 1):
        if ohlcv[i - k][3] <= low or ohlcv[i + k][3] <= low:
            return False
    return True


def find_equal_lows(ohlcv, i, atr):
    if i < EQL_LOOKBACK + SWING_N * 2 + 2:
        return None
    end = i
    start = end - EQL_LOOKBACK
    swings = []
    for j in range(start + SWING_N, end - SWING_N + 1):
        if is_swing_low(ohlcv, j, SWING_N):
            swings.append({"price": ohlcv[j][3], "volume": ohlcv[j][5], "index": j})
    if len(swings) < 2:
        return None
    ref = ohlcv[end][4]
    tol = max((atr * EQL_TOLERANCE_ATR) if atr else 0, ref * EQL_TOLERANCE_PCT)
    if tol <= 0:
        return None
    best, best_sc = None, 0
    for a in range(len(swings)):
        cluster = [swings[a]]
        for b in range(len(swings)):
            if a == b:
                continue
            if (abs(swings[b]["price"] - swings[a]["price"]) <= tol
                    and abs(swings[b]["index"] - swings[a]["index"]) <= MAX_CLUSTER_SPAN
                    and abs(swings[b]["index"] - swings[a]["index"]) >= MIN_TOUCH_DISTANCE):
                if not any(c["index"] == swings[b]["index"] for c in cluster):
                    cluster.append(swings[b])
        if len(cluster) < 2:
            continue
        idxs = sorted(c["index"] for c in cluster)
        if any(abs(x - y) < MIN_TOUCH_DISTANCE for x, y in zip(idxs, idxs[1:])):
            continue
        zone_low = min(c["price"] for c in cluster)
        touches = len(cluster)
        avg_vol = sum(c["volume"] for c in cluster) / touches
        vol_slice = ohlcv[max(0, end - 20):end]
        vol_ma = sum(c[5] for c in vol_slice) / len(vol_slice) if vol_slice else avg_vol
        is_fresh = any(c["index"] >= end - FRESH_ZONE_BARS for c in cluster)
        if not is_fresh:
            continue
        score = (1 if touches == 2 else 2 if touches == 3 else 3)
        if avg_vol >= vol_ma * 1.2:
            score += 1
        if is_fresh:
            score += 1
        if score > best_sc:
            best_sc = score
            best = {"zone_low": zone_low, "score": score, "touches": touches, "avg_vol": avg_vol}
    if best is None or best["score"] < MIN_ZONE_SCORE:
        return None
    return best


def make_stop(entry, raid_high, atr, cfg):
    mode = cfg["stop_mode"]
    if mode == "cap":
        return entry * (1 + cfg["stop_pct"])
    if mode == "wick":
        stop = raid_high * (1 + cfg["wick_buf"])
        if atr:
            stop = max(stop, raid_high + atr * 0.15)
        if (stop - entry) / entry > cfg["max_stop_pct"]:
            return None
        return stop
    if mode == "wick_cap":
        wick = raid_high * (1 + cfg["wick_buf"])
        cap = entry * (1 + cfg["max_stop_pct"])
        stop = min(wick, cap)
        if stop <= entry:
            return None
        return stop
    return None


def check_at(ohlcv, i, cfg):
    if i < 70 or i >= len(ohlcv) - 1:
        return None
    last, prev = ohlcv[i], ohlcv[i - 1]
    o, h, l, c, v = last[1], last[2], last[3], last[4], last[5]

    body = abs(c - o) / o * 100 if o else 0
    if body < cfg["min_body"] or body > cfg["max_body"]:
        return None
    prev_body = (prev[4] - prev[1]) / prev[1] * 100 if prev[1] else 0
    if prev_body > cfg["max_prev_body"]:
        return None

    impulse = (h - l) / l * 100 if l > 0 else 0
    if impulse < cfg["min_body"] * cfg["impulse_strength"]:
        return None

    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
    if v < vol_ma * cfg["volume_ratio"]:
        return None
    vol_recent = vol_ma
    older = ohlcv[max(0, i - 60):i - 20]
    if older:
        vol_older = sum(x[5] for x in older) / len(older)
        if vol_older > 0 and vol_recent < vol_older * cfg["prior_vol_mult"]:
            return None

    atr = calc_atr(ohlcv, i)
    zone = find_equal_lows(ohlcv, i, atr)
    if zone is None:
        return None

    break_level = zone["zone_low"] - (atr * BREAK_ATR_MULT if atr else zone["zone_low"] * 0.003)
    if l >= break_level:
        return None

    recent = [x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]]
    if l > min(recent):
        return None

    if not (c < o and c < prev[4]):
        return None

    # SMC close-back: close returned below zone low after sweeping it
    if cfg["require_close_back"] and c >= zone["zone_low"]:
        return None

    rng = h - l
    if rng <= 0:
        return None
    if (c - l) / rng > cfg["close_in_range_max"]:
        return None
    if abs(c - o) / rng < cfg["min_body_to_range"]:
        return None

    if v < max(prev[5] * cfg["condition_d"], zone.get("avg_vol", 0) * 1.15):
        return None

    entry = c
    stop = make_stop(entry, h, atr, cfg)
    if stop is None or stop <= entry:
        return None
    risk = stop - entry
    if risk / entry < 0.005:
        return None

    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body": body, "bar_index": i,
        "score": zone["score"], "touches": zone["touches"],
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
        for i in range(70, n - 8):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            sig = check_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": sig["ts"], "result": res, "bars": bars, "r": r,
                "stop_pct": sig["stop_pct_px"],
                "split": "IS" if i < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    lines = [
        "=" * 64,
        f"SUMMARY | RAID {name}",
        (
            f"stop={cfg['stop_mode']} pct={cfg.get('stop_pct',0)*100:.0f}% "
            f"closeback={cfg['require_close_back']} risk={cfg['risk_pct']*100:.0f}% "
            f"body={cfg['min_body']}-{cfg['max_body']}"
        ),
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, cfg["risk_pct"], label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("RAID lab v1 | equal-lows + dump | 90d IS/OOS")
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
        f"RAID lab v1 | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
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
    path = out / f"bt_RAID_LAB_{stamp}.txt"
    latest = out / "latest_raid.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
