# Активный контекст — Liquidity Raid Hunter

## Текущий фокус (27.08.2026)

Основная стратегия: **DUMP v0.2b** (живой сканер не трогаем).

Lab: confirm-gate «отмена если high(bar+1) ≥ orig stop».
- LAB scanner пишет pending/confirmed/skip_bounce в `signals_dump_lab/`
- BT: `python scanner/backtest_dump_gate.py` → `backtests/latest_dump_gate.txt`
- Paper replay: `python scanner/paper_gate_replay.py`

Paper live v0.2b на 27.08: 22 сделки, $296.91 (−1.0%), 14 STOP / 5 TP / 3 BE.
Оптимистичный гейт на тех же 22: take 13, +5.7R, ~$334 (+11.5%).

## Живые параметры DUMP v0.2b

- body 6.5–9%, cpos≤0.35, vol≥3×MA20, pump window, sweep low
- stop cap 3%, TP 1.6/3R, risk 2%, BE after TP1
- cooldown 32, stop-cooldown 96

## Правила

- Live DUMP не переписывать без 60d OOS по гейту
- GitHub ветка dump = истина
- RAID / RAID2 / DUMP-STRUCT = lab only
