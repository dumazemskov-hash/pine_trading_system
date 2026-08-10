#!/usr/bin/env python3
"""
DUMP lab — 4 варианта на одном датасете (60d, top-150).
v0.1_base   pump>=5% vol>=1.7x body 4-9 risk 4%
v0.2_strict pump>=8% vol>=2.5x body 5-9 risk 2%
v0.3_gcd    = v0.2 + global cooldown 16 bars
v0.4_ultra  pump>=10% vol>=3x body 5.5-8.5 risk 2% gcd 24
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
STOP_ATR_MULT = 0.35
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
TP1_RR = 1.6
TP2_RR = 3.0
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN_PER_SYMBOL = 32
STOP_COOLDOWN = 96

VARIANTS = {
    "v0.1_base": dict(
        pump_lb=6, pump_min=5.0, min_body=4.0, max_body=9.0,
        vol_ratio=1.7, risk_pct=0.04, global_cd=0,
    ),
    "v0.2_strict": dict(
        pump_lb=6, pump_min=8.0, min_body=5.0, max_body=9.0,
        vol_ratio=2.5, risk_pct=0.02, global_cd=0,
    ),
    "v0.3_gcd": dict(
        pump_lb=4, pump_min=8.0, min_body=5.0, max_body=9.0,
        vol_ratio=2.5, risk_pct=0.02, global_cd=16,
    ),
    "v0.4_ultra": dict(
        pump_lb=4, pump_min=10.0, min_body=5.5, max_body=8.5,
        vol_ratio=3.0, risk_pct=0.02, global_cd=24,
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


def check_at(ohlcv, i, cfg):
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
    if (c - l) / rng > CLOSE_IN_RANGE_MAX:
        return None
    if abs(c - o) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(x[5] for x in ohlcv[i - 20:i]) / 20
    if v < vol_ma * cfg["vol_ratio"]:
        return None
    if l > min(x[3] for x in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]):
        return None
    atr = calc_atr(ohlcv, i)
    entry = c
    stop = min(h + (atr * STOP_ATR_MULT if atr else h * 0.005), entry * (1 + cfg["risk_pct"]))
    risk = stop - entry
    if risk <= 0:
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


def run_variant(name, cfg, by_symbol_ohlcv):
    raw = []
    for symbol, ohlcv in by_symbol_ohlcv.items():
        if len(ohlcv) < 100:
            continue
        last_i, stop_ban = -999, -1
        for i in range(70, len(ohlcv) - 8):
            if i - last_i < COOLDOWN_PER_SYMBOL or i < stop_ban:
                continue
            sig = check_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
            raw.append({
                "ts_ms": sig["ts"], "symbol": symbol.split("/")[0],
                "body": round(sig["body"], 2), "result": res, "bars": bars, "r": r,
            })
    raw.sort(key=lambda x: (x["ts_ms"], x["symbol"]))

    gcd = cfg.get("global_cd") or 0
    filtered = []
    last_global_ts = -10**18
    bar_ms = 15 * 60 * 1000
    for s in raw:
        if gcd and (s["ts_ms"] - last_global_ts) < gcd * bar_ms:
            continue
        filtered.append(s)
        last_global_ts = s["ts_ms"]

    stats = defaultdict(int)
    for s in filtered:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]

    capital = peak = START_CAPITAL
    max_dd = 0.0
    risk_pct = cfg["risk_pct"]
    equity_lines = []
    for s in filtered:
        risk_usd = capital * risk_pct
        capital += risk_usd * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
        ts = datetime.fromtimestamp(s["ts_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        equity_lines.append(
            f"{ts} | {s['symbol']:12} | body={s['body']:5.1f}% | {s['result']:10} | {s['bars']:3}b | {s['r']:+.1f}R | eq={capital:.2f}"
        )

    total = stats["TOTAL"]
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    lines = []
    lines.append("=" * 64)
    lines.append(f"SUMMARY | DUMP {name}")
    lines.append(
        f"pump>={cfg['pump_min']}%/{cfg['pump_lb']}b vol>={cfg['vol_ratio']}x "
        f"body {cfg['min_body']}-{cfg['max_body']}% risk={risk_pct*100:.0f}% gcd={gcd}"
    )
    lines.append("=" * 64)
    if total == 0:
        lines.append("Сигналов нет")
        return lines
    lines.append(f"Всего: {total}")
    lines.append(f"Wins:  {wins} ({wins/total*100:.1f}%)")
    lines.append(f"BE:    {stats['TP1->BE']}")
    lines.append(f"STOP:  {losses} ({losses/total*100:.1f}%)")
    lines.append(f"OPEN:  {stats['OPEN']}")
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            lines.append(f"  {r}: {stats[r]}")
    lines.append("")
    lines.append(f"Start ${START_CAPITAL:.2f} → Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%)")
    lines.append(f"Total R {stats['sum_R']:+.1f} | Avg {stats['sum_R']/total:+.2f}R | MaxDD {max_dd:.1f}%")
    lines.append("")
    lines.append("--- trades ---")
    lines.extend(equity_lines)
    lines.append("")
    return lines


def main():
    print("=" * 64)
    print("DUMP LAB | 4 variants | 60d | top-150")
    print("Fetch once → filter many")
    print("=" * 64)

    symbols = top_symbols()
    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 100:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)

    print(f"\nЗагружено: {len(by_sym)}. Считаю варианты...\n")

    all_lines = [
        f"DUMP LAB | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
        f"Days={LOOKBACK_DAYS} | symbols={len(by_sym)}",
        "",
    ]
    for name, cfg in VARIANTS.items():
        print(f">>> {name}")
        part = run_variant(name, cfg, by_sym)
        for line in part:
            print(line)
        all_lines.extend(part)
        all_lines.append("")

    root = Path(__file__).resolve().parent.parent
    out = root / "backtests"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out / f"bt_DUMP_LAB_{stamp}.txt"
    latest = out / "latest_dump.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}")
    print("Готово. Push all")


if __name__ == "__main__":
    main()
