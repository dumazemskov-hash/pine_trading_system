#!/usr/bin/env python3
"""
RAID Hunter — Historical Backtest v8.32
Q1 2026 (2026-01-01 → 2026-03-31) | top-150 volume | risk 4% | BE | confirm OFF
"""

import ccxt
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
MIN_BODY_PCT = 4.0
MAX_BODY_PCT = 9.0
MAX_PREV_BODY_PCT = 3.0
COOLDOWN_BARS = 32
STOP_COOLDOWN_BARS = 96
CONFIRM_BARS = 0
CLOSE_IN_RANGE_MAX = 0.35
MIN_BODY_TO_RANGE = 0.50
IMPULSE_STRENGTH = 1.25
VOLUME_RATIO = 1.7
PRIOR_VOLUME_MULT = 1.35
CONDITION_D = 0.60
MAX_RISK_PCT = 0.04

EQL_LOOKBACK = 20
SWING_N = 2
MIN_TOUCH_DISTANCE = 3
MAX_CLUSTER_SPAN = 12
EQL_TOLERANCE_ATR = 0.30
EQL_TOLERANCE_PCT = 0.0025
MIN_ZONE_SCORE = 2
BREAK_ATR_MULT = 0.25
STOP_ATR_MULT = 0.35
SWEEP_LOOKBACK = 10
FRESH_ZONE_BARS = 8

TP1_RR = 1.6
TP2_RR = 3.0

START_CAPITAL = 300.0
RISK_PER_TRADE = 0.04
BE_AFTER_TP1 = True

RANGE_START = "2026-01-01T00:00:00Z"
RANGE_END   = "2026-04-01T00:00:00Z"
MAX_SYMBOLS = 150
SLEEP = 0.12
WARMUP_BARS = 80

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


START_MS = ms(RANGE_START)
END_MS = ms(RANGE_END)
BARS_NEEDED = int((END_MS - START_MS) / (15 * 60 * 1000)) + WARMUP_BARS + 50


def calc_atr(ohlcv, i, period=14):
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        high, low, prev_close = ohlcv[j][2], ohlcv[j][3], ohlcv[j - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
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
    ref_price = ohlcv[end][4]
    tolerance = max((atr * EQL_TOLERANCE_ATR) if atr else 0, ref_price * EQL_TOLERANCE_PCT)
    if tolerance <= 0:
        return None
    best_zone, best_score = None, 0
    for a in range(len(swings)):
        cluster = [swings[a]]
        for b in range(len(swings)):
            if a == b:
                continue
            if (abs(swings[b]["price"] - swings[a]["price"]) <= tolerance
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
        if score > best_score:
            best_score = score
            best_zone = {"zone_low": zone_low, "score": score, "touches": touches, "avg_vol": avg_vol}
    if best_zone is None or best_zone["score"] < MIN_ZONE_SCORE:
        return None
    return best_zone


def check_at(ohlcv, i):
    if i < 70 or i + max(CONFIRM_BARS, 0) >= len(ohlcv) - 1:
        return None
    if ohlcv[i][0] < START_MS or ohlcv[i][0] >= END_MS:
        return None
    last, prev = ohlcv[i], ohlcv[i - 1]
    open_p, high_p, low_p, close_p, volume = last[1], last[2], last[3], last[4], last[5]

    body_pct = abs(close_p - open_p) / open_p * 100
    if body_pct < MIN_BODY_PCT or body_pct > MAX_BODY_PCT:
        return None

    prev_body = (prev[4] - prev[1]) / prev[1] * 100 if prev[1] else 0
    if prev_body > MAX_PREV_BODY_PCT:
        return None

    impulse = (high_p - low_p) / low_p * 100 if low_p > 0 else 0
    if impulse < MIN_BODY_PCT * IMPULSE_STRENGTH:
        return None

    vol_ma = sum(c[5] for c in ohlcv[i - 20:i]) / 20
    if volume < vol_ma * VOLUME_RATIO:
        return None

    vol_recent = sum(c[5] for c in ohlcv[i - 20:i]) / 20
    vol_older = sum(c[5] for c in ohlcv[max(0, i - 60):i - 20]) / max(1, min(40, i - 20))
    if vol_older <= 0 or vol_recent < vol_older * PRIOR_VOLUME_MULT:
        return None

    atr = calc_atr(ohlcv, i)
    zone = find_equal_lows(ohlcv, i, atr)
    if zone is None:
        return None

    break_level = zone["zone_low"] - (atr * BREAK_ATR_MULT if atr else zone["zone_low"] * 0.003)
    if low_p >= break_level:
        return None

    recent_lows = [c[3] for c in ohlcv[i - SWEEP_LOOKBACK + 1:i + 1]]
    if low_p > min(recent_lows):
        return None

    if not (close_p < open_p and close_p < prev[4]):
        return None

    rng = high_p - low_p
    if rng <= 0:
        return None
    if (close_p - low_p) / rng > CLOSE_IN_RANGE_MAX:
        return None
    if abs(close_p - open_p) / rng < MIN_BODY_TO_RANGE:
        return None

    if volume < max(prev[5] * CONDITION_D, zone.get("avg_vol", 0) * 1.15):
        return None

    stop = min(high_p + (atr * STOP_ATR_MULT if atr else high_p * 0.005), close_p * (1 + MAX_RISK_PCT))
    if stop <= close_p:
        return None

    return {
        "ts": last[0], "raid_close": close_p, "stop": stop,
        "body_pct": body_pct,
        "zone_score": zone["score"], "zone_touches": zone["touches"],
        "bar_index": i, "raid_high": high_p,
    }


def confirm_entry(ohlcv, cand):
    i = cand["bar_index"]
    stop = cand["stop"]
    if CONFIRM_BARS <= 0:
        entry = cand["raid_close"]
        conf_i = i
        ts = cand["ts"]
    else:
        if i + CONFIRM_BARS >= len(ohlcv) - 1:
            return None
        for k in range(1, CONFIRM_BARS + 1):
            if ohlcv[i + k][2] >= stop:
                return None
        conf_i = i + CONFIRM_BARS
        conf = ohlcv[conf_i]
        entry = conf[4]
        ts = conf[0]
    if entry >= stop:
        return None
    risk = stop - entry
    if risk <= 0:
        return None
    if risk / entry > MAX_RISK_PCT:
        stop = entry * (1 + MAX_RISK_PCT)
        risk = stop - entry
    return {
        "ts": ts, "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body_pct": cand["body_pct"],
        "zone_score": cand["zone_score"], "zone_touches": cand["zone_touches"],
        "bar_index": conf_i, "raid_index": i,
    }


def outcome(ohlcv, sig):
    i = sig["bar_index"]
    entry = sig["entry"]
    stop, tp1, tp2 = sig["stop"], sig["tp1"], sig["tp2"]
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
    print("Загружаю рынки + тикеры (volume)...")
    markets = exchange.load_markets()
    candidates = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ]
    try:
        tickers = exchange.fetch_tickers(candidates)
    except Exception as e:
        print(f"tickers fail ({e}), беру первые {n}")
        return candidates[:n]
    ranked = []
    for s in candidates:
        tk = tickers.get(s) or {}
        vol = tk.get("quoteVolume") or 0
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = 0.0
        ranked.append((vol, s))
    ranked.sort(reverse=True)
    symbols = [s for _, s in ranked[:n]]
    print(f"Топ-{len(symbols)} по 24h volume:")
    for vol, s in ranked[:10]:
        print(f"  {s.split('/')[0]:12} vol≈{vol:,.0f}")
    return symbols


def fetch_ohlcv_range(symbol):
    since = START_MS - WARMUP_BARS * 15 * 60 * 1000
    until = END_MS + 50 * 15 * 60 * 1000
    ohlcv = []
    cursor = since
    while cursor < until:
        try:
            chunk = exchange.fetch_ohlcv(
                symbol, timeframe=TIMEFRAME, since=cursor, limit=1000
            )
        except Exception as e:
            if not ohlcv:
                raise
            break
        if not chunk:
            break
        if ohlcv and chunk[0][0] <= ohlcv[-1][0]:
            chunk = [c for c in chunk if c[0] > ohlcv[-1][0]]
        if not chunk:
            break
        ohlcv.extend(chunk)
        last_ts = chunk[-1][0]
        if last_ts >= until or len(chunk) < 1000:
            break
        cursor = last_ts + 15 * 60 * 1000
        time.sleep(SLEEP)
    return [c for c in ohlcv if c[0] >= since and c[0] <= until]


def _save_report(lines):
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "backtests"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"bt_Q1_2026_{stamp}.txt"
    latest = out_dir / "latest.txt"
    header = [
        f"RAID Backtest report | {stamp} UTC",
        f"RANGE: Q1 2026 ({RANGE_START[:10]} → {RANGE_END[:10]})",
        f"Filters: body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | cd={COOLDOWN_BARS} | stop_cd={STOP_COOLDOWN_BARS} | confirm={CONFIRM_BARS} | risk={RISK_PER_TRADE*100:.0f}% | BE | $300",
        f"Symbols=top{MAX_SYMBOLS} by volume",
        "",
    ]
    text = "\n".join(header + lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return path


def main():
    print("=" * 64)
    print("RAID Backtest v8.32 | Q1 2026 (full quarter)")
    print(f"Range: {RANGE_START[:10]} → {RANGE_END[:10]}")
    print(f"Sym=top{MAX_SYMBOLS} | Risk={RISK_PER_TRADE*100:.0f}% | BE={BE_AFTER_TP1}")
    print(f"Bars needed ≈ {BARS_NEEDED} per symbol — это займёт время")
    print("=" * 64)

    symbols = top_symbols()
    all_signals = []
    stats = defaultdict(int)

    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_range(symbol)
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] {symbol}: skip ({e})")
            continue
        if len(ohlcv) < 100:
            print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]}: мало данных ({len(ohlcv)})")
            continue

        found = 0
        last_sig_i = -999
        stop_ban_until = -1
        for i in range(70, len(ohlcv) - 8):
            if i - last_sig_i < COOLDOWN_BARS:
                continue
            if i < stop_ban_until:
                continue
            cand = check_at(ohlcv, i)
            if cand is None:
                continue
            sig = confirm_entry(ohlcv, cand)
            if sig is None:
                continue
            res, bars, r_mult = outcome(ohlcv, sig)
            found += 1
            last_sig_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban_until = sig["bar_index"] + STOP_COOLDOWN_BARS
            stats[res] += 1
            stats["TOTAL"] += 1
            stats["sum_R"] += r_mult

            ts = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            name = symbol.split("/")[0]
            all_signals.append({
                "time": ts, "symbol": name, "score": sig["zone_score"],
                "touches": sig["zone_touches"], "body": round(sig["body_pct"], 2),
                "result": res, "bars": bars, "r": r_mult,
            })
            print(f"  {ts} | {name:12} | score={sig['zone_score']} | {res:10} | bars={bars} | {r_mult:+.1f}R")

        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]}: {found} signals | bars={len(ohlcv)}")
        time.sleep(SLEEP)

    lines = []
    def emit(s=""):
        print(s)
        lines.append(s)

    emit("")
    emit("=" * 64)
    emit("SUMMARY  |  Q1 2026")
    emit("=" * 64)
    total = stats["TOTAL"]
    if total == 0:
        emit("Сигналов не найдено.")
        print(f"Отчёт: {_save_report(lines)}")
        return

    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    emit(f"Всего сигналов: {total}")
    emit(f"Wins (TP1/TP2):  {wins}  ({wins/total*100:.1f}%)")
    emit(f"BE after TP1:    {stats['TP1->BE']}")
    emit(f"Losses (STOP):   {losses}  ({losses/total*100:.1f}%)")
    emit(f"OPEN:            {stats['OPEN']}")
    emit("")
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            emit(f"  {r:12}: {stats[r]}")

    all_signals.sort(key=lambda x: x["time"])
    capital = START_CAPITAL
    peak = capital
    max_dd = 0.0
    emit("")
    emit("--- Equity ($300 start, 4% risk, BE after TP1) ---")
    emit(f"{'time':16} {'sym':12} {'result':10} {'R':>6} {'risk$':>8} {'pnl$':>8} {'equity':>10}")
    for s in all_signals:
        risk_usd = capital * RISK_PER_TRADE
        pnl = risk_usd * s["r"]
        capital += pnl
        peak = max(peak, capital)
        dd = (peak - capital) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        emit(f"{s['time']:16} {s['symbol']:12} {s['result']:10} {s['r']:+5.1f}R {risk_usd:8.2f} {pnl:+8.2f} {capital:10.2f}")
    total_r = stats["sum_R"]
    emit("")
    emit(f"Start:     ${START_CAPITAL:.2f}")
    emit(f"Final:     ${capital:.2f}  ({(capital/START_CAPITAL-1)*100:+.1f}%)")
    emit(f"Total R:   {total_r:+.1f}R")
    emit(f"Avg R:     {total_r/total:+.2f}R")
    emit(f"Max DD:    {max_dd:.1f}%")
    emit("")
    emit("--- Детальный список ---")
    for s in all_signals:
        emit(f"{s['time']} | {s['symbol']:12} | sc={s['score']} t={s['touches']} body={s['body']:5.1f}% | {s['result']:10} | {s['bars']} bars | {s['r']:+.1f}R")
    emit("=" * 64)
    emit("Готово.")
    print(f"Отчёт сохранён: {_save_report(lines)}")


if __name__ == "__main__":
    main()
