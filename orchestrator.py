"""
Лёгкий оркестратор для multi-agent системы Liquidity Raid Hunter
"""

import os
import requests
from datetime import datetime

MEMORY_BANK = "memory-bank"

# === Настройки LLM ===
LLM_API_URL = "https://api.openai.com/v1/chat/completions"
LLM_API_KEY = "sk-..."          # <-- Вставь свой ключ
LLM_MODEL = "gpt-4o-mini"

def log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

def read_file(filename: str) -> str:
    path = os.path.join(MEMORY_BANK, filename)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(filename: str, content: str):
    path = os.path.join(MEMORY_BANK, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"Записан файл: {filename}")

def call_llm(system_prompt: str, user_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
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
    write_file("last_orchestrator.md", orchestrator_output)
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
    write_file("last_critique.md", critic_output)
    log("CodeCritic завершён")

    # === 3. Improver ===
    log("Запуск Improver...")
    improver_output = call_llm(
        system_prompt="Ты — практичный генератор улучшений. Предлагай реалистичные решения.",
        user_prompt=f"""Ты — Improver.
{agents_md}

Текущий контекст:
{active_context}

Критика от CodeCritic:
{critic_output}

На основе критики предложи конкретные, реалистичные улучшения. 
Оцени сложность и пользу каждого предложения."""
    )
    write_file("last_improver.md", improver_output)
    log("Improver завершён")

    log("=== Цикл полностью завершён ===")

if __name__ == "__main__":
    run_cycle()
