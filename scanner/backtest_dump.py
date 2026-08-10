#!/usr/bin/env python3
"""
DUMP-after-PUMP Backtest
После пампа (green body >=5% в последних 6 барах) ловим dump-свечу вниз.
Без equal-lows — чистый dump.
"""

import ccxt
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
MIN_BODY_PCT = 4.0
MAX_BODY_PCT = 9.0
PUMP_LOOKBACK = 6
PUMP_MIN_BODY = 5.0
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
VOLUME_RATIO = 1.7
COOLDOWN_BARS = 32
STOP_COOLDOWN_BARS = 96
MAX_RISK_PCT = 0.04
STOP_ATR_MULT = 0.35
SWEEP_LOOKBACK = 10
TP1_RR = 1.6
TP2_RR = 3.0

START_CAPITAL = 300.0
RISK_PER_TRADE = 0.04
BE_AFTER_TP1 = True

LOOKBACK_DAYS = 60
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 80
SLEEP = 0.10

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
    if i < PUMP_LOOKBACK + 1:
        return False
    for j in range(i - PUMP_LOOKBACK, i):
        o, c = ohlcv[j][1], ohlcv[j][4]
        if o <= 0:
            continue
        if (c - o) / o * 100 >= PUMP_MIN_BODY:
            return True
    window = ohlcv[i - PUMP_LOOKBACK:i]
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    return lo > 0 and (hi - lo) / lo * 100 >= PUMP_MIN_BODY


def check_at(ohlcv, i):
    if i < 70 or i >= len(ohlcv) - 1:
        return None
    if not had_pump(ohlcv, i):
        return None
    last = ohlcv[i]
    open_p, high_p, low_p, close_p, volume = last[1], last[2], last[3], last[4], last[5]
    if close_p >= open_p:
        return None
    body_pct = (open_p - close_p) / open_p * 100
    if body_pct < MIN_BODY_PCT or body_pct > MAX_BODY_PCT:
        return None
    rng = high_p - low_p
    if rng <= 0:
        return None
    if (close_p - low_p) / rng > CLOSE_IN_RANGE_MAX:
        return None
    if abs(close_p - open_p) / rng < MIN_BODY_TO_RANGE:
        return None
    vol_ma = sum(c[5] for c in ohlcv[i - 20:i]) / 20
    if volume < vol_ma * VOLUME_RATIO:
        return None
    recent_lows = [c[3] for c in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]]
    if low_p > min(recent_lows):
        return None
    atr = calc_atr(ohlcv, i)
    entry = close_p
    stop = min(high_p + (atr * STOP_ATR_MULT if atr else high_p * 0.005), entry * (1 + MAX_RISK_PCT))
    risk = stop - entry
    if risk <= 0:
        return None
    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body_pct": body_pct, "bar_index": i,
    }


def outcome(ohlcv, sig):
    i = sig["bar_index"]
    entry, stop, tp1, tp2 = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"]
    tp1_hit = False
    for j in range(i + 1, len(ohlcv)):
        high, low = ohlcv[j][2], ohlcv[j][3]
        bars = j - i
        eff_stop = entry if (tp1_hit and BE_AFTER_TP1) else stop
        if high >= eff_stop:
            if tp1_hit and BE_AFTER_TP1:
                return ("TP1->BE", bars, TP1_RR * 0.5)
            if tp1_hit:
                return ("TP1->STOP", bars, TP1_RR * 0.5 - 0.5)
            return ("STOP", bars, -1.0)
        if low <= tp2:
            if tp1_hit:
                return ("TP1+TP2", bars, (TP1_RR + TP2_RR) / 2)
            return ("TP2", bars, TP2_RR)
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return ("TP1", len(ohlcv) - i - 1, TP1_RR)
    return ("OPEN", len(ohlcv) - i - 1, 0.0)


def top_symbols(n=MAX_SYMBOLS):
    print("Загружаю рынки + volume...")
    markets = exchange.load_markets()
    candidates = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ]
    try:
        tickers = exchange.fetch_tickers(candidates)
        ranked = sorted(
            ((float((tickers.get(s) or {}).get("quoteVolume") or 0), s) for s in candidates),
            reverse=True,
        )
        return [s for _, s in ranked[:n]]
    except Exception as e:
        print(f"tickers fail: {e}")
        return candidates[:n]


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


def _save_report(lines):
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "backtests"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"bt_DUMP_{stamp}.txt"
    latest = out_dir / "latest_dump.txt"
    header = [
        f"DUMP-after-PUMP report | {stamp} UTC",
        f"pump_lb={PUMP_LOOKBACK} pump_min={PUMP_MIN_BODY}% | body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | vol>={VOLUME_RATIO}x | risk={RISK_PER_TRADE*100:.0f}%",
        f"Days={LOOKBACK_DAYS} | top{MAX_SYMBOLS} | BE after TP1",
        "",
    ]
    text = "\n".join(header + lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return path


def main():
    print("=" * 64)
    print("DUMP-after-PUMP Backtest | 60d | top-volume | risk 4%")
    print(f"Pump: last {PUMP_LOOKBACK} bars green>={PUMP_MIN_BODY}%")
    print(f"Dump: body {MIN_BODY_PCT}-{MAX_BODY_PCT}% red + vol + sweep")
    print("=" * 64)

    symbols = top_symbols()
    all_signals = []
    stats = defaultdict(int)

    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip ({e})")
            continue
        if len(ohlcv) < 100:
            continue
        found = 0
        last_sig_i, stop_ban = -999, -1
        for i in range(70, len(ohlcv) - 8):
            if i - last_sig_i < COOLDOWN_BARS or i < stop_ban:
                continue
            sig = check_at(ohlcv, i)
            if sig is None:
                continue
            res, bars, r_mult = outcome(ohlcv, sig)
            found += 1
            last_sig_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN_BARS
            stats[res] += 1
            stats["TOTAL"] += 1
            stats["sum_R"] += r_mult
            ts = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            name = symbol.split("/")[0]
            all_signals.append({"time": ts, "symbol": name, "body": round(sig["body_pct"], 2),
                                "result": res, "bars": bars, "r": r_mult})
            print(f"  {ts} | {name:12} | {res:10} | {r_mult:+.1f}R")
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]}: {found}")
        time.sleep(SLEEP)

    lines = []
    def emit(s=""):
        print(s); lines.append(s)

    emit(""); emit("=" * 64); emit("SUMMARY | DUMP-after-PUMP"); emit("=" * 64)
    total = stats["TOTAL"]
    if total == 0:
        emit("Сигналов нет."); print(_save_report(lines)); return
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    emit(f"Всего: {total}")
    emit(f"Wins:  {wins} ({wins/total*100:.1f}%)")
    emit(f"BE:    {stats['TP1->BE']}")
    emit(f"STOP:  {losses} ({losses/total*100:.1f}%)")
    emit(f"OPEN:  {stats['OPEN']}")
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            emit(f"  {r}: {stats[r]}")

    all_signals.sort(key=lambda x: x["time"])
    capital = peak = START_CAPITAL
    max_dd = 0.0
    emit(""); emit("--- Equity $300 / 4% ---")
    for s in all_signals:
        risk_usd = capital * RISK_PER_TRADE
        capital += risk_usd * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)
        emit(f"{s['time']} {s['symbol']:12} {s['result']:10} {s['r']:+.1f}R eq={capital:.2f}")
    emit("")
    emit(f"Start ${START_CAPITAL:.2f} → Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%)")
    emit(f"Total R {stats['sum_R']:+.1f} | Avg {stats['sum_R']/total:+.2f}R | MaxDD {max_dd:.1f}%")
    emit(""); emit("--- list ---")
    for s in all_signals:
        emit(f"{s['time']} | {s['symbol']:12} | body={s['body']:5.1f}% | {s['result']:10} | {s['bars']}b | {s['r']:+.1f}R")
    emit("=" * 64); emit("Готово.")
    print(f"Отчёт: {_save_report(lines)}")


if __name__ == "__main__":
    main()
