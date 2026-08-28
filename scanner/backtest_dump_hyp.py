#!/usr/bin/env python3
"""
DUMP hyp lab — PRELOW / DEEP-SWEEP / VOL-CAP / RETEST / BREAK
Live dump_scanner.py НЕ трогаем.

1-3: тот же вход v0.2b, только фильтр на close сигнала.
4: limit short на 35% retrace close→high, только bar+1, stop=high+0.15ATR
5: sell-stop под low-0.10ATR, только bar+1, stop=max(high, entry*1.03)

Запуск: python scanner/backtest_dump_hyp.py
Отчёт:  backtests/bt_DUMP_HYP_*.txt + latest_dump_hyp.txt
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
    "base": dict(mode="base"),
    "prelow": dict(mode="base", prelow_break=0.0015),
    "deep": dict(mode="base", prelow_break=0.0010, sweep_depth=0.0040),
    "deep_loose": dict(mode="base", prelow_break=0.0000, sweep_depth=0.0025),
    "volcap6": dict(mode="base", vmax=6.0),
    "volcap8": dict(mode="base", vmax=8.0),
    "retest": dict(mode="retest", retrace=0.35, stop_atr=0.15, max_stop_pct=0.12),
    "break": dict(mode="break", atr_under=0.10, max_stop_pct=0.15),
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
    pre_low = min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK:i])
    atr = calc_atr(ohlcv, i)
    struct = h + (atr * STOP_ATR_MULT if atr else h * 0.005)
    stop = min(struct, c * (1 + MAX_RISK_PCT))
    return {
        "i": i, "ts": ohlcv[i][0],
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "body": body, "atr": atr, "orig_stop": stop,
        "pre_low": pre_low, "vol_ma": vol_ma,
        "volx": v / vol_ma if vol_ma else 0,
        "sweep_depth": (pre_low - l) / pre_low if pre_low else 0,
    }


def pass_filters(sig, cfg):
    if cfg.get("vmax") is not None and sig["volx"] > cfg["vmax"]:
        return False, "VOLCAP"
    if cfg.get("sweep_depth") is not None and sig["sweep_depth"] < cfg["sweep_depth"]:
        return False, "SHALLOW"
    if cfg.get("prelow_break") is not None:
        if sig["c"] >= sig["pre_low"] * (1 - cfg["prelow_break"]):
            return False, "NOACCEPT"
    return True, "OK"


def build_trade(ohlcv, sig, cfg):
    ok, why = pass_filters(sig, cfg)
    if not ok:
        return None, why

    mode = cfg.get("mode", "base")
    i = sig["i"]

    if mode == "base":
        entry, stop, start = sig["c"], sig["orig_stop"], i
    elif mode == "retest":
        if i + 1 >= len(ohlcv) - 1:
            return None, "NOBAR"
        limit = sig["c"] + cfg.get("retrace", 0.35) * (sig["h"] - sig["c"])
        n = ohlcv[i + 1]
        if n[2] < limit:
            return None, "NOFILL"
        entry = limit
        atr = sig["atr"] or sig["h"] * 0.01
        stop = sig["h"] + cfg.get("stop_atr", 0.15) * atr
        start = i + 1
    elif mode == "break":
        if i + 1 >= len(ohlcv) - 1:
            return None, "NOBAR"
        atr = sig["atr"] or sig["l"] * 0.01
        trigger = sig["l"] - cfg.get("atr_under", 0.10) * atr
        n = ohlcv[i + 1]
        if n[3] > trigger:
            return None, "NOFILL"
        entry = trigger
        stop = max(sig["h"], entry * 1.03)
        start = i + 1
    else:
        return None, "SKIP"

    risk = stop - entry
    max_sp = cfg.get("max_stop_pct", 0.20)
    if risk <= 0 or risk / entry < MIN_STOP_PCT:
        return None, "WIDE"
    if risk / entry > max_sp:
        return None, "WIDE"
    return {
        "ts": sig["ts"],
        "entry": entry,
        "stop": stop,
        "tp1": entry - risk * TP1_RR,
        "tp2": entry - risk * TP2_RR,
        "risk": risk,
        "bar_index": start,
        "stop_pct_px": risk / entry * 100,
        "body": sig["body"],
        "volx": sig["volx"],
        "sweep_depth": sig["sweep_depth"] * 100,
        "accepted": sig["c"] < sig["pre_low"],
    }, "TAKE"


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
        if s["result"] in ("STOP", "TP1->STOP") and s["bars"] == 1:
            onebar += 1
        if s.get("stop_pct_px") is not None:
            stops_pct.append(s["stop_pct_px"])
    total = int(stats["TOTAL"])
    lines = [f"--- {label} ---"]
    if total == 0:
        lines.append("Сделок нет")
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
    avg_stop = sum(stops_pct) / len(stops_pct) if stops_pct else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%) avg_stop={avg_stop:.2f}%",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
        f"STOP={int(stats['STOP'])} TP1+TP2={int(stats['TP1+TP2'])} TP2={int(stats['TP2'])} "
        f"TP1={int(stats['TP1'])} BE={int(stats['TP1->BE'])} OPEN={int(stats['OPEN'])}",
    ]
    return lines


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
            trade, why = build_trade(ohlcv, sig0, cfg)
            last_i = i
            if not trade:
                funnel[why] += 1
                continue
            funnel["TAKE"] += 1
            res, bars, r = outcome(ohlcv, trade)
            if res in ("STOP", "TP1->STOP"):
                stop_ban = trade["bar_index"] + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": ohlcv[trade["bar_index"]][0],
                "result": res, "bars": bars, "r": r,
                "stop_pct_px": trade.get("stop_pct_px"),
                "volx": trade.get("volx"),
                "split": "IS" if trade["bar_index"] < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]
    skip_bits = " ".join(f"{k}={funnel[k]}" for k in ("NOACCEPT", "SHALLOW", "VOLCAP", "NOFILL", "WIDE") if funnel[k])
    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-HYP {name}",
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
    print("DUMP hyp lab | prelow / deep-sweep / volcap / retest / break")
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
        f"DUMP hyp | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
        "base       = live v0.2b",
        "prelow     = close < pre_low * 0.9985",
        "deep       = sweep>=0.40% AND close < pre_low * 0.999",
        "deep_loose = sweep>=0.25% AND close <= pre_low",
        "volcap6/8  = 3x <= volx <= 6/8",
        "retest     = limit at 35% retrace close→high, bar+1 only, stop=high+0.15ATR",
        "break      = sell-stop low-0.10ATR, bar+1 only, stop=max(high,entry*1.03)",
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
    path = out / f"bt_DUMP_HYP_{stamp}.txt"
    latest = out / "latest_dump_hyp.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
