# Decision Log — Liquidity Raid Hunter

## 28.08.2026 — DUMP hyp2 BT60d: оси Клода на close t закрыты

**Результат** `bt_DUMP_HYP2_2026-08-28_0755`:

| mode | N | WR | AvgR | OOS | 1-bar | funnel |
|---|---|---|---|---|---|---|
| base | 28 | 35.7% | +0.60 | −0.27 N=7 | 9 (69%) | TAKE=28 |
| skip_asia | 16 | 43.8% | +1.03 | −0.40 WR0% N=3 | 4 (80%) | ASIA=12 |
| pump_clean | 0 | — | — | — | — | SAW=28 |
| pump_clean1 | 0 | — | — | — | — | SAW=28 |
| volpump1 | 17 | 35.3% | +0.46 | −1.00 WR0% N=5 | 5 (56%) | QUIET=11 |
| volpump08 | 22 | 31.8% | +0.39 | −1.00 WR0% N=5 | 8 (67%) | QUIET=6 |
| sweep15 | 25 | 36.0% | +0.66 | −0.64 WR0% N=5 | 8 (73%) | SHALLOW=3 |

**Решение:** в live не переносить. Семья фильтров на close t закрыта (hyp1+hyp2).

**Почему:**
- skip_asia красивее на FULL за счёт вырезания 12 сделок, которые в сумме ≈ +0.3R (~flat). 1-bar среди оставшихся стопов хуже (80%). OOS N=3 WR 0%.
- pump_clean несовместим с вселенной: у всех 28 дампов ≥2 красных в окне пампа.
- vol_vs_pump режет край и убивает OOS (−1.00R). Климакс на дампе vs пик пампа — не тот сетап.
- sweep 1.5% = тот же haircut на 3 сделки, что deep 0.40%.

**Дальше:** split-entry 40/60 — последняя гипотеза Клода, уже не фильтр, а размер. `scanner/backtest_dump_split.py`.

**Статус:** принято. Live v0.2b не меняем.

---

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

**Статус:** принято.

---

## 27.08.2026 — DUMP confirm-gate (lab only)

**Решение:** гейт подтверждения только LAB. Live не трогаем.

**Статус:** lab only.
