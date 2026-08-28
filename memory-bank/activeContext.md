# Активный контекст — Liquidity Raid Hunter

## Текущий фокус (28.08.2026)

Live / paper: **DUMP v0.2b** собирает сигналы, параметры заморожены.

Новый lab (вторая система, не фильтр DUMP): **VAL-FADE**
- `python scanner/backtest_val_fade.py`
- `backtests/latest_val_fade.txt`
- окно 8/16 × стоп tight/struct, риск 1%

Закрыто без промоута по DUMP: gate, reclaim, hyp1, hyp2, split, TP3.

## Живые параметры DUMP v0.2b

- body 6.5–9%, cpos≤0.35, vol≥3×MA20, pump window, sweep low
- stop cap 3%, TP 1.6/3R, risk 2%, BE after TP1

## Правила

- Live DUMP не переписывать
- GitHub ветка dump = истина
- VAL-FADE = lab only, отдельный сканер пока не пишем
