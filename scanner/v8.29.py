import ccxt
import time
import requests
import json
import os
from datetime import datetime, timezone

# === RAID Hunter v8.29 ===
# На базе v8.28 + полное логирование сигналов для анализа без скринов
# При каждом сигнале пишем в signals/YYYY-MM-DD.jsonl:
#   - все параметры сигнала
#   - последние 40 свечей (OHLCV)
#   - данные зоны Equal Lows
#   - ATR, объёмы, метрики

TELEGRAM_TOKEN = "8821282524:AAG7OKFKdzks0qy2WdqBi4gU2dV62Isp90k"
CHAT_ID = "401292001"

TIMEFRAME = "15m"
MIN_BODY_PCT = 4.5
IMPULSE_STRENGTH = 1.25
VOLUME_RATIO = 1.9
PRIOR_VOLUME_MULT = 1.35
CONDITION_D = 0.70
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

CANDLES_TO_LOG = 40

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
        "fetchMarkets": ["linear"],
    }
})

sent_signals = set()

SIGNALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "signals")
os.makedirs(SIGNALS_DIR, exist_ok=True)


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")


def log_signal(signal: dict, ohlcv: list, atr):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(SIGNALS_DIR, f"{today}.jsonl")

    recent = []
    for c in ohlcv[-CANDLES_TO_LOG:]:
        recent.append({
            "ts": c[0],
            "o": round(c[1], 8),
            "h": round(c[2], 8),
            "l": round(c[3], 8),
            "c": round(c[4], 8),
            "v": round(c[5], 4),
        })

    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "version": "v8.29",
        "symbol": signal["symbol"],
        "timeframe": TIMEFRAME,
        "entry": signal["entry"],
        "stop": signal["stop"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "risk": signal["risk"],
        "risk_pct": round(signal["risk"] / signal["entry"] * 100, 3),
        "body_pct": signal["body_pct"],
        "zone_score": signal["zone_score"],
        "zone_touches": signal["zone_touches"],
        "zone_low": signal.get("zone_low"),
        "zone_avg_vol": signal.get("zone_avg_vol"),
        "atr": round(atr, 8) if atr else None,
        "bar_ts": signal["bar_ts"],
        "candles": recent,
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"  → лог записан: {path}")


def get_symbols():
    print("Загружаю список монет с Bybit...")
    markets = exchange.load_markets()
    symbols = []
    for symbol, market in markets.items():
        if (market.get("swap") and
            market.get("quote") == "USDT" and
            market.get("active") and
            symbol.endswith(":USDT")):
            symbols.append(symbol)
    print(f"Загружено {len(symbols)} активных монет")
    return symbols


def calc_atr(ohlcv, period=14):
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        prev_close = ohlcv[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


def is_swing_low(ohlcv, i, n=SWING_N):
    low = ohlcv[i][3]
    for k in range(1, n + 1):
        if ohlcv[i - k][3] <= low or ohlcv[i + k][3] <= low:
            return False
    return True


def find_equal_lows(ohlcv, atr):
    if len(ohlcv) < EQL_LOOKBACK + SWING_N * 2 + 2:
        return None

    end = -1
    start = end - EQL_LOOKBACK

    swings = []
    for i in range(start + SWING_N, end - SWING_N + 1):
        if is_swing_low(ohlcv, i, SWING_N):
            swings.append({
                "price": ohlcv[i][3],
                "volume": ohlcv[i][5],
                "index": i
            })

    if len(swings) < 2:
        return None

    ref_price = ohlcv[end][4]
    tolerance = max(
        (atr * EQL_TOLERANCE_ATR) if atr else 0,
        ref_price * EQL_TOLERANCE_PCT
    )
    if tolerance <= 0:
        return None

    best_zone = None
    best_score = 0

    for i in range(len(swings)):
        cluster = [swings[i]]
        for j in range(len(swings)):
            if i == j:
                continue
            price_ok = abs(swings[j]["price"] - swings[i]["price"]) <= tolerance
            time_ok = abs(swings[j]["index"] - swings[i]["index"]) <= MAX_CLUSTER_SPAN
            dist_ok = abs(swings[j]["index"] - swings[i]["index"]) >= MIN_TOUCH_DISTANCE
            if price_ok and time_ok and dist_ok:
                if not any(c["index"] == swings[j]["index"] for c in cluster):
                    cluster.append(swings[j])

        if len(cluster) < 2:
            continue

        idxs = sorted(c["index"] for c in cluster)
        too_close = False
        for a, b in zip(idxs, idxs[1:]):
            if abs(b - a) < MIN_TOUCH_DISTANCE:
                too_close = True
                break
        if too_close:
            continue

        zone_low = min(c["price"] for c in cluster)
        touches = len(cluster)
        avg_vol = sum(c["volume"] for c in cluster) / touches
        vol_ma = sum(c[5] for c in ohlcv[-21:-1]) / 20 if len(ohlcv) >= 22 else avg_vol
        is_fresh = any(c["index"] >= -FRESH_ZONE_BARS for c in cluster)

        if not is_fresh:
            continue

        score = 0
        if touches == 2:
            score += 1
        elif touches == 3:
            score += 2
        else:
            score += 3
        if avg_vol >= vol_ma * 1.2:
            score += 1
        if is_fresh:
            score += 1

        if score > best_score:
            best_score = score
            best_zone = {
                "zone_low": zone_low,
                "score": score,
                "touches": touches,
                "avg_vol": avg_vol
            }

    if best_zone is None or best_zone["score"] < MIN_ZONE_SCORE:
        return None

    return best_zone


def calculate_stop(entry, high, atr):
    stop_candidate = high + (atr * STOP_ATR_MULT if atr else high * 0.005)
    max_allowed = entry * (1 + MAX_RISK_PCT)
    return min(stop_candidate, max_allowed)


def check_signal(symbol, ohlcv_raw):
    if len(ohlcv_raw) < 75:
        return None

    ohlcv = ohlcv_raw[:-1]

    last = ohlcv[-1]
    prev = ohlcv[-2]

    open_p = last[1]
    high_p = last[2]
    low_p = last[3]
    close_p = last[4]
    volume = last[5]

    body_pct = abs(close_p - open_p) / open_p * 100
    if body_pct < MIN_BODY_PCT:
        return None

    impulse = (high_p - low_p) / low_p * 100 if low_p > 0 else 0
    if impulse < MIN_BODY_PCT * IMPULSE_STRENGTH:
        return None

    vol_ma = sum(c[5] for c in ohlcv[-21:-1]) / 20
    if volume < vol_ma * VOLUME_RATIO:
        return None

    vol_recent = sum(c[5] for c in ohlcv[-21:-1]) / 20
    vol_older = sum(c[5] for c in ohlcv[-61:-21]) / 40
    if vol_older <= 0 or vol_recent < vol_older * PRIOR_VOLUME_MULT:
        return None

    atr = calc_atr(ohlcv)
    zone = find_equal_lows(ohlcv, atr)
    if zone is None:
        return None

    break_level = zone["zone_low"] - (atr * BREAK_ATR_MULT if atr else zone["zone_low"] * 0.003)
    if low_p >= break_level:
        return None

    recent_lows = [c[3] for c in ohlcv[-SWEEP_LOOKBACK:]]
    if low_p > min(recent_lows):
        return None

    bearish = close_p < open_p and close_p < prev[4]
    if not bearish:
        return None

    zone_vol_threshold = zone.get("avg_vol", 0) * 1.15
    if volume < max(prev[5] * CONDITION_D, zone_vol_threshold):
        return None

    entry = close_p
    stop = calculate_stop(entry, high_p, atr)
    risk = stop - entry
    if risk <= 0:
        return None

    tp1 = entry - risk * TP1_RR
    tp2 = entry - risk * TP2_RR

    return {
        "symbol": symbol,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "body_pct": body_pct,
        "zone_score": zone["score"],
        "zone_touches": zone["touches"],
        "zone_low": zone["zone_low"],
        "zone_avg_vol": zone.get("avg_vol"),
        "bar_ts": last[0],
        "atr": atr,
        "ohlcv_closed": ohlcv,
    }


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] === Scanner v8.29 запущен ===")
    print("Score>=2 | Break 0.25*ATR | True Sweep 10 | Fresh Zone 8 | CondD 0.70 | Full signal log")

    symbols = get_symbols()

    while True:
        for symbol in symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=90)
                signal = check_signal(symbol, ohlcv)

                if signal is None:
                    continue

                signal_id = f"{symbol}_{signal['bar_ts']}"
                if signal_id in sent_signals:
                    continue

                sent_signals.add(signal_id)

                log_signal(signal, signal["ohlcv_closed"], signal.get("atr"))

                risk_pct = signal["risk"] / signal["entry"] * 100
                msg = (
                    f"RAID v8.29 | {symbol}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"Stop:  {signal['stop']:.6f}\n"
                    f"TP1:   {signal['tp1']:.6f} | TP2: {signal['tp2']:.6f}\n"
                    f"Risk:  {risk_pct:.2f}%\n"
                    f"Body:  {signal['body_pct']:.2f}%\n"
                    f"Zone:  score={signal['zone_score']} | touches={signal['zone_touches']}"
                )

                send_telegram(msg)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] SIGNAL → {symbol} | score={signal['zone_score']} touches={signal['zone_touches']}")

            except Exception:
                pass

        time.sleep(45)


if __name__ == "__main__":
    main()
