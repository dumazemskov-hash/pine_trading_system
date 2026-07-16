"""
Лёгкий оркестратор для multi-agent системы Liquidity Raid Hunter
(Безопасная версия с переменной окружения)
"""

import os
import requests
from datetime import datetime

MEMORY_BANK = "memory-bank"

# === OpenRouter настройки ===
LLM_API_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_API_KEY = os.getenv("OPENROUTER_API_KEY")        # ← Ключ берётся из переменной окружения
LLM_MODEL = "openai/gpt-4o-mini"

def log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

def read_file(filename: str) -> str:
    path = os.path.join(MEMORY_BANK, filename)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def append_to_file(filename: str, content: str):
    path = os.path.join(MEMORY_BANK, filename)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n\n### [{timestamp}]\n{content.strip()}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    log(f"Добавлена запись в {filename}")

def call_llm(system_prompt: str, user_prompt: str) -> str:
    if not LLM_API_KEY:
        return "[ERROR] OPENROUTER_API_KEY не задан"

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/dumazemskov-hash/pine_trading_system",
        "X-Title": "Liquidity Raid Hunter Orchestrator"
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.4
    }
    try:
        response = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"Ошибка LLM: {e}")
        return f"[ERROR] {e}"

# ============================================
# Основной цикл
# ============================================

def run_cycle():
    log("=== Новый цикл ===")

    active_context = read_file("activeContext.md")
    progress = read_file("Progress.md")
    agents_md = read_file("Agents.md")

    # === 1. Orchestrator ===
    log("Запуск Orchestrator...")
    orchestrator_output = call_llm(
        system_prompt="Ты — Orchestrator проекта Liquidity Raid Hunter.",
        user_prompt=f"""Текущий контекст:
{active_context}

Текущий прогресс:
{progress}

Роли агентов:
{agents_md}

Дай чёткие задачи для CodeCritic и Improver на этот цикл."""
    )
    append_to_file("last_orchestrator.md", orchestrator_output)
    log("Orchestrator завершён")

    # === 2. CodeCritic ===
    log("Запуск CodeCritic...")
    critic_output = call_llm(
        system_prompt="Ты — жёсткий и детальный критик кода и архитектуры.",
        user_prompt=f"""Ты — CodeCritic.
{agents_md}

Текущий контекст:
{active_context}

Результат Orchestrator:
{orchestrator_output}

Найди самые важные проблемы в коде и логике. Запиши их структурировано."""
    )
    append_to_file("CritiqueLog.md", critic_output)
    log("CodeCritic завершён")

    # === 3. Improver ===
    log("Запуск Improver...")
    improver_output = call_llm(
        system_prompt="Ты — практичный генератор улучшений.",
        user_prompt=f"""Ты — Improver.
{agents_md}

Текущий контекст:
{active_context}

Критика от CodeCritic:
{critic_output}

Предложи конкретные, реалистичные улучшения. Оцени сложность и пользу."""
    )
    append_to_file("IdeaLog.md", improver_output)
    log("Improver завершён")

    log("=== Цикл полностью завершён ===")

if __name__ == "__main__":
    run_cycle()
