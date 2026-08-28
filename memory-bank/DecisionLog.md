# Decision Log — Liquidity Raid Hunter

## 28.08.2026 — DUMP hyp1 BT60d: фильтры/входы PDF закрыты, в live НЕ переносить

**Результат** `latest_dump_hyp.txt` (150 символов, 60d, live untouched):

| mode | N | WR | AvgR | OOS AvgR | 1-bar | funnel |
|---|---|---|---|---|---|---|
| base | 28 | 35.7% | +0.60 | −0.27 (N=7) | 9/13 (69%) | TAKE=28 |
| prelow | 25 | 32.0% | +0.53 | −0.70 WR0% | 8 (67%) | NOACCEPT=3 |
| deep | 25 | 32.0% | +0.53 | −0.70 WR0% | 8 (67%) | NOACCEPT=1 SHALLOW=2 |
| deep_loose | 25 | 32.0% | +0.53 | −0.70 WR0% | 8 (67%) | NOACCEPT=2 SHALLOW=1 |
| volcap6 | 25 | 32.0% | +0.53 | −0.15 | 8 (67%) | VOLCAP=3 |
| volcap8 | 25 | 32.0% | +0.53 | −0.15 | 8 (67%) | VOLCAP=3 |
| retest | 8 | 0.0% | −0.55 | −1.00 | 3 (50%) | NOFILL=20 stop 6.5% |
| break | 15 | 26.7% | +0.14 | +0.80 N=3 | 1 (12%) | NOFILL=11 stop 11% IS −0.03R |

**Решение:** ни один вариант не в live. Семья prelow/deep/volcap/retest/break закрыта.

**Почему:**
- prelow / deep / deep_loose / volcap6 / volcap8 срезали одни и те же ~3 сделки. 1-bar остался ~67%. OOS у prelow/deep хуже базы (WR 0%, −0.70R).
- volcap чуть мягче по OOS (−0.15 vs −0.27), но IS тоже хуже (+0.74 vs +0.89) — отрезал победителей, не стопы.
- retest не заполняется (20/28 NOFILL), WR 0%.
- break режет 1-bar ценой стопа 11% и отрицательным IS. OOS N=3 — шум.

**Дальше:** hyp2 — оси Клода, которых не было в этом прогоне: skip_asia, pump_clean, vol_vs_pump, sweep 1.5%. Split-entry не открываем, пока не закрыты дешёвые фильтры.

**Статус:** принято. Live `dump_scanner.py` v0.2b не меняем.

---

## 28.08.2026 — DUMP failed-reclaim BT60d: в live НЕ переносить

**Результат** reclaim 60d:
- base N=28 +0.60R OOS −0.27R
- rec_open TAKE=2/28 (SWEPT8 TIMEOUT11 MISS7) −0.10R
- rec_close N=17 +0.14R OOS −0.18R ≈ gate_f
- rec_open_dh TAKE=2 −1.00R

**Решение:** reclaim-семья закрыта. Не промоутить.

---

## 27.08.2026 — DUMP confirm-gate BT60d: в live НЕ переносить

**Результат** `bt_DUMP_GATE_2026-08-27_1107`:

| mode | N | WR | AvgR | Final | DD | 1-bar | OOS AvgR |
|---|---|---|---|---|---|---|---|
| base | 26 | 34.6% | +0.67 | $418 (+39%) | 7.8% | 9 (82%) | −0.01 (N=7) |
| gate_opt | 17 | 52.9% | +1.55 | $501 (+67%) | 2.0% | 0 | +0.72 (N=4) |
| gate_f | 17 | 23.5% | +0.19 | $318 (+6%) | 7.1% | 2 | +0.07 (N=4) |
| gate_new | 17 | 17.6% | −0.16 | $282 (−6%) | 9.6% | 8 | −1.00 (N=4) |

**Решение:** live `dump_scanner.py` v0.2b не меняем. Гейт остаётся lab.

**Почему:**
- `gate_opt` — look-ahead. Вход по close сигнала после знания bar+1 вживую недоступен (шорт уже ушёл ниже).
- Честный `gate_f` (вход close1, стоп orig) раздувает стоп до ~6.4%, WR падает 53%→23%, край +0.19R. OOS N=4 — шум.
- `gate_new` убивает край. 1-bar стопы возвращаются.
- base OOS уже плоский (−0.01R). Оптимистичный paper +5.7R = тот же look-ahead, что gate_opt.

**Статус:** принято. LAB-сканер с pending можно оставить для наблюдения, не для исполнения.

---

## 27.08.2026 — DUMP confirm-gate (lab only)

**Решение:**  
Гейт подтверждения: отмена сделки, если high(bar+1) ≥ original stop (3% от close сигнала).

**Где:** только LAB.
- `scanner/dump_scanner_lab.py` — pending → confirm/skip. Live `dump_scanner.py` не трогаем.
- `scanner/backtest_dump_gate.py` — 60d IS/OOS: base / gate_opt / gate_f / gate_new
- `scanner/paper_gate_replay.py` — те же правила на 22 paper-сделках

**Fill:**
- gate_opt — исходные уровни (оптимистичный, знаем bar+1 после входа)
- gate_f — честный: entry=close1, stop=orig, TP rescale
- gate_new — честный: entry=close1, stop=3% новый

**Статус:** lab only. В live не переносить (см. запись выше после BT60d).

---

## 10.08.2026 — Точечное ослабление → v8.27

**Решение:**  
На базе v8.26 сделаны два точечных изменения:
1. MIN_ZONE_SCORE: 3 → **2**
2. BREAK_ATR_MULT: 0.40 → **0.25**

**Причина:**  
v8.26 с score≥3 и глубоким пробоем давал слишком мало сигналов. Нужно проверить, даст ли ослабление больше качественных входов без сильного падения winrate.

**Статус:** Внедрено в Scanner v8.27 + Indicator v8.27
