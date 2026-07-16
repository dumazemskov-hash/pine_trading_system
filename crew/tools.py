from crewai_tools import tool
import os

@tool("Read Memory File")
def read_memory_file(file_name: str) -> str:
    """Читает содержимое файла из папки memory-bank."""
    base_path = "memory-bank"
    file_path = os.path.join(base_path, file_name)
    
    if not os.path.exists(file_path):
        return f"Файл {file_name} не найден."
    
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


@tool("Write Memory File")
def write_memory_file(file_name: str, content: str) -> str:
    """Записывает содержимое в файл в папке memory-bank."""
    base_path = "memory-bank"
    file_path = os.path.join(base_path, file_name)
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    return f"Файл {file_name} успешно обновлён."
