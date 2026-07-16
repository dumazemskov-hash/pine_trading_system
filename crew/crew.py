from crewai import Crew, Process
from agents import orchestrator, code_critic, improver
from tasks import analyze_code_task, propose_improvements_task

# Создаём экипаж (Crew)
liquidity_raid_crew = Crew(
    agents=[orchestrator, code_critic, improver],
    tasks=[analyze_code_task, propose_improvements_task],
    process=Process.sequential,   # Задачи выполняются по очереди
    verbose=True
)

# Запуск экипажа
if __name__ == "__main__":
    result = liquidity_raid_crew.kickoff()
    print("\n=== Результат работы экипажа ===\n")
    print(result)
