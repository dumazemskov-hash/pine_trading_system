#!/usr/bin/env python3
"""VAL-FADE only. DUMP/LAB frozen in git, not on this desk."""

import os, sys, subprocess, threading, queue, shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
SCANNER_DIR = ROOT / "scanner"
SIGNALS_VAL_DIR = ROOT / "signals_val"
PAPER_DIR = ROOT / "paper"
BACKTESTS_DIR = ROOT / "backtests"

VAL_SCRIPT = SCANNER_DIR / "val_scanner.py"
VAL_PAPER = SCANNER_DIR / "val_paper.py"
VAL_PAPER_FILE = PAPER_DIR / "val_latest.txt"


def find_git():
    g = shutil.which("git")
    if g:
        return g
    for c in [r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"]:
        if Path(c).exists():
            return c
    return "git"


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VAL-FADE — Control")
        self.geometry("860x620")
        self.minsize(720, 480)
        self.configure(bg="#1e1e1e")
        self.val_proc = None
        self.log_queue = queue.Queue()
        self._build_ui()
        self.after(100, self._poll_log)
        self._log(f"Панель VAL | {ROOT}")
        self._log("DUMP заморожен в git — не запускаем")
        self._log(f"VAL: {VAL_SCRIPT.name}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=6, font=("Segoe UI", 9))
        style.configure("TLabel", background="#1e1e1e", foreground="#ddd", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground="#f9a8d4")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#aaa")
        ttk.Label(self, text="VAL-FADE", style="Header.TLabel").pack(pady=(10, 4))
        st = ttk.Frame(self)
        st.pack(fill="x", padx=12)
        self.val_status = tk.StringVar(value="VAL: стоп")
        ttk.Label(st, textvariable=self.val_status, style="Status.TLabel").pack(side="left", padx=8)
        ttk.Label(st, text="DUMP off", style="Status.TLabel").pack(side="left", padx=8)
        val_col = tk.Frame(self, bg="#3a1520", padx=10, pady=10)
        val_col.pack(fill="x", padx=12, pady=8)
        tk.Label(val_col, text="● VAL-FADE  ·  Bybit 15m  ·  1%", bg="#3a1520", fg="#f9a8d4",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        row = tk.Frame(val_col, bg="#3a1520")
        row.pack(fill="x")
        for text, cmd in [
            ("▶ Старт VAL", self.cmd_start_val),
            ("■ Стоп VAL", self.cmd_stop_val),
            ("Paper VAL", self.cmd_paper_val),
            ("Push логов VAL", self.cmd_push_val_logs),
        ]:
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=3)
        shared = ttk.Frame(self)
        shared.pack(fill="x", padx=12, pady=(4, 4))
        for text, cmd in [
            ("Git Pull", self.cmd_git_pull),
            ("Push all", self.cmd_push_all),
            ("signals_val", self.cmd_open_val_signals),
            ("Статус", self.cmd_refresh),
        ]:
            ttk.Button(shared, text=text, command=cmd).pack(side="left", padx=4)
        ttk.Label(self, text="Лог", style="Status.TLabel").pack(anchor="w", padx=16)
        self.log_box = scrolledtext.ScrolledText(
            self, height=18, bg="#121212", fg="#d4d4d4",
            insertbackground="#fff", font=("Consolas", 9), relief="flat")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self.log_box.bind("<Control-c>", self._copy_log)
        self.log_box.bind("<Control-a>", self._select_all)
        self._menu = tk.Menu(self.log_box, tearoff=0)
        self._menu.add_command(label="Копировать", command=self._copy_log)
        self._menu.add_command(label="Выделить всё", command=self._select_all)
        self._menu.add_command(label="Копировать весь лог", command=self._copy_all)
        self.log_box.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

    def _log(self, msg):
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _poll_log(self):
        try:
            while True:
                self.log_box.insert("end", self.log_queue.get_nowait() + "\n")
                self.log_box.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _run_async(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _env(self):
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin", env.get("PATH", "")])
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run_cmd(self, args, cwd=None):
        cwd = cwd or str(ROOT)
        if args and args[0] == "git":
            args = [find_git()] + list(args[1:])
        self._log("$ " + " ".join(str(a) for a in args))
        try:
            p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120, env=self._env())
            if p.stdout:
                for line in p.stdout.strip().splitlines()[-40:]:
                    self._log(line)
            if p.stderr and p.returncode != 0:
                for line in p.stderr.strip().splitlines()[-15:]:
                    self._log("ERR: " + line)
            self._log("OK" if p.returncode == 0 else f"exit {p.returncode}")
            return p.returncode == 0
        except Exception as e:
            self._log(f"EXC: {e}")
            return False

    def _copy_log(self, event=None):
        try:
            text = self.log_box.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear(); self.clipboard_append(text); self.update()
        return "break"

    def _select_all(self, event=None):
        self.log_box.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _copy_all(self):
        self.clipboard_clear(); self.clipboard_append(self.log_box.get("1.0", "end-1c")); self.update()

    def _open_dir(self, path):
        path = Path(path); path.mkdir(exist_ok=True)
        self._log(f"Открываю {path}")
        try:
            os.startfile(str(path)) if sys.platform.startswith("win") else subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(str(e))

    def cmd_start_val(self):
        if self.val_proc and self.val_proc.poll() is None:
            self._log("VAL уже запущен"); return
        if not VAL_SCRIPT.exists():
            messagebox.showerror("Ошибка", f"Нет файла:\n{VAL_SCRIPT}\ngit pull"); return
        def job():
            self._log(f"--- старт {VAL_SCRIPT.name} ---")
            try:
                p = subprocess.Popen([sys.executable, "-u", str(VAL_SCRIPT)], cwd=str(SCANNER_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1, env=self._env())
                self.val_proc = p
                self.val_status.set("VAL: РАБОТАЕТ")
                for line in p.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                self.val_status.set("VAL: стоп")
                self._log("VAL завершился")
            except Exception as e:
                self._log(f"fail: {e}"); self.val_status.set("VAL: ошибка")
        self._run_async(job)

    def cmd_stop_val(self):
        if self.val_proc and self.val_proc.poll() is None:
            self.val_proc.terminate()
            try:
                self.val_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.val_proc.kill()
            self.val_status.set("VAL: стоп"); self._log("VAL остановлен")
        else:
            self._log("VAL не запущен")

    def cmd_open_val_signals(self):
        self._open_dir(SIGNALS_VAL_DIR)

    def cmd_push_val_logs(self):
        def job():
            SIGNALS_VAL_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "signals_val", "paper/val_book.jsonl", "paper/val_latest.txt"])
            r = subprocess.run([find_git(), "status", "--porcelain", "signals_val", "paper"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            if not (r.stdout or "").strip():
                self._log("Нет VAL логов"); return
            self._run_cmd(["git", "commit", "-m", f"signals val {datetime.now():%Y-%m-%d %H:%M}"])
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_paper_val(self):
        def job():
            self._log("--- Paper VAL ---")
            PAPER_DIR.mkdir(exist_ok=True)
            if VAL_PAPER.exists():
                self._run_cmd([sys.executable, "-u", str(VAL_PAPER)], cwd=str(SCANNER_DIR))
            else:
                self._log(f"нет {VAL_PAPER}")
        self._run_async(job)

    def cmd_git_pull(self):
        self._run_async(lambda: (self._log("--- git pull ---"), self._run_cmd(["git", "pull"])))

    def cmd_push_all(self):
        def job():
            for d in (BACKTESTS_DIR, SIGNALS_VAL_DIR, PAPER_DIR):
                d.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "-A"])
            self._run_cmd(["git", "add", "-f", "--", "backtests", "signals_val", "paper"])
            r = subprocess.run([find_git(), "status", "--porcelain"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            dirty = (r.stdout or "").strip()
            if dirty:
                for line in dirty.splitlines()[:25]:
                    self._log("  " + line)
                self._run_cmd(["git", "commit", "-m", f"val update {datetime.now():%Y-%m-%d %H:%M}"])
            else:
                self._log("Чисто")
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_refresh(self):
        v_on = self.val_proc and self.val_proc.poll() is None
        self.val_status.set(f"VAL: {'РАБОТАЕТ' if v_on else 'стоп'}")
        self._log(f"Статус: VAL={'ON' if v_on else 'off'}  DUMP=frozen")


if __name__ == "__main__":
    ControlPanel().mainloop()
