#!/usr/bin/env python3
"""
DUMP-STRUCT backtest | 60d top-150
Идея: памп → проторговка (range) → снос лоя range → шорт за ликвидностью.

Не путать с v0.2 (импульсная красная свеча).
"""

import ccxt
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

TIMEFRAME = "15m"
LOOKBACK_DAYS = 60
MAX_SYMBOLS = 150
BARS_LIMIT = 96 * LOOKBACK_DAYS + 120
SLEEP = 0.10
TP1_RR, TP2_RR = 1.6, 3.0
START_CAPITAL = 300.0
BE_AFTER_TP1 = True
COOLDOWN_PER_SYMBOL = 48
STOP_COOLDOWN = 96

# --- варианты ---
# pump_lb / pump_min: импульс ДО начала range
# range_min / range_max: длина проторговки в барах
# range_h_min / range_h_max: высота range в % от mid
# stop_mode: "range_high" | "sweep_high"
BASE = dict(
    pump_lb=16, pump_min=6.0,
    range_min=6, range_max=28,
    range_h_min=0.8, range_h_max=5.0,
    stop_mode="range_high",
    risk_pct=0.02,
    require_close_below=True,  # close < range_low, не только wick
)

VARIANTS = {
    "v1_base":      {**BASE},
    "v1_pump8":     {**BASE, "pump_min": 8.0},
    "v1_range8_24": {**BASE, "range_min": 8, "range_max": 24},
    "v1_tight":     {**BASE, "range_h_min": 1.0, "range_h_max": 3.5},
    "v1_sweep_hi":  {**BASE, "stop_mode": "sweep_high"},
    "v1_wick_ok":   {**BASE, "require_close_below": False},
    "v1_risk3":     {**BASE, "risk_pct": 0.03},
}

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
})


def had_pump_before(ohlcv, range_start, pump_lb, pump_min):
    """Памп в окне [range_start - pump_lb, range_start)."""
    a = range_start - pump_lb
    if a < 1:
        return False
    window = ohlcv[a:range_start]
    if not window:
        return False
    lo = min(c[3] for c in window)
    hi = max(c[2] for c in window)
    if lo <= 0:
        return False
    if (hi - lo) / lo * 100 >= pump_min:
        return True
    for c in window:
        o, cl = c[1], c[4]
        if o > 0 and (cl - o) / o * 100 >= pump_min:
            return True
    return False


def find_signal_at(ohlcv, i, cfg):
    """
    i = индекс бара-кандидата на sweep (последний закрытый).
    Ищем range, заканчивающийся на i-1, sweep на i.
    """
    if i < 40 or i >= len(ohlcv) - 1:
        return None

    # range ends at i-1, length L in [range_min, range_max]
    for L in range(cfg["range_min"], cfg["range_max"] + 1):
        range_start = i - L
        range_end = i  # exclusive end index = i (bars range_start .. i-1)
        if range_start < 2:
            continue
        if not had_pump_before(ohlcv, range_start, cfg["pump_lb"], cfg["pump_min"]):
            continue

        rng_bars = ohlcv[range_start:range_end]
        if len(rng_bars) < cfg["range_min"]:
            continue

        rh = max(c[2] for c in rng_bars)
        rl = min(c[3] for c in rng_bars)
        mid = (rh + rl) / 2
        if mid <= 0:
            continue
        height_pct = (rh - rl) / mid * 100
        if height_pct < cfg["range_h_min"] or height_pct > cfg["range_h_max"]:
            continue

        # необязательно: mid range roughly after pump (price elevated)
        # sweep bar
        bar = ohlcv[i]
        o, h, l, c = bar[1], bar[2], bar[3], bar[4]
        if l >= rl:
            continue  # no sweep of range low

        if cfg["require_close_below"] and c >= rl:
            continue

        # entry short
        entry = c
        if cfg["stop_mode"] == "sweep_high":
            stop = h * 1.001
        else:
            stop = rh * 1.001

        risk = stop - entry
        if risk <= 0:
            continue
        risk_pct = risk / entry
        # cap extreme stops — skip if > 8%
        if risk_pct > 0.08:
            continue
        # too tight noise
        if risk_pct < 0.003:
            continue

        return {
            "ts": bar[0],
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk * TP1_RR,
            "tp2": entry - risk * TP2_RR,
            "risk": risk,
            "risk_pct_px": risk_pct * 100,
            "range_h": round(height_pct, 2),
            "range_bars": L,
            "bar_index": i,
            "rl": rl,
            "rh": rh,
        }

    return None


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
            if tp1_hit:
                return "TP1+TP2", bars, (TP1_RR + TP2_RR) / 2
            return "TP2", bars, TP2_RR
        if low <= tp1:
            tp1_hit = True
    if tp1_hit:
        return "TP1", len(ohlcv) - i - 1, TP1_RR
    return "OPEN", len(ohlcv) - i - 1, 0.0


def top_symbols(n=MAX_SYMBOLS):
    print("markets + volume...")
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


def run_variant(name, cfg, by_sym):
    raw = []
    onebar = 0
    for symbol, ohlcv in by_sym.items():
        if len(ohlcv) < 120:
            continue
        last_i, stop_ban = -999, -1
        for i in range(50, len(ohlcv) - 8):
            if i - last_i < COOLDOWN_PER_SYMBOL or i < stop_ban:
                continue
            sig = find_signal_at(ohlcv, i, cfg)
            if not sig:
                continue
            res, bars, r = outcome(ohlcv, sig)
            last_i = i
            if res in ("STOP", "TP1->STOP"):
                stop_ban = i + STOP_COOLDOWN
                if bars == 1:
                    onebar += 1
            raw.append({
                "ts_ms": sig["ts"],
                "symbol": symbol.split("/")[0],
                "result": res,
                "bars": bars,
                "r": r,
                "range_h": sig["range_h"],
                "range_bars": sig["range_bars"],
                "stop_pct": round(sig["risk_pct_px"], 2),
            })
    raw.sort(key=lambda x: (x["ts_ms"], x["symbol"]))
    stats = defaultdict(int)
    for s in raw:
        stats[s["result"]] += 1
        stats["TOTAL"] += 1
        stats["sum_R"] += s["r"]

    capital = peak = START_CAPITAL
    max_dd = 0.0
    rp = cfg["risk_pct"]
    for s in raw:
        capital += capital * rp * s["r"]
        peak = max(peak, capital)
        max_dd = max(max_dd, (peak - capital) / peak * 100 if peak else 0)

    total = stats["TOTAL"]
    wins = stats["TP2"] + stats["TP1+TP2"] + stats["TP1"]
    losses = stats["STOP"] + stats["TP1->STOP"]
    lines = [
        "=" * 64,
        f"SUMMARY | DUMP-STRUCT {name}",
        (
            f"pump>={cfg['pump_min']}%/{cfg['pump_lb']}b range={cfg['range_min']}-{cfg['range_max']}b "
            f"h={cfg['range_h_min']}-{cfg['range_h_max']}% stop={cfg['stop_mode']} "
            f"close_below={cfg['require_close_below']} $risk={rp*100:.0f}%"
        ),
        "=" * 64,
    ]
    if total == 0:
        lines.append("Сигналов нет")
        return lines

    avg_h = sum(s["range_h"] for s in raw) / total
    avg_lb = sum(s["range_bars"] for s in raw) / total
    avg_stop = sum(s["stop_pct"] for s in raw) / total
    lines += [
        f"Всего: {total}",
        f"Wins:  {wins} ({wins/total*100:.1f}%)",
        f"BE:    {stats['TP1->BE']}",
        f"STOP:  {losses} ({losses/total*100:.1f}%)  | 1-bar STOP: {onebar}",
        f"OPEN:  {stats['OPEN']}",
    ]
    for r in ["TP2", "TP1+TP2", "TP1", "TP1->BE", "TP1->STOP", "STOP", "OPEN"]:
        if stats[r]:
            lines.append(f"  {r}: {stats[r]}")
    lines += [
        "",
        f"avg range_h={avg_h:.2f}%  avg range_bars={avg_lb:.1f}  avg_stop={avg_stop:.2f}%",
        f"Start ${START_CAPITAL:.2f} → Final ${capital:.2f} ({(capital/START_CAPITAL-1)*100:+.1f}%)",
        f"Total R {stats['sum_R']:+.1f} | Avg {stats['sum_R']/total:+.2f}R | MaxDD {max_dd:.1f}%",
        "",
    ]
    return lines


def main():
    print("=" * 64)
    print("DUMP-STRUCT | pump → range → sweep low | 60d top-150")
    print("=" * 64)
    symbols = top_symbols()
    by_sym = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            ohlcv = fetch_ohlcv_full(symbol, min(BARS_LIMIT, 3000))
        except Exception as e:
            print(f"[{idx}/{len(symbols)}] skip {symbol}: {e}")
            continue
        if len(ohlcv) >= 120:
            by_sym[symbol] = ohlcv
        print(f"[{idx}/{len(symbols)}] {symbol.split('/')[0]} bars={len(ohlcv) if symbol in by_sym else 0}")
        time.sleep(SLEEP)
    print(f"\nЗагружено: {len(by_sym)}\n")

    all_lines = [
        f"DUMP-STRUCT | {datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')} UTC",
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
    path = out / f"bt_DUMP_STRUCT_{stamp}.txt"
    latest = out / "latest_dump_struct.txt"
    text = "\n".join(all_lines) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"\nОтчёт: {path}\nГотово. Push all")


if __name__ == "__main__":
    main()
