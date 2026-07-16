"""
Лёгкий оркестратор для multi-agent системы Liquidity Raid Hunter
"""

import os
import requests
from datetime import datetime

MEMORY_BANK = "memory-bank"

# === Настройки LLM ===
# Пока используем OpenAI-совместимый API.
# Позже можно поменять на Grok / Claude / локальную модель.
LLM_API_URL = "https://api.openai.com/v1/chat/completions"
LLM_API_KEY = "sk-..."          # <-- Сюда нужно будет вставить свой ключ
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
    """Вызов LLM через OpenAI-совместимый API"""
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
        log(f"Ошибка вызова LLM: {e}")
        return f"[ERROR] Не удалось получить ответ от LLM: {e}"

# ============================================
# Основной цикл
# ============================================

def run_cycle():
    log("=== Запуск нового цикла ===")

    active_context = read_file("activeContext.md")
    progress = read_file("Progress.md")
    agents = read_file("Agents.md")

    log("Прочитаны файлы памяти")

    # === Шаг 1: Orchestrator ===
    log("Запуск Orchestrator...")
    orchestrator_prompt = f"""Ты — Orchestrator.
{agents}

Текущий контекст:
{active_context}

Текущий прогресс:
{progress}

Твоя задача: проанализировать ситуацию и дать чёткие задачи для CodeCritic и Improver на следующий цикл.
"""
    orchestrator_response = call_llm(
        system_prompt="Ты — опытный Team Lead проекта Liquidity Raid Hunter.",
        user_prompt=orchestrator_prompt
    )
    write_file("Orchestrator_Response.md", orchestrator_response)
    log("Orchestrator завершил работу")

    # TODO: Добавить CodeCritic и Improver

    log("=== Цикл завершён ===")

if __name__ == "__main__":
    run_cycle()
