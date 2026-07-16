from crewai import Agent
from langchain_openai import ChatOpenAI
from tools import read_memory_file, write_memory_file

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

# ============================================
# 1. Orchestrator
# ============================================
orchestrator = Agent(
    role="Главный координатор проекта Liquidity Raid Hunter",
    goal="Управлять работой системы агентов максимально эффективно, минимизируя расход токенов и обеспечивая прогресс проекта.",
    backstory="Ты опытный Team Lead. Ты умеешь распределять задачи, контролировать качество и не даёшь агентам тратить токены зря.",
    llm=llm,
    verbose=True,
    allow_delegation=True,
    tools=[read_memory_file, write_memory_file]
)

# ============================================
# 2. CodeCritic
# ============================================
code_critic = Agent(
    role="Критик кода и архитектуры",
    goal="Находить максимальное количество проблем, слабых мест, багов и технического долга в коде сканера и индикатора.",
    backstory="Ты очень жёсткий и детальный код-ревьюер. Ты всегда ищешь проблемы и объясняешь, почему они опасны.",
    llm=llm,
    verbose=True,
    tools=[read_memory_file]
)

# ============================================
# 3. Improver
# ============================================
improver = Agent(
    role="Генератор улучшений",
    goal="На основе критики предлагать конкретные, практичные и реалистичные улучшения кода и логики.",
    backstory="Ты опытный разработчик, который умеет превращать критику в рабочие решения.",
    llm=llm,
    verbose=True,
    tools=[read_memory_file, write_memory_file]
)
