#!/usr/bin/env python3
"""
RAID Hunter — Historical Backtest v8.32
Фильтры: body 4-9%, prev<=3%, cooldown 32,
         close in bottom 35% of range, body/range>=50%,
         STOP-cooldown 96 bars (24h) per symbol
Equity: $300 start, 2% risk, BE after TP1
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
COOLDOWN_BARS = 32          # 8ч после любого сигнала по символу
STOP_COOLDOWN_BARS = 96     # 24ч после STOP по символу
CLOSE_IN_RANGE_MAX = 0.35   # close в нижних 35% диапазона свечи
MIN_BODY_TO_RANGE = 0.50    # тело >= 50% диапазона (не фитильная свеча)
IMPULSE_STRENGTH = 1.25
VOLUME_RATIO = 1.7
PRIOR_VOLUME_MULT = 1.35
CONDITION_D = 0.60
MAX_RISK_PCT = 0.02

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

START_CAPITAL = 300.0   # стартовый депозит $
RISK_PER_TRADE = 0.02   # риск на сделку 2% от капитала
BE_AFTER_TP1 = True     # после TP1 стоп → безубыток

LOOKBACK_DAYS = 30
MAX_SYMBOLS = 250
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
    if i < 70 or i >= len(ohlcv) - 1:
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

    # качество свечи: close у низа + доминирующее тело (анти 1-bar death)
    rng = high_p - low_p
    if rng <= 0:
        return None
    close_pos = (close_p - low_p) / rng
    if close_pos > CLOSE_IN_RANGE_MAX:
        return None
    body = abs(close_p - open_p)
    if body / rng < MIN_BODY_TO_RANGE:
        return None

    if volume < max(prev[5] * CONDITION_D, zone.get("avg_vol", 0) * 1.15):
        return None

    entry = close_p
    stop = min(high_p + (atr * STOP_ATR_MULT if atr else high_p * 0.005), entry * (1 + MAX_RISK_PCT))
    risk = stop - entry
    if risk <= 0:
        return None

    return {
        "ts": last[0], "entry": entry, "stop": stop,
        "tp1": entry - risk * TP1_RR, "tp2": entry - risk * TP2_RR,
        "risk": risk, "body_pct": body_pct,
        "zone_score": zone["score"], "zone_touches": zone["touches"],
        "bar_index": i,
    }


def outcome(ohlcv, sig):
    """Возвращает (result, bars, r_multiple).
    После TP1 стоп → BE. Half @ TP1 + rest BE = +0.8R.
    """
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
    print("Загружаю рынки...")
    markets = exchange.load_markets()
    symbols = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ]
    symbols = symbols[:n]
    print(f"Символов для прогона: {len(symbols)}")
    return symbols


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
    path = out_dir / f"bt_{stamp}.txt"
    latest = out_dir / "latest.txt"
    header = [
        f"RAID Backtest report | {stamp} UTC",
        f"Filters: body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | prev<={MAX_PREV_BODY_PCT}% | cd={COOLDOWN_BARS} | stop_cd={STOP_COOLDOWN_BARS} | close<={CLOSE_IN_RANGE_MAX} | body/rng>={MIN_BODY_TO_RANGE} | BE | $300",
        f"Days={LOOKBACK_DAYS} | Symbols<={MAX_SYMBOLS}",
        "",
    ]
    text = "\n".join(header + lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return path


def main():
    print("=" * 64)
    print("RAID Backtest v8.32")
    print(
        f"Days={LOOKBACK_DAYS} | Sym<={MAX_SYMBOLS} | "
        f"Body {MIN_BODY_PCT}-{MAX_BODY_PCT}% | prev<={MAX_PREV_BODY_PCT}% | cd={COOLDOWN_BARS} | stop_cd={STOP_COOLDOWN_BARS} | close<={CLOSE_IN_RANGE_MAX}"
    )
    print(f"Capital=${START_CAPITAL:.0f} | Risk={RISK_PER_TRADE*100:.0f}% | BE after TP1={BE_AFTER_TP1}")
    print("=" * 64)

    symbols = top_symbols()
    all_signals = []
    stats = defaultdict(int)

    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] {symbol}: skip ({e})")
            continue

        if len(ohlcv) < 100:
            continue

        found = 0
        last_sig_i = -999
        stop_ban_until = -1  # index until which symbol is banned after STOP
        for i in range(70, len(ohlcv) - 8):
            if i - last_sig_i < COOLDOWN_BARS:
                continue
            if i < stop_ban_until:
                continue
            sig = check_at(ohlcv, i)
            if sig is None:
                continue
            res, bars, r_mult = outcome(ohlcv, sig)
            found += 1
            last_sig_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban_until = i + STOP_COOLDOWN_BARS
            stats[res] += 1
            stats["TOTAL"] += 1
            stats[f"score_{sig['zone_score']}"] += 1
            stats["sum_R"] += r_mult

            ts = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            name = symbol.split("/")[0]
            all_signals.append({
                "time": ts, "symbol": name, "score": sig["zone_score"],
                "touches": sig["zone_touches"], "body": round(sig["body_pct"], 2),
                "result": res, "bars": bars, "r": r_mult,
            })
            print(f"  {ts} | {name:12} | score={sig['zone_score']} | {res:10} | bars={bars} | {r_mult:+.1f}R")

        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]}: {found} signals")
        time.sleep(SLEEP)

    lines = []
    def emit(s=""):
        print(s)
        lines.append(s)

    emit("")
    emit("=" * 64)
    emit("SUMMARY")
    emit("=" * 64)
    total = stats["TOTAL"]
    if total == 0:
        emit("Сигналов не найдено.")
        path = _save_report(lines)
        print(f"Отчёт сохранён: {path}")
        return

    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    be = stats["TP1->BE"]
    emit(f"Всего сигналов: {total}")
    emit(f"Wins (TP1/TP2):  {wins}  ({wins/total*100:.1f}%)")
    emit(f"BE after TP1:    {be}")
    emit(f"Losses (STOP):   {losses}  ({losses/total*100:.1f}%)")
    emit(f"OPEN:            {stats['OPEN']}")
    emit("")
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            emit(f"  {r:12}: {stats[r]}")

    # хронологический порядок для equity
    all_signals.sort(key=lambda x: x["time"])

    capital = START_CAPITAL
    peak = capital
    max_dd = 0.0
    emit("")
    emit("--- Equity ($300 start, 2% risk, BE after TP1) ---")
    emit(f"{'time':16} {'sym':12} {'result':10} {'R':>6} {'risk$':>8} {'pnl$':>8} {'equity':>10}")
    for s in all_signals:
        risk_usd = capital * RISK_PER_TRADE
        pnl = risk_usd * s["r"]
        capital += pnl
        peak = max(peak, capital)
        dd = (peak - capital) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        emit(
            f"{s['time']:16} {s['symbol']:12} {s['result']:10} {s['r']:+5.1f}R "
            f"{risk_usd:8.2f} {pnl:+8.2f} {capital:10.2f}"
        )
    total_r = stats["sum_R"]
    emit("")
    emit(f"Start:     ${START_CAPITAL:.2f}")
    emit(f"Final:     ${capital:.2f}  ({(capital/START_CAPITAL-1)*100:+.1f}%)")
    emit(f"Total R:   {total_r:+.1f}R")
    emit(f"Avg R:     {total_r/total:+.2f}R" if total else "Avg R: n/a")
    emit(f"Max DD:    {max_dd:.1f}%")

    emit("")
    emit("--- Детальный список ---")
    for s in all_signals:
        emit(
            f"{s['time']} | {s['symbol']:12} | sc={s['score']} t={s['touches']} "
            f"body={s['body']:5.1f}% | {s['result']:10} | {s['bars']} bars | {s['r']:+.1f}R"
        )

    emit("=" * 64)
    emit("Готово.")
    path = _save_report(lines)
    print(f"Отчёт сохранён: {path}")


if __name__ == "__main__":
    main()
