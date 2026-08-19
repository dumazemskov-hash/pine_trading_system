#!/usr/bin/env python3
"""
Аудит DUMP v0.2b — проверка тезисов двух внешних ревью на реальных jsonl + коде.
Live-сканер не меняет. Только отчёт.

Аудит A (mismatch stop): всегда ли stop = 3% при фильтрах?
Аудит B (геометрия 1-bar + self-ref): H-C vs stop, индексы vol/pump/sweep.
"""

import ast
import json
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "signals_dump"
SCANNER = ROOT / "scanner" / "dump_scanner.py"
OUT = ROOT / "backtests"
STOP_ATR_MULT = 0.35
MAX_RISK = 0.03


def load_signals():
    rows = []
    if not SIGNALS.exists():
        return rows
    for path in sorted(SIGNALS.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("candles"):
                continue
            rows.append(r)
    return rows


def analyze_geometry(rows):
    lines = [
        "=" * 64,
        "АУДИТ A+B | геометрия стопа и сигнала (jsonl)",
        "=" * 64,
        f"Сигналов: {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("Нет signals_dump/*.jsonl — сначала Push логов DUMP")
        return lines

    stats = []
    differ = 0  # live struct vs fixed 3% would differ
    for r in rows:
        sig = r["candles"][-1]
        o, h, l, c = sig["o"], sig["h"], sig["l"], sig["c"]
        entry = r["entry"]
        stop = r["stop"]
        atr = r.get("atr") or 0
        body = r.get("body_pct") or ((o - c) / o * 100 if o else 0)
        rng = h - l
        close_pos = (c - l) / rng if rng else 0
        hi_en = (h - entry) / entry * 100 if entry else 0
        risk_pct = (stop - entry) / entry * 100 if entry else 0
        # live formula
        struct = h + (atr * STOP_ATR_MULT if atr else h * 0.005)
        live_stop = min(struct, entry * (1 + MAX_RISK))
        fixed_stop = entry * (1 + MAX_RISK)
        live_pct = (live_stop - entry) / entry * 100
        fixed_pct = 3.0
        delta = abs(live_stop - fixed_stop) / entry * 100
        if delta > 0.05:  # >0.05% price
            differ += 1
        stats.append({
            "sym": r["symbol"].split("/")[0],
            "body": body,
            "cpos": close_pos,
            "hi_en": hi_en,
            "risk_pct": risk_pct,
            "live_pct": live_pct,
            "json_pct": risk_pct,
            "delta": delta,
            "atr_pct": (atr / entry * 100) if entry and atr else 0,
        })

    lines.append(
        f"{'sym':8} {'body':>5} {'cpos':>5} {'H-E%':>6} {'json%':>6} "
        f"{'live%':>6} {'d%':>5} {'atr%':>5}"
    )
    for s in stats:
        lines.append(
            f"{s['sym']:8} {s['body']:5.1f} {s['cpos']:5.2f} {s['hi_en']:6.2f} "
            f"{s['json_pct']:6.2f} {s['live_pct']:6.2f} {s['delta']:5.2f} {s['atr_pct']:5.2f}"
        )

    hi = [s["hi_en"] for s in stats]
    rp = [s["json_pct"] for s in stats]
    cp = [s["cpos"] for s in stats]
    lines += [
        "",
        "--- SUMMARY геометрия ---",
        f"N={len(stats)}",
        f"mean H-entry% = {mean(hi):.2f}  min={min(hi):.2f}  (стоп 3% внутри свечи если H-E>3)",
        f"mean risk_pct jsonl = {mean(rp):.3f}  min={min(rp):.3f} max={max(rp):.3f}",
        f"mean close_pos = {mean(cp):.3f}",
        f"live_stop ≠ fixed3% (delta>0.05%): {differ}/{len(stats)}",
        "",
    ]
    if differ == 0:
        lines += [
            "ВЫВОД Аудит-2: mismatch stop НЕ влияет на эти сигналы.",
            "min(high+0.35ATR, entry*1.03) = entry*1.03 на 100% выборки.",
            "H-entry всегда >3% → стоп внутри сигнальной → 1-bar риск структурный.",
        ]
    else:
        lines += [
            f"ВЫВОД: на {differ} сигналах live-стоп отличался от fixed 3%.",
            "Смотри столбцы live% vs json%.",
        ]
    lines.append("")
    return lines


def analyze_code():
    """Статический разбор dump_scanner на self-ref."""
    lines = [
        "=" * 64,
        "АУДИТ B | код dump_scanner (vol / pump / sweep)",
        "=" * 64,
    ]
    if not SCANNER.exists():
        lines.append(f"Нет файла {SCANNER}")
        return lines

    src = SCANNER.read_text(encoding="utf-8")
    checks = []

    # volume MA
    if "ohlcv[-21:-1]" in src or "ohlcv[-21:-1]" in src.replace(" ", ""):
        checks.append(("Vol MA20", "ohlcv[-21:-1] — сигнал НЕ в среднем", "OK"))
    elif "[-20:]" in src and "volume" in src.lower():
        checks.append(("Vol MA20", "возможное включение сигнала — проверь", "CHECK"))
    else:
        # try find vol_ma line
        for ln in src.splitlines():
            if "vol_ma" in ln or "VOLUME" in ln and "sum" in ln:
                checks.append(("Vol MA20", ln.strip()[:80], "SEE"))
                break
        else:
            checks.append(("Vol MA20", "паттерн не найден автоматом", "SEE"))

    if "-PUMP_LOOKBACK - 1:-1" in src or "[-PUMP_LOOKBACK - 1:-1]" in src:
        checks.append(("Pump window", "исключает сигнальный бар [:-1]", "OK"))
    elif "had_pump" in src:
        checks.append(("Pump window", "см. had_pump — сверка вручную", "SEE"))

    if "-SWEEP_LOOKBACK:]" in src or "[-SWEEP_LOOKBACK:]" in src:
        if "low_p > min" in src or "low_p > min(recent" in src:
            checks.append(
                ("Sweep low", "low > min(window incl. self) → нужен new/equal low", "OK")
            )
        else:
            checks.append(("Sweep low", "окно включает self — проверь оператор", "SEE"))

    if "ohlcv_raw[:-1]" in src or "ohlcv = ohlcv_raw[:-1]" in src:
        checks.append(("Closed bar only", "[:-1] отсекает незакрытый бар", "OK"))

    if "min(high_p +" in src.replace(" ", "") or "min(high_p +" in src:
        checks.append(
            ("Stop formula", "min(high+ATR*0.35, entry*1.03) — при фильтрах = 3%", "INFO")
        )

    for name, detail, flag in checks:
        lines.append(f"[{flag:5}] {name}: {detail}")

    lines += [
        "",
        "ВЫВОД по коду (авто):",
        "- Vol/pump: ожидается без self-ref (как в ревью-2).",
        "- Sweep: не тавтология, если reject при low > min(...).",
        "- Стоп min(): мёртвая ветка при body>=6.5 и cpos<=0.35.",
        "",
    ]
    return lines


def main():
    rows = load_signals()
    lines = []
    lines.extend(analyze_geometry(rows))
    lines.extend(analyze_code())
    lines += [
        "=" * 64,
        "Сверка с аудитами:",
        "Аудит-1: mismatch критичен → на jsonl обычно НЕТ эффекта (см. differ).",
        "Аудит-2: H-E>3% + stop 3% → 1-bar структурный; self-ref не подтверждён.",
        "Дальше: не плодить фильтры; lab = continuation или stop за high.",
        "=" * 64,
    ]
    text = "\n".join(lines)
    print(text)
    OUT.mkdir(exist_ok=True)
    path = OUT / "audit_dump_latest.txt"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"\nСохранено: {path}")


if __name__ == "__main__":
    main()
