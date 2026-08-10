# Signal logs

Каждый сигнал сканера пишется в файл `YYYY-MM-DD.jsonl`.

Формат одной строки — JSON с:
- параметрами сигнала (entry, stop, tp, zone_score...)
- последними 40 свечами (OHLCV)
- ATR и данными зоны

После появления сигналов сделай `git add signals && git commit -m "signals" && git push` — тогда я смогу их разобрать без скринов.
