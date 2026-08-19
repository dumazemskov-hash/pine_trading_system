#!/usr/bin/env python3
"""
DUMP geometry lab — 60d IS/OOS
Live scanner НЕ трогаем.

Гипотезы против структурных 1-bar stops:
  base       — как live (entry=close сигнала, stop=3%)
  stop_hi    — entry=close, stop=high*1.001 (стоп ЗА high)
  skip5      — skip если (high-entry)/entry > 5%
  cont1      — вход на close СЛЕДУЮЩЕГО бара, только если close1 < low0; stop 3%
  cont1_hi   — continuation + stop = high сигнала

Сравниваем 1-bar%, OOS AvgR, N.
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

VARIANTS = {
    "base":     dict(mode="base"),
    "stop_hi":  dict(mode="stop_hi", max_stop_pct=0.12),
    "stop_hi8": dict(mode="stop_hi", max_stop_pct=0.08),
    "skip5":    dict(mode="skip_wide", max_he_pct=5.0),
    "skip6":    dict(mode="skip_wide", max_he_pct=6.0),
    "cont1":    dict(mode="cont", stop="fixed3"),
    "cont1_hi": dict(mode="cont", stop="signal_high", max_stop_pct=0.12),
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
    """Тот же вход, что live dump v0.2b. Возвращает dict или None."""
    if i < 70 or i >= len(ohlcv) - 2:
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
    return {
        "i": i, "ts": ohlcv[i][0],
        "o": o, "h": h, "l": l, "c": c,
        "body": body, "atr": atr,
    }


def build_trade(ohlcv, sig, cfg):
    """Строит entry/stop/tp или None."""
    i = sig["i"]
    mode = cfg["mode"]

    if mode == "base":
        entry = sig["c"]
        atr = sig["atr"]
        struct = sig["h"] + (atr * STOP_ATR_MULT if atr else sig["h"] * 0.005)
        stop = min(struct, entry * (1 + MAX_RISK_PCT))
        start = i  # outcome с i+1

    elif mode == "stop_hi":
        entry = sig["c"]
        stop = sig["h"] * 1.001
        if (stop - entry) / entry > cfg.get("max_stop_pct", 0.12):
            return None
        start = i

    elif mode == "skip_wide":
        entry = sig["c"]
        he = (sig["h"] - entry) / entry * 100
        if he > cfg.get("max_he_pct", 5.0):
            return None
        atr = sig["atr"]
        struct = sig["h"] + (atr * STOP_ATR_MULT if atr else sig["h"] * 0.005)
        stop = min(struct, entry * (1 + MAX_RISK_PCT))
        start = i

    elif mode == "cont":
        # нужен закрытый следующий бар
        if i + 1 >= len(ohlcv) - 1:
            return None
        n = ohlcv[i + 1]
        nc = n[4]
        if nc >= sig["l"]:
            return None  # нет break below signal low
        entry = nc
        if cfg.get("stop") == "signal_high":
            stop = sig["h"] * 1.001
            if (stop - entry) / entry > cfg.get("max_stop_pct", 0.12):
                return None
        else:
            stop = entry * (1 + MAX_RISK_PCT)
        start = i + 1  # outcome с i+2

    else:
        return None

    risk = stop - entry
    if risk <= 0 or risk / entry < 0.004:
        return None
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
    stops_pct = []
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s["result"] in ("STOP", "TP1->STOP") and s["bars"] == 1:
            onebar += 1
        if s.get("stop_pct_px") is not None:
            stops_pct.append(s["stop_pct_px"])
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
    avg_stop = sum(stops_pct) / len(stops_pct) if stops_pct else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%) avg_stop={avg_stop:.2f}%",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
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
        for i in range(70, n - 10):
            if i - last_i < COOLDOWN or i < stop_ban:
                continue
            sig0 = signal_bar(ohlcv, i)
            if not sig0:
                continue
            trade = build_trade(ohlcv, sig0, cfg)
            if not trade:
                continue
            res, bars, r = outcome(ohlcv, trade)
            last_i = trade["bar_index"]
            if res in ("STOP", "TP1->STOP"):
                stop_ban = last_i + STOP_COOLDOWN
            raw_all.append({
                "ts_ms": trade["ts"], "result": res, "bars": bars, "r": r,
                "stop_pct_px": trade.get("stop_pct_px"),
                "split": "IS" if trade["bar_index"] < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-GEOM {name}",
        f"mode={cfg['mode']} | 60d | live untouched",
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP geometry lab | continuation / stop-behind-high")
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
        f"DUMP geometry | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
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
    path = out / f"bt_DUMP_GEOM_{stamp}.txt"
    latest = out / "latest_dump_geom.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
