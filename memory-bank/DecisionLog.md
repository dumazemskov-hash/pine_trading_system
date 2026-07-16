# Decision Log — Liquidity Raid Hunter

## 16.07.2026
- Принято решение использовать **CrewAI** как основную платформу для multi-agent системы.
- Выбран состав из **3 агентов**: Orchestrator, CodeCritic, Improver.
- Главный приоритет — глубокий анализ и улучшение существующего кода (сканер + индикатор).
- Структура `memory-bank` зафиксирована (productContext, activeContext, DecisionLog, CritiqueLog, IdeaLog, Progress).
