# Активный контекст — Liquidity Raid Hunter

## Текущий фокус (28.08.2026)

Основная стратегия: **DUMP v0.2b** (живой сканер не трогаем).

Закрыто без промоута:
- confirm-gate (opt = lookahead, honest fill убивает край)
- failed-reclaim (rec_open не заполняется, rec_close ≈ gate_f)
- hyp1 PDF: prelow / deep / volcap / retest / break
- hyp2 Claude-on-t: skip_asia / pump_clean / vol_vs_pump / sweep15

Сейчас lab: **split-entry** (размер, не фильтр).
- `python scanner/backtest_dump_split.py`
- base / split4060 / split5050 / leg1only40
- отчёт: `backtests/latest_dump_split.txt`

## Живые параметры DUMP v0.2b

- body 6.5–9%, cpos≤0.35, vol≥3×MA20, pump window, sweep low
- stop cap 3%, TP 1.6/3R, risk 2%, BE after TP1
- cooldown 32, stop-cooldown 96

## Правила

- Live DUMP не переписывать без честного 60d OOS
- GitHub ветка dump = истина
- RAID / RAID2 / DUMP-STRUCT = lab only
