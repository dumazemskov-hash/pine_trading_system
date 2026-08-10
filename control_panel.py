#!/usr/bin/env python3
"""
RAID Hunter — Control Panel
"""

import os
import sys
import subprocess
import threading
import queue
import shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
SCANNER_DIR = ROOT / "scanner"
SIGNALS_DIR = ROOT / "signals"
SCANNER_SCRIPT = SCANNER_DIR / "v8.32-exp.py"
CHECK_SCRIPT = SCANNER_DIR / "check_signals.py"
BACKTEST_SCRIPT = SCANNER_DIR / "backtest.py"
BACKTESTS_DIR = ROOT / "backtests"

if not SCANNER_SCRIPT.exists():
    SCANNER_SCRIPT = SCANNER_DIR / "scanner.py"


def find_git() -> str:
    g = shutil.which("git")
    if g:
        return g
    for c in [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ]:
        if Path(c).exists():
            return c
    return "git"


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAID Hunter — Control Panel")
        self.geometry("860x580")
        self.minsize(640, 480)
        self.configure(bg="#1e1e1e")
        self.scanner_proc = None
        self.log_queue = queue.Queue()
        self._build_ui()
        self.after(100, self._poll_log)
        self._log(f"Панель запущена | {ROOT}")
        self._log(f"Сканер: {SCANNER_SCRIPT.name}")
        self._log(f"Git: {find_git()}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=8, font=("Segoe UI", 10))
        style.configure("TLabel", background="#1e1e1e", foreground="#ddd", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#7dd3fc")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#aaa")

        ttk.Label(self, text="RAID Hunter Control", style="Header.TLabel").pack(pady=(12, 4))
        self.status_var = tk.StringVar(value="Сканер: остановлен")
        ttk.Label(self, textvariable=self.status_var, style="Status.TLabel").pack()

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=12, padx=16, fill="x")
        buttons = [
            ("Git Pull", self.cmd_git_pull),
            ("Запустить сканер", self.cmd_start_scanner),
            ("Остановить сканер", self.cmd_stop_scanner),
            ("Проверить сигналы", self.cmd_check_signals),
            ("Бэктест", self.cmd_backtest),
            ("Последний BT", self.cmd_show_backtest),
            ("Push логов", self.cmd_push_signals),
            ("Push all", self.cmd_push_all),
            ("Открыть signals", self.cmd_open_signals),
            ("Обновить статус", self.cmd_refresh_status),
        ]
        for i, (text, cmd) in enumerate(buttons):
            ttk.Button(btn_frame, text=text, command=cmd).grid(
                row=i // 4, column=i % 4, padx=6, pady=6, sticky="ew"
            )
            btn_frame.columnconfigure(i % 4, weight=1)

        ttk.Label(self, text="Лог", style="Status.TLabel").pack(anchor="w", padx=16)
        self.log_box = scrolledtext.ScrolledText(
            self, height=18, bg="#121212", fg="#d4d4d4",
            insertbackground="#fff", font=("Consolas", 9), relief="flat", borderwidth=0,
        )
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(4, 12))
        self.log_box.bind("<Control-c>", self._copy_log)
        self.log_box.bind("<Control-C>", self._copy_log)
        self.log_box.bind("<Control-a>", self._select_all_log)
        self.log_box.bind("<Control-A>", self._select_all_log)
        self._log_menu = tk.Menu(self.log_box, tearoff=0)
        self._log_menu.add_command(label="Копировать", command=lambda: self._copy_log())
        self._log_menu.add_command(label="Выделить всё", command=lambda: self._select_all_log())
        self._log_menu.add_separator()
        self._log_menu.add_command(label="Копировать весь лог", command=self._copy_all_log)
        self.log_box.bind("<Button-3>", self._show_log_menu)

    def _log(self, msg: str):
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

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

    def _env(self):
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([
            r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin", env.get("PATH", "")
        ])
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run_cmd(self, args, cwd=None):
        cwd = cwd or str(ROOT)
        if args and args[0] == "git":
            args = [find_git()] + list(args[1:])
        self._log("$ " + " ".join(str(a) for a in args))
        try:
            p = subprocess.run(
                args, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, env=self._env(),
            )
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            if out:
                for line in out.splitlines()[-40:]:
                    self._log(line)
            if err and p.returncode != 0:
                for line in err.splitlines()[-20:]:
                    self._log("ERR: " + line)
            self._log("OK" if p.returncode == 0 else f"exit code {p.returncode}")
            return p.returncode == 0
        except Exception as e:
            self._log(f"EXC: {e}")
            return False

    def _copy_log(self, event=None):
        try:
            text = self.log_box.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        return "break"

    def _select_all_log(self, event=None):
        self.log_box.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _copy_all_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end-1c"))
        self.update()
        self._log("Лог скопирован в буфер")

    def _show_log_menu(self, event):
        try:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._log_menu.grab_release()

    def cmd_git_pull(self):
        self._run_async(lambda: (self._log("--- git pull ---"), self._run_cmd(["git", "pull"])))

    def cmd_start_scanner(self):
        if self.scanner_proc and self.scanner_proc.poll() is None:
            self._log("Сканер уже запущен"); return
        if not SCANNER_SCRIPT.exists():
            messagebox.showerror("Ошибка", f"Нет файла:\n{SCANNER_SCRIPT}"); return

        def job():
            self._log(f"--- старт {SCANNER_SCRIPT.name} ---")
            try:
                self.scanner_proc = subprocess.Popen(
                    [sys.executable, "-u", str(SCANNER_SCRIPT)],
                    cwd=str(SCANNER_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, env=self._env(),
                )
                self.status_var.set(f"Сканер: РАБОТАЕТ ({SCANNER_SCRIPT.name})")
                for line in self.scanner_proc.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                self.status_var.set("Сканер: остановлен")
                self._log("Сканер завершился")
            except Exception as e:
                self._log(f"Не удалось запустить: {e}")
                self.status_var.set("Сканер: ошибка")
        self._run_async(job)

    def cmd_stop_scanner(self):
        if self.scanner_proc and self.scanner_proc.poll() is None:
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
            if not CHECK_SCRIPT.exists():
                self._log("check_signals.py не найден"); return
            self._run_cmd([sys.executable, "-u", str(CHECK_SCRIPT)], cwd=str(SCANNER_DIR))
        self._run_async(job)

    def cmd_backtest(self):
        def job():
            self._log("--- backtest ---")
            if not BACKTEST_SCRIPT.exists():
                self._log(f"Не найден: {BACKTEST_SCRIPT}"); return
            self._log("Запуск backtest.py...")
            try:
                p = subprocess.Popen(
                    [sys.executable, "-u", str(BACKTEST_SCRIPT)],
                    cwd=str(SCANNER_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, env=self._env(),
                )
                for line in p.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                code = p.wait(timeout=1)
                self._log("Бэктест завершён OK" if code == 0 else f"exit {code}")
                if code == 0:
                    self._log("Результат: backtests/latest.txt — жми Push all")
            except Exception as e:
                self._log(f"Ошибка бэктеста: {e}")
        self._run_async(job)

    def cmd_show_backtest(self):
        def job():
            latest = BACKTESTS_DIR / "latest.txt"
            if not latest.exists():
                self._log("Нет backtests/latest.txt"); return
            for line in latest.read_text(encoding="utf-8").splitlines():
                self._log(line)
            self._log(f"(файл: {latest})")
        self._run_async(job)

    def cmd_push_signals(self):
        def job():
            self._log("--- push signals ---")
            SIGNALS_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "signals"])
            r = subprocess.run(
                [find_git(), "status", "--porcelain", "signals"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env(),
            )
            if not (r.stdout or "").strip():
                self._log("Нет новых логов"); return
            self._run_cmd(["git", "commit", "-m", f"signals {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_push_all(self):
        def job():
            self._log("--- push all ---")
            BACKTESTS_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "-A"])
            self._run_cmd(["git", "add", "-f", "--", "backtests"])
            r = subprocess.run(
                [find_git(), "status", "--porcelain"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env(),
            )
            dirty = (r.stdout or "").strip()
            if dirty:
                for line in dirty.splitlines()[:30]:
                    self._log("  " + line)
                self._run_cmd(["git", "commit", "-m", f"update {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
            else:
                self._log("Рабочее дерево чистое")
            self._run_cmd(["git", "push"])
            self._run_cmd(["git", "status", "-sb"])
        self._run_async(job)

    def cmd_open_signals(self):
        SIGNALS_DIR.mkdir(exist_ok=True)
        path = str(SIGNALS_DIR)
        self._log(f"Открываю {path}")
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:
            self._log(f"Не открылось: {e}")

    def cmd_refresh_status(self):
        running = self.scanner_proc and self.scanner_proc.poll() is None
        self.status_var.set(
            f"Сканер: {'РАБОТАЕТ' if running else 'остановлен'} | {SCANNER_SCRIPT.name}"
        )


if __name__ == "__main__":
    ControlPanel().mainloop()
