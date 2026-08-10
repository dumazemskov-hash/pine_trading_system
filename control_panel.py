#!/usr/bin/env python3
"""
RAID Hunter — Control Panel
Минималистичная панель управления сканером и служебными командами.
"""

import os
import sys
import subprocess
import threading
import queue
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
SCANNER_DIR = ROOT / "scanner"
SIGNALS_DIR = ROOT / "signals"
SCANNER_SCRIPT = SCANNER_DIR / "v8.30-exp.py"
CHECK_SCRIPT = SCANNER_DIR / "check_signals.py"

if not SCANNER_SCRIPT.exists():
    SCANNER_SCRIPT = SCANNER_DIR / "scanner.py"


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAID Hunter — Control Panel")
        self.geometry("780x560")
        self.minsize(640, 480)
        self.configure(bg="#1e1e1e")

        self.scanner_proc = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self.after(100, self._poll_log)
        self._log(f"Панель запущена | {ROOT}")
        self._log(f"Сканер: {SCANNER_SCRIPT.name}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=8, font=("Segoe UI", 10))
        style.configure("TLabel", background="#1e1e1e", foreground="#ddd", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#7dd3fc")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#aaa")

        header = ttk.Label(self, text="RAID Hunter Control", style="Header.TLabel")
        header.pack(pady=(12, 4))

        self.status_var = tk.StringVar(value="Сканер: остановлен")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel").pack()

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=12, padx=16, fill="x")

        buttons = [
            ("Git Pull", self.cmd_git_pull),
            ("Запустить сканер", self.cmd_start_scanner),
            ("Остановить сканер", self.cmd_stop_scanner),
            ("Проверить сигналы", self.cmd_check_signals),
            ("Push логов", self.cmd_push_signals),
            ("Открыть signals", self.cmd_open_signals),
            ("Обновить статус", self.cmd_refresh_status),
        ]

        for i, (text, cmd) in enumerate(buttons):
            b = ttk.Button(btn_frame, text=text, command=cmd)
            b.grid(row=i // 4, column=i % 4, padx=6, pady=6, sticky="ew")
            btn_frame.columnconfigure(i % 4, weight=1)

        log_label = ttk.Label(self, text="Лог", style="Status.TLabel")
        log_label.pack(anchor="w", padx=16)

        self.log_box = scrolledtext.ScrolledText(
            self,
            height=18,
            bg="#121212",
            fg="#d4d4d4",
            insertbackground="#fff",
            font=("Consolas", 9),
            relief="flat",
            borderwidth=0,
        )
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(4, 12))

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _poll_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _run_async(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _run_cmd(self, args, cwd=None, shell=False):
        cwd = cwd or str(ROOT)
        self._log(f"$ {' '.join(args) if isinstance(args, list) else args}")
        try:
            p = subprocess.run(
                args,
                cwd=cwd,
                shell=shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            if out:
                for line in out.splitlines()[-30:]:
                    self._log(line)
            if err and p.returncode != 0:
                for line in err.splitlines()[-15:]:
                    self._log("ERR: " + line)
            if p.returncode == 0:
                self._log("OK")
            else:
                self._log(f"exit code {p.returncode}")
            return p.returncode == 0
        except subprocess.TimeoutExpired:
            self._log("TIMEOUT")
            return False
        except Exception as e:
            self._log(f"EXC: {e}")
            return False

    def cmd_git_pull(self):
        def job():
            self._log("--- git pull ---")
            self._run_cmd(["git", "pull"], cwd=str(ROOT))
        self._run_async(job)

    def cmd_start_scanner(self):
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self._log("Сканер уже запущен")
            return

        if not SCANNER_SCRIPT.exists():
            self._log(f"Не найден: {SCANNER_SCRIPT}")
            messagebox.showerror("Ошибка", f"Нет файла:\n{SCANNER_SCRIPT}")
            return

        def job():
            self._log(f"--- старт {SCANNER_SCRIPT.name} ---")
            try:
                self.scanner_proc = subprocess.Popen(
                    [sys.executable, str(SCANNER_SCRIPT)],
                    cwd=str(SCANNER_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                self.status_var.set(f"Сканер: РАБОТАЕТ ({SCANNER_SCRIPT.name})")
                self._log("Процесс запущен")
                assert self.scanner_proc.stdout is not None
                for line in self.scanner_proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._log(line)
                self.status_var.set("Сканер: остановлен")
                self._log("Сканер завершился")
            except Exception as e:
                self._log(f"Не удалось запустить: {e}")
                self.status_var.set("Сканер: ошибка")

        self._run_async(job)

    def cmd_stop_scanner(self):
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self._log("Останавливаю сканер...")
            self.scanner_proc.terminate()
            try:
                self.scanner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.scanner_proc.kill()
            self.status_var.set("Сканер: остановлен")
            self._log("Сканер остановлен")
        else:
            self._log("Сканер не запущен")

    def cmd_check_signals(self):
        def job():
            self._log("--- check_signals ---")
            script = CHECK_SCRIPT
            if not script.exists():
                self._log("check_signals.py не найден — положи его в scanner/")
                return
            self._run_cmd([sys.executable, str(script)], cwd=str(SCANNER_DIR))
        self._run_async(job)

    def cmd_push_signals(self):
        def job():
            self._log("--- push signals ---")
            SIGNALS_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "signals"], cwd=str(ROOT))
            r = subprocess.run(
                ["git", "status", "--porcelain", "signals"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            if not r.stdout.strip():
                self._log("Нет новых логов для пуша")
                return
            msg = f"signals {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            self._run_cmd(["git", "commit", "-m", msg], cwd=str(ROOT))
            self._run_cmd(["git", "push"], cwd=str(ROOT))
        self._run_async(job)

    def cmd_open_signals(self):
        SIGNALS_DIR.mkdir(exist_ok=True)
        path = str(SIGNALS_DIR)
        self._log(f"Открываю {path}")
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:
            self._log(f"Не открылось: {e}")

    def cmd_refresh_status(self):
        running = self.scanner_proc and self.scanner_proc.poll() is None
        self.status_var.set(
            f"Сканер: {'РАБОТАЕТ' if running else 'остановлен'} | {SCANNER_SCRIPT.name}"
        )
        if SIGNALS_DIR.exists():
            files = sorted(SIGNALS_DIR.glob("*.jsonl"))
            if files:
                last = files[-1]
                try:
                    lines = last.read_text(encoding="utf-8").strip().splitlines()
                    self._log(f"Лог {last.name}: {len(lines)} записей")
                except Exception:
                    self._log(f"Последний лог: {last.name}")
            else:
                self._log("Сигналов в signals/ пока нет")
        else:
            self._log("Папка signals/ ещё не создана")


if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()
