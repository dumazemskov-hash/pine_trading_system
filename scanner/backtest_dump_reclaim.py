#!/usr/bin/env python3
"""
DUMP failed-reclaim lab — 60d IS/OOS
Live dump_scanner.py НЕ трогаем.

Идея: dump-свеча только рисует уровень.
Вход не на close сигнала, а когда отскок:
  - задевает уровень (open / mid / close dump)
  - НЕ переписывает high dump
  - закрывается обратно ниже уровня
Стоп за high свечи отскока (или за high dump).

Если до триггера high >= dump.high → идея мертва (SWEPT).
Если окно истекло без паттерна → TIMEOUT.
Если цена ушла в старый TP2 без отскока → MISS (раннер без нас).

Запуск: python scanner/backtest_dump_reclaim.py
Отчёт:  backtests/bt_DUMP_RECLAIM_*.txt + latest_dump_reclaim.txt
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
SKIP_ASIA = False
ASIA_HOUR_END = 6
MIN_STOP_PCT = 0.004

VARIANTS = {
    "base": dict(mode="base"),
    "rec_open": dict(
        mode="reclaim", level="open", stop="reject_high",
        window=8, max_stop_pct=0.08, bearish=True,
    ),
    "rec_open_w16": dict(
        mode="reclaim", level="open", stop="reject_high",
        window=16, max_stop_pct=0.08, bearish=True,
    ),
    "rec_mid": dict(
        mode="reclaim", level="mid", stop="reject_high",
        window=8, max_stop_pct=0.08, bearish=True,
    ),
    "rec_close": dict(
        mode="reclaim", level="close", stop="reject_high",
        window=8, max_stop_pct=0.08, bearish=True,
    ),
    "rec_open_dh": dict(
        mode="reclaim", level="open", stop="dump_high",
        window=8, max_stop_pct=0.12, bearish=True,
    ),
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
    if SKIP_ASIA:
        hour = (ohlcv[i][0] // 1000) % 86400 // 3600
        if hour < ASIA_HOUR_END:
            return None
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
        "o": o, "h": h, "l": l, "c": c,
        "body": body, "atr": atr, "orig_stop": stop,
    }


def level_price(sig, name):
    if name == "open":
        return sig["o"]
    if name == "mid":
        return (sig["h"] + sig["l"]) / 2
    return sig["c"]


def find_reclaim(ohlcv, sig, cfg):
    i = sig["i"]
    dump_h = sig["h"]
    lvl = level_price(sig, cfg["level"])
    window = int(cfg.get("window", 8))
    end = min(i + window, len(ohlcv) - 3)
    old_tp2 = sig["c"] - (sig["orig_stop"] - sig["c"]) * TP2_RR

    for j in range(i + 1, end + 1):
        o, h, l, c = ohlcv[j][1], ohlcv[j][2], ohlcv[j][3], ohlcv[j][4]
        if h >= dump_h:
            return None, "SWEPT"
        if l <= old_tp2 and h < lvl:
            return None, "MISS"
        if h < lvl:
            continue
        if cfg.get("bearish", True) and c >= o:
            continue
        if c >= lvl:
            continue
        if cfg["stop"] == "dump_high":
            stop = dump_h * 1.001
        else:
            stop = h * 1.001
        entry = c
        risk = stop - entry
        max_sp = cfg.get("max_stop_pct", 0.08)
        if risk <= 0 or risk / entry < MIN_STOP_PCT or risk / entry > max_sp:
            return None, "WIDE"
        return {
            "ts": sig["ts"],
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk * TP1_RR,
            "tp2": entry - risk * TP2_RR,
            "risk": risk,
            "bar_index": j,
            "stop_pct_px": risk / entry * 100,
            "body": sig["body"],
            "wait": j - i,
        }, "TAKE"
    return None, "TIMEOUT"


def build_trade(ohlcv, sig, cfg):
    if cfg["mode"] == "base":
        entry = sig["c"]
        stop = sig["orig_stop"]
        risk = stop - entry
        if risk <= 0:
            return None, "SKIP"
        return {
            "ts": sig["ts"],
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk * TP1_RR,
            "tp2": entry - risk * TP2_RR,
            "risk": risk,
            "bar_index": sig["i"],
            "stop_pct_px": risk / entry * 100,
            "body": sig["body"],
            "wait": 0,
        }, "TAKE"
    return find_reclaim(ohlcv, sig, cfg)


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
    waits = []
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]
        if s["result"] in ("STOP", "TP1->STOP") and s["bars"] == 1:
            onebar += 1
        if s.get("stop_pct_px") is not None:
            stops_pct.append(s["stop_pct_px"])
        if s.get("wait") is not None:
            waits.append(s["wait"])
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
    avg_wait = sum(waits) / len(waits) if waits else 0
    lines += [
        f"N={total} WR={wins/total*100:.1f}% 1-bar={onebar}({onebar_pct:.0f}%) "
        f"avg_stop={avg_stop:.2f}% avg_wait={avg_wait:.1f}b",
        f"Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%) "
        f"AvgR {stats['sum_R']/total:+.2f} MaxDD {max_dd:.1f}%",
        f"STOP={int(stats['STOP'])} TP1+TP2={int(stats['TP1+TP2'])} TP2={int(stats['TP2'])} "
        f"TP1={int(stats['TP1'])} BE={int(stats['TP1->BE'])} OPEN={int(stats['OPEN'])}",
    ]
    return lines


def run_variant(name, cfg, by_sym):
    raw_all = []
    funnel = defaultdict(int)
    waits = []
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
            waits.append(trade.get("wait", 0))
            raw_all.append({
                "ts_ms": ohlcv[trade["bar_index"]][0],
                "result": res, "bars": bars, "r": r,
                "stop_pct_px": trade.get("stop_pct_px"),
                "wait": trade.get("wait", 0),
                "split": "IS" if trade["bar_index"] < is_end else "OOS",
            })
    raw_all.sort(key=lambda x: x["ts_ms"])
    raw_is = [x for x in raw_all if x["split"] == "IS"]
    raw_oos = [x for x in raw_all if x["split"] == "OOS"]

    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-RECLAIM {name}",
        f"cfg={cfg} | 60d | live untouched",
        f"funnel dump={funnel['dump']} TAKE={funnel['TAKE']} "
        f"SWEPT={funnel['SWEPT']} TIMEOUT={funnel['TIMEOUT']} "
        f"MISS={funnel['MISS']} WIDE={funnel['WIDE']}",
        "=" * 64,
    ]
    for label, chunk in [("FULL", raw_all), ("IS (first 2/3)", raw_is), ("OOS (last 1/3)", raw_oos)]:
        lines.extend(summarize(chunk, label))
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP failed-reclaim lab | dump marks level, entry on reject")
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
        f"DUMP failed-reclaim | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
        "base       = live v0.2b (контроль)",
        "rec_open   = отскок к open dump, close < open, stop=high отскока, окно 8",
        "rec_open_w16 = то же, окно 16",
        "rec_mid    = отскок к mid dump, close < mid, stop=high отскока",
        "rec_close  = отскок выше close сигнала, close < close0, stop=high отскока",
        "rec_open_dh = отскок к open, стоп за high dump",
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
    path = out / f"bt_DUMP_RECLAIM_{stamp}.txt"
    latest = out / "latest_dump_reclaim.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
