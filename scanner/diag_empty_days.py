#!/usr/bin/env python3
"""
Диагностика пустых дней: прогон логики DUMP v0.2b по календарным датам.
Цель: были ли сетапы 21–24 Aug 2026, или сканер просто не работал.
"""

import ccxt
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TIMEFRAME = "15m"
MAX_SYMBOLS = 150
SLEEP = 0.08

MIN_BODY, MAX_BODY = 6.5, 9.0
VOL_RATIO = 3.0
PUMP_LB, PUMP_MIN = 6, 8.0
CLOSE_POS_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
SWEEP_LOOKBACK = 10
COOLDOWN = 32

# пустые дни (UTC dates)
TARGET_DAYS = {"2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"}
# для сравнения — дни где сигналы точно были
REF_DAYS = {"2026-08-19", "2026-08-20"}

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


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


def check(ohlcv, i):
    if i < 70:
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
    return {"ts": ohlcv[i][0], "body": body, "i": i}


def day_utc(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def top_symbols(n=MAX_SYMBOLS):
    print("markets...")
    markets = exchange.load_markets()
    cands = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ]
    try:
        tickers = exchange.fetch_tickers(cands)
        ranked = sorted(
            ((float((tickers.get(s) or {}).get("quoteVolume") or 0), s) for s in cands),
            reverse=True,
        )
        return [s for _, s in ranked[:n]]
    except Exception as e:
        print("tickers", e)
        return cands[:n]


def fetch_range(symbol, since_ms, until_ms):
    """Свечи 15m от since до until."""
    out = []
    cursor = since_ms
    while cursor < until_ms:
        chunk = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=cursor, limit=1000)
        if not chunk:
            break
        for c in chunk:
            if c[0] < until_ms and (not out or c[0] > out[-1][0]):
                out.append(c)
        last = chunk[-1][0]
        if last <= cursor:
            break
        cursor = last + 15 * 60 * 1000
        time.sleep(SLEEP)
        if len(chunk) < 1000:
            break
    return out


def main():
    print("=" * 64)
    print("DIAG empty days | DUMP v0.2b logic replay")
    print(f"Target: {sorted(TARGET_DAYS)} | Ref: {sorted(REF_DAYS)}")
    print("=" * 64)

    # окно: с запасом до ref + target (warmup ~3 дня)
    start = datetime(2026, 8, 16, tzinfo=timezone.utc)
    end = datetime(2026, 8, 25, tzinfo=timezone.utc)
    since_ms = int(start.timestamp() * 1000)
    until_ms = int(end.timestamp() * 1000)

    symbols = top_symbols()
    by_day = defaultdict(list)  # day -> list of (sym, body, ts)
    per_sym_cd = {}

    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_range(symbol, since_ms, until_ms)
        except Exception as e:
            print(f"[{idx}] skip {symbol}: {e}")
            continue
        if len(ohlcv) < 80:
            continue
        last_i = -999
        for i in range(70, len(ohlcv)):
            if i - last_i < COOLDOWN:
                continue
            sig = check(ohlcv, i)
            if not sig:
                continue
            d = day_utc(sig["ts"])
            if d not in TARGET_DAYS and d not in REF_DAYS:
                continue
            last_i = i
            sym = symbol.split("/")[0]
            by_day[d].append((sym, sig["body"], sig["ts"]))
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv)}")
        time.sleep(SLEEP)

    print("\n" + "=" * 64)
    print("РЕЗУЛЬТАТ по дням (UTC)")
    print("=" * 64)
    all_days = sorted(TARGET_DAYS | REF_DAYS)
    for d in all_days:
        rows = by_day.get(d, [])
        tag = "TARGET" if d in TARGET_DAYS else "REF"
        print(f"\n{d} [{tag}]  N={len(rows)}")
        for sym, body, ts in sorted(rows, key=lambda x: x[2]):
            t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M")
            print(f"  {t}  {sym:12} body={body:.1f}%")

    print("\n" + "=" * 64)
    print("СВОДКА")
    for d in all_days:
        n = len(by_day.get(d, []))
        tag = "пусто?" if d in TARGET_DAYS else "ref"
        print(f"  {d}: {n:3d}  ({tag})")
    print("=" * 64)
    print("Если TARGET N>0 а jsonl пуст → сканер не писал (упал/не бежал).")
    print("Если TARGET N=0 и REF N>0 → рынок реально без сетапов.")


if __name__ == "__main__":
    main()
