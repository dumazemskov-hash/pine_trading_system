#!/usr/bin/env python3
"""
DUMP split-entry lab — 40/60 две ноги, не фильтр v0.2b
Live dump_scanner.py НЕ трогаем.

Leg1: 40% риска, entry=close[t], stop=как live (cap 3%)
Leg2: 60% риска, ТОЛЬКО если close[t+1] < close[t]
      entry2=close[t+1], stop2=min(high1+0.35ATR1, entry2*1.03)
Суммарный риск сигнала ≤ 2%. Ноги независимы (свои стоп/TP/BE).

Варианты:
  base       — live 100% на close t
  split4060  — как выше
  split5050  — 50/50, тот же триггер leg2
  leg1only40 — только первая нога (бандаж без добора)

Запуск: python scanner/backtest_dump_split.py
Отчёт:  backtests/bt_DUMP_SPLIT_*.txt + latest_dump_split.txt
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
BE_AFTER_TP1 = True
COOLDOWN = 32
STOP_COOLDOWN = 96
TP1_RR, TP2_RR = 1.6, 3.0
IS_FRAC = 2.0 / 3.0
RISK_PCT = 0.02
STOP_ATR_MULT = 0.35
MAX_RISK_PCT = 0.03
MIN_STOP_PCT = 0.004

VARIANTS = {
    "base": dict(mode="base", w1=1.0, w2=0.0, need_leg2=False),
    "split4060": dict(mode="split", w1=0.40, w2=0.60, need_leg2=True),
    "split5050": dict(mode="split", w1=0.50, w2=0.50, need_leg2=True),
    "leg1only40": dict(mode="split", w1=0.40, w2=0.0, need_leg2=False),
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
    return {
        "i": i, "ts": ohlcv[i][0],
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "body": body, "atr": atr, "orig_stop": stop,
    }


def make_leg(entry, stop, start_i, weight):
    risk = stop - entry
    if risk <= 0 or risk / entry < MIN_STOP_PCT or risk / entry > 0.20:
        return None
    return {
        "entry": entry,
        "stop": stop,
        "tp1": entry - risk * TP1_RR,
        "tp2": entry - risk * TP2_RR,
        "bar_index": start_i,
        "weight": weight,
        "stop_pct_px": risk / entry * 100,
    }


def outcome(ohlcv, trade):
    i = trade["bar_index"]
    entry, stop, tp1, tp2 = trade["entry"], trade["stop"], trade["tp1"], trade["tp2"]
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


def resolve_signal(ohlcv, sig, cfg):
    i = sig["i"]
    legs = []
    why2 = "NOLEG2"
    leg1 = make_leg(sig["c"], sig["orig_stop"], i, cfg["w1"])
    if not leg1:
        return None, "WIDE", []
    legs.append(("L1", leg1))

    if cfg.get("need_leg2") and cfg.get("w2", 0) > 0:
        if i + 1 >= len(ohlcv) - 1:
            why2 = "NOBAR"
        else:
            n = ohlcv[i + 1]
            if n[4] >= sig["c"]:
                why2 = "NOCONT"
            else:
                atr1 = calc_atr(ohlcv, i + 1) or n[2] * 0.01
                stop2 = min(n[2] + STOP_ATR_MULT * atr1, n[4] * (1 + MAX_RISK_PCT))
                leg2 = make_leg(n[4], stop2, i + 1, cfg["w2"])
                if not leg2:
                    why2 = "WIDE2"
                else:
                    legs.append(("L2", leg2))
                    why2 = "TAKE2"
    return legs, why2, legs


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


def summarize(raw, label):
    stats = defaultdict(float)
    onebar = 0
    stops_pct = []
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s.get("l1_stop1"):
            onebar += 1
        if s.get("stop_pct_px") is not None:
            stops_pct.append(s["stop_pct_px"])
    total = int(stats["TOTAL"])
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сделок нет")
        return lines
    wins = sum(1 for s in raw if s["r"] > 0)
    losses = sum(1 for s in raw if s["r"] < 0)
    capital = peak = START_CAPITAL
    max_dd = 0.0
    for s in raw:
        capital += capital * RISK_PCT * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
    onebar_pct = onebar / losses * 100 if losses else 0
    avg_stop = sum(stops_pct) / len(stops_pct) if stops_pct else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-barL1={onebar}({onebar_pct:.0f}%) avg_stopL1={avg_stop:.2f}%",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
        f"leg2_fill={int(sum(1 for s in raw if s.get('leg2')))} "
        f"STOP={int(stats['STOP'])} MIX={int(stats['MIX'])} "
        f"TP={int(stats['TP'])} BE={int(stats['BE'])} OPEN={int(stats['OPEN'])}",
    ]
    return lines


def tag_result(names):
    if not names:
        return "OPEN"
    if all(n == "STOP" for n in names):
        return "STOP"
    if all(n in ("TP2", "TP1+TP2", "TP1") for n in names):
        return "TP"
    if all(n == "TP1->BE" for n in names):
        return "BE"
    if all(n == "OPEN" for n in names):
        return "OPEN"
    if any(n in ("TP2", "TP1+TP2", "TP1", "TP1->BE") for n in names) and any(n == "STOP" for n in names):
        return "MIX"
    if any(n == "TP1->BE" for n in names):
        return "BE"
    if any(n in ("TP2", "TP1+TP2", "TP1") for n in names):
        return "TP"
    return "MIX"


def run_variant(name, cfg, by_sym):
    raw_all = []
    funnel = defaultdict(int)
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 120:
            continue
        n = len(ohlcv)
        is_end = int(n * IS_FRAC)
        last_i, stop_ban = -999, -1
        for i in range(70, n - 12):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            sig0 = signal_bar(ohlcv, i)
            if not sig0:
                continue
            funnel["dump"] += 1
            pack, why2, legs = resolve_signal(ohlcv, sig0, cfg)
            last_i = i
            if not pack:
                funnel[why2] += 1
                continue
            funnel["TAKE"] += 1
            funnel[why2] += 1
            weighted_r = 0.0
            legs_out = []
            l1_bars = None
            l1_stop1 = False
            any_stop = False
            stop_px = None
            has_l2 = False
            last_bar = i
            for tag, leg in pack:
                res, bars, r = outcome(ohlcv, leg)
                weighted_r += leg["weight"] * r
                legs_out.append((res, bars, r, tag))
                last_bar = max(last_bar, leg["bar_index"] + bars)
                if tag == "L1":
                    l1_bars = bars
                    stop_px = leg["stop_pct_px"]
                    if res in ("STOP", "TP1->STOP"):
                        any_stop = True
                        if bars == 1:
                            l1_stop1 = True
                if tag == "L2":
                    has_l2 = True
                    if res in ("STOP", "TP1->STOP"):
                        any_stop = True
            if any_stop:
                stop_ban = last_bar + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": ohlcv[i][0],
                "result": tag_result([o[0] for o in legs_out]),
                "r": weighted_r,
                "l1_bars": l1_bars,
                "l1_stop1": l1_stop1,
                "stop_pct_px": stop_px,
                "leg2": has_l2,
                "split": "IS" if i < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]
    skip_bits = " ".join(
        f"{k}={funnel[k]}"
        for k in ("TAKE2", "NOCONT", "NOBAR", "WIDE", "WIDE2", "NOLEG2")
        if funnel[k]
    )
    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-SPLIT {name}",
        f"cfg={cfg} | 60d | live untouched",
        f"funnel dump={funnel['dump']} TAKE={funnel['TAKE']} {skip_bits}",
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP split-entry | base / 40-60 / 50-50 / leg1only40")
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

    all_lines = [
        f"DUMP split | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
        "base        = live v0.2b 100% close t",
        "split4060   = L1 40% close t + L2 60% if close1 < close0",
        "split5050   = 50/50 тот же триггер L2",
        "leg1only40  = только L1 40% (бандаж без добора)",
        "AvgR на сигнал:  w1*r1 + w2*r2  (нет L2 → w2=0)",
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
    path = out / f"bt_DUMP_SPLIT_{stamp}.txt"
    latest = out / "latest_dump_split.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
