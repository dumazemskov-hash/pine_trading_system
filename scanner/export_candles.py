#!/usr/bin/env python3
"""
Выгрузка OHLCV вокруг сигналов бэктеста.
Для каждого сигнала: 15 свечей до + сигнал + 10 после.
Результат: signals_candles.txt
"""

import ccxt
import time
from datetime import datetime, timezone
from pathlib import Path

TIMEFRAME = "15m"
MIN_BODY_PCT = 4.0
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

LOOKBACK_DAYS = 30
MAX_SYMBOLS = 250
BARS_LIMIT = 96 * LOOKBACK_DAYS + 80
SLEEP = 0.10
BARS_BEFORE = 15
BARS_AFTER = 10

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
    if body_pct < MIN_BODY_PCT:
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
        "risk": risk, "body_pct": body_pct, "atr": atr,
        "zone_score": zone["score"], "zone_touches": zone["touches"],
        "zone_low": zone["zone_low"], "bar_index": i,
        "vol": volume, "vol_ma": vol_ma,
    }


def outcome(ohlcv, sig):
    i = sig["bar_index"]
    stop, tp1, tp2 = sig["stop"], sig["tp1"], sig["tp2"]
    tp1_hit = False
    for j in range(i + 1, len(ohlcv)):
        high, low = ohlcv[j][2], ohlcv[j][3]
        bars = j - i
        if high >= stop:
            return ("STOP", bars) if not tp1_hit else ("TP1->STOP", bars)
        if low <= tp2:
            return ("TP2", bars) if not tp1_hit else ("TP1+TP2", bars)
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return ("TP1", len(ohlcv) - i - 1)
    return ("OPEN", len(ohlcv) - i - 1)


def fmt_bar(c, mark=""):
    ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
    o, h, l, cl, v = c[1], c[2], c[3], c[4], c[5]
    body = (cl - o) / o * 100 if o else 0
    wick_up = (h - max(o, cl)) / o * 100 if o else 0
    wick_dn = (min(o, cl) - l) / o * 100 if o else 0
    return (
        f"  {ts} {mark:6} O={o:.6g} H={h:.6g} L={l:.6g} C={cl:.6g} "
        f"V={v:.0f} body={body:+.2f}% upW={wick_up:.2f}% dnW={wick_dn:.2f}%"
    )


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


def main():
    out_path = Path(__file__).resolve().parent.parent / "signals_candles.txt"
    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 72)
    emit("RAID signal candle dump")
    emit("=" * 72)

    markets = exchange.load_markets()
    symbols = [
        s for s, m in markets.items()
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active") and s.endswith(":USDT")
    ][:MAX_SYMBOLS]

    n_sig = 0
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}] {symbol} skip {e}")
            continue
        if len(ohlcv) < 100:
            continue

        for i in range(70, len(ohlcv) - 8):
            sig = check_at(ohlcv, i)
            if sig is None:
                continue
            res, bars = outcome(ohlcv, sig)
            n_sig += 1
            name = symbol.split("/")[0]
            ts = datetime.fromtimestamp(sig["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

            emit("")
            emit("#" * 72)
            emit(f"SIGNAL #{n_sig} | {name} | {ts} UTC | RESULT={res} after {bars} bars")
            emit(
                f"  entry={sig['entry']:.6g} stop={sig['stop']:.6g} "
                f"tp1={sig['tp1']:.6g} tp2={sig['tp2']:.6g}"
            )
            emit(
                f"  body={sig['body_pct']:.2f}% score={sig['zone_score']} "
                f"touches={sig['zone_touches']} zone_low={sig['zone_low']:.6g}"
            )
            atr = sig.get("atr")
            atr_s = f"{atr:.6g}" if atr else "0"
            emit(
                f"  atr={atr_s} vol={sig['vol']:.0f} "
                f"vol_ma={sig['vol_ma']:.0f} vol_ratio={sig['vol']/sig['vol_ma']:.2f}"
            )
            emit("-" * 72)

            start = max(0, i - BARS_BEFORE)
            end = min(len(ohlcv), i + 1 + BARS_AFTER)
            for j in range(start, end):
                mark = ""
                if j == i:
                    mark = "<<<SIG"
                elif j == i - 1:
                    mark = "prev"
                emit(fmt_bar(ohlcv[j], mark))

            emit("-" * 72)

        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} done, signals so far={n_sig}")
        time.sleep(SLEEP)

    emit("")
    emit(f"TOTAL SIGNALS DUMPED: {n_sig}")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved to: {out_path}")
    print("Скопируй файл signals_candles.txt или его часть мне.")


if __name__ == "__main__":
    main()
