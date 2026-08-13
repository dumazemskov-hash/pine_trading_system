#!/usr/bin/env python3
"""
RAID 2.0 — SMC-style short lab | 90d IS/OOS

Не апгрейд v8.32. Новая логика из smart-money-concepts / FractalTrader:

  1) Equal highs (buy-side liquidity cluster)
  2) Sweep: high пробивает уровень, close возвращается НИЖЕ уровня
  3) Красная свеча, close в нижней половине range
  4) Стоп ЗА high sweep-свечи (wick), skip если risk > max%
  5) Опционально: vol spike, min score (touches + fresh)

Цель — проверить, есть ли edge у «правильного» raid-reclaim на 15m Bybit.
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
COOLDOWN = 48
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0

SWING_N = 2
EQL_LOOKBACK = 24
MIN_TOUCH_DIST = 3
MAX_CLUSTER_SPAN = 16
TOL_ATR = 0.30
TOL_PCT = 0.0025
FRESH_BARS = 10

BASE = dict(
    min_touches=2,
    min_score=2,
    vol_ratio=1.5,
    min_body=2.0,
    max_body=12.0,
    close_pos_max=0.45,   # close in lower 45% of candle
    max_stop_pct=0.05,
    wick_buf=0.001,
    risk_pct=0.02,
    require_red=True,
)

VARIANTS = {
    "r2_base":       {**BASE},
    "r2_touch3":     {**BASE, "min_touches": 3, "min_score": 3},
    "r2_vol2":       {**BASE, "vol_ratio": 2.0},
    "r2_body3":      {**BASE, "min_body": 3.0, "close_pos_max": 0.35},
    "r2_strict":     {**BASE, "min_touches": 3, "vol_ratio": 2.0, "min_body": 3.0,
                      "close_pos_max": 0.35, "min_score": 3, "max_stop_pct": 0.04},
    "r2_wide_stop":  {**BASE, "max_stop_pct": 0.07},
    "r2_risk4":      {**BASE, "risk_pct": 0.04},
    "r2_tight":      {**BASE, "min_touches": 3, "vol_ratio": 2.5, "min_body": 4.0,
                      "close_pos_max": 0.30, "max_stop_pct": 0.035, "min_score": 4},
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


def is_swing_high(ohlcv, i, n=SWING_N):
    if i < n or i + n >= len(ohlcv):
        return False
    high = ohlcv[i][2]
    for k in range(1, n + 1):
        if ohlcv[i - k][2] >= high or ohlcv[i + k][2] >= high:
            return False
    return True


def find_equal_highs(ohlcv, i, atr):
    """Кластер equal highs = buy-side liquidity."""
    if i < EQL_LOOKBACK + SWING_N * 2 + 2:
        return None
    start = i - EQL_LOOKBACK
    swings = []
    for j in range(start + SWING_N, i - SWING_N + 1):
        if is_swing_high(ohlcv, j, SWING_N):
            swings.append({"price": ohlcv[j][2], "volume": ohlcv[j][5], "index": j})
    if len(swings) < 2:
        return None

    ref = ohlcv[i][4]
    tol = max((atr * TOL_ATR) if atr else 0, ref * TOL_PCT)
    if tol <= 0:
        return None

    best, best_sc = None, 0
    for a in range(len(swings)):
        cluster = [swings[a]]
        for b in range(len(swings)):
            if a == b:
                continue
            if (abs(swings[b]["price"] - swings[a]["price"]) <= tol
                    and MIN_TOUCH_DIST <= abs(swings[b]["index"] - swings[a]["index"]) <= MAX_CLUSTER_SPAN):
                if not any(c["index"] == swings[b]["index"] for c in cluster):
                    cluster.append(swings[b])
        if len(cluster) < 2:
            continue
        idxs = sorted(c["index"] for c in cluster)
        if any(abs(x - y) < MIN_TOUCH_DIST for x, y in zip(idxs, idxs[1:])):
            continue
        zone_high = max(c["price"] for c in cluster)
        touches = len(cluster)
        avg_vol = sum(c["volume"] for c in cluster) / touches
        is_fresh = any(c["index"] >= i - FRESH_BARS for c in cluster)
        if not is_fresh:
            continue
        score = (1 if touches == 2 else 2 if touches == 3 else 3)
        vol_ma = sum(x[5] for x in ohlcv[max(0, i - 20):i]) / max(1, min(20, i))
        if avg_vol >= vol_ma * 1.15:
            score += 1
        if is_fresh:
            score += 1
        if score > best_sc:
            best_sc = score
            best = {
                "zone_high": zone_high,
                "score": score,
                "touches": touches,
                "avg_vol": avg_vol,
            }
    return best


def check_at(ohlcv, i, cfg):
    if i < 70 or i >= len(ohlcv) - 1:
        return None

    o, h, l, c, v = ohlcv[i][1], ohlcv[i][2], ohlcv[i][3], ohlcv[i][4], ohlcv[i][5]
    if cfg["require_red"] and c >= o:
        return None

    body = abs(c - o) / o * 100 if o else 0
    if body < cfg["min_body"] or body > cfg["max_body"]:
        return None

    rng = h - l
    if rng <= 0:
        return None
    close_pos = (c - l) / rng
    if close_pos > cfg["close_pos_max"]:
        return None

    atr = calc_atr(ohlcv, i)
    zone = find_equal_highs(ohlcv, i, atr)
    if zone is None:
        return None
    if zone["touches"] < cfg["min_touches"]:
        return None
    if zone["score"] < cfg["min_score"]:
        return None

    zh = zone["zone_high"]
    # SWEEP + CLOSE BACK (ключ SMC)
    if h <= zh:
        return None          # не пробили
    if c >= zh:
        return None          # не вернулись ниже — нет reclaim

    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20 if i >= 20 else v
    if vol_ma > 0 and v < vol_ma * cfg["vol_ratio"]:
        return None

    entry = c
    stop = h * (1 + cfg["wick_buf"])
    risk = stop - entry
    if risk <= 0:
        return None
    risk_pct = risk / entry
    if risk_pct > cfg["max_stop_pct"]:
        return None          # слишком широкий — skip
    if risk_pct < 0.005:
        return None

    return {
        "ts": ohlcv[i][0],
        "entry": entry,
        "stop": stop,
        "tp1": entry - risk * TP1_RR,
        "tp2": entry - risk * TP2_RR,
        "risk": risk,
        "body": body,
        "bar_index": i,
        "score": zone["score"],
        "touches": zone["touches"],
        "stop_pct_px": risk_pct * 100,
        "zone_high": zh,
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
        f"SUMMARY | RAID2 {name}",
        (
            f"SMC equal-highs sweep+closeback | touches>={cfg['min_touches']} "
            f"vol>={cfg['vol_ratio']}x body>={cfg['min_body']}% max_stop={cfg['max_stop_pct']*100:.0f}% "
            f"risk={cfg['risk_pct']*100:.0f}%"
        ),
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, cfg["risk_pct"], label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("RAID 2.0 | SMC equal-highs → sweep → close back → short")
    print("стоп за wick | 90d IS/OOS | top-150")
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
        f"RAID2 SMC | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
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
    path = out / f"bt_RAID2_{stamp}.txt"
    latest = out / "latest_raid2.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
