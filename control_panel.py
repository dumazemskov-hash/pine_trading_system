#!/usr/bin/env python3
"""RAID / DUMP Control Panel — компактно. DUMP = основная."""

import os, sys, subprocess, threading, queue, shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
SCANNER_DIR = ROOT / "scanner"
SIGNALS_DIR = ROOT / "signals"
SIGNALS_DUMP_DIR = ROOT / "signals_dump"
BACKTESTS_DIR = ROOT / "backtests"
PAPER_DIR = ROOT / "paper"

RAID_SCRIPT = SCANNER_DIR / "v8.32-exp.py"
DUMP_SCRIPT = SCANNER_DIR / "dump_scanner.py"
DUMP_BT = SCANNER_DIR / "backtest_dump_filters.py"  # lab: close/trend/BTC
DUMP_BT_FALLBACK = SCANNER_DIR / "backtest_dump.py"
CHECK_SCRIPT = SCANNER_DIR / "check_signals.py"
PAPER_SCRIPT = SCANNER_DIR / "paper_engine.py"
if not RAID_SCRIPT.exists():
    RAID_SCRIPT = SCANNER_DIR / "scanner.py"


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
        self.title("DUMP / RAID — Control")
        self.geometry("780x640")
        self.minsize(640, 480)
        self.configure(bg="#1e1e1e")
        self.raid_proc = None
        self.dump_proc = None
        self.log_queue = queue.Queue()
        self._build_ui()
        self.after(100, self._poll_log)
        self._log(f"Панель | {ROOT}")
        self._log(f"DUMP: {DUMP_SCRIPT.name} (основная) | RAID: {RAID_SCRIPT.name}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=5, font=("Segoe UI", 9))
        style.configure("TLabel", background="#1e1e1e", foreground="#ddd")
        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground="#7dd3fc")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#aaa")

        ttk.Label(self, text="DUMP Control  ·  RAID secondary", style="Header.TLabel").pack(pady=(10, 2))
        st = ttk.Frame(self)
        st.pack(fill="x", padx=12)
        self.dump_status = tk.StringVar(value="DUMP: стоп")
        self.raid_status = tk.StringVar(value="RAID: стоп")
        ttk.Label(st, textvariable=self.dump_status, style="Status.TLabel").pack(side="left", padx=8)
        ttk.Label(st, textvariable=self.raid_status, style="Status.TLabel").pack(side="left", padx=8)

        cols = ttk.Frame(self)
        cols.pack(fill="x", padx=12, pady=8)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)

        # DUMP — primary, short list
        right = tk.Frame(cols, bg="#2e2a1a", padx=8, pady=8)
        right.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(right, text="● DUMP v0.2b  (основная)", bg="#2e2a1a", fg="#fbbf24",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        for text, cmd in [
            ("▶ Старт DUMP", self.cmd_start_dump),
            ("■ Стоп DUMP", self.cmd_stop_dump),
            ("BT filters (close/BTC)", self.cmd_bt_dump),
            ("Последний BT", self.cmd_show_dump_bt),
            ("Push логов DUMP", self.cmd_push_dump_logs),
        ]:
            ttk.Button(right, text=text, command=cmd).pack(fill="x", pady=2)

        # RAID — minimal
        left = tk.Frame(cols, bg="#1a2e1a", padx=8, pady=8)
        left.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(left, text="○ RAID  (наблюдение)", bg="#1a2e1a", fg="#86efac",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        for text, cmd in [
            ("▶ Старт RAID", self.cmd_start_raid),
            ("■ Стоп RAID", self.cmd_stop_raid),
        ]:
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)

        # shared row
        shared = ttk.Frame(self)
        shared.pack(fill="x", padx=12, pady=4)
        for text, cmd in [
            ("Git Pull", self.cmd_git_pull),
            ("Push all", self.cmd_push_all),
            ("Проверить сигналы", self.cmd_check_signals),
            ("📄 Paper", self.cmd_paper),
            ("signals_dump", self.cmd_open_dump_signals),
            ("Статус", self.cmd_refresh),
        ]:
            ttk.Button(shared, text=text, command=cmd).pack(side="left", padx=3)

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
        env["PATH"] = os.pathsep.join([
            r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin", env.get("PATH", "")])
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
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        return "break"

    def _select_all(self, event=None):
        self.log_box.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end-1c"))
        self.update()

    def _start_proc(self, script, label, attr, status_var):
        proc = getattr(self, attr)
        if proc and proc.poll() is None:
            self._log(f"{label} уже запущен")
            return
        if not script.exists():
            messagebox.showerror("Ошибка", f"Нет:\n{script}")
            return

        def job():
            self._log(f"--- старт {script.name} ---")
            try:
                p = subprocess.Popen(
                    [sys.executable, "-u", str(script)], cwd=str(SCANNER_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1, env=self._env())
                setattr(self, attr, p)
                status_var.set(f"{label}: РАБОТАЕТ")
                for line in p.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                status_var.set(f"{label}: стоп")
                self._log(f"{label} завершился")
            except Exception as e:
                self._log(f"fail: {e}")
                status_var.set(f"{label}: ошибка")

        self._run_async(job)

    def _stop_proc(self, attr, label, status_var):
        proc = getattr(self, attr)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            status_var.set(f"{label}: стоп")
            self._log(f"{label} остановлен")
        else:
            self._log(f"{label} не запущен")

    def _run_bt(self, script, name):
        def job():
            self._log(f"--- {name} ---")
            if not script.exists():
                self._log(f"Нет: {script} — git pull")
                return
            try:
                p = subprocess.Popen(
                    [sys.executable, "-u", str(script)], cwd=str(SCANNER_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1, env=self._env())
                for line in p.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                code = p.wait()
                self._log(f"{name} OK" if code == 0 else f"exit {code}")
            except Exception as e:
                self._log(f"BT error: {e}")

        self._run_async(job)

    def _show_file(self, path):
        def job():
            if not path.exists():
                self._log(f"Нет: {path}")
                return
            for line in path.read_text(encoding="utf-8").splitlines():
                self._log(line)

        self._run_async(job)

    def _open_dir(self, path):
        path = Path(path)
        path.mkdir(exist_ok=True)
        self._log(f"Открываю {path}")
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            else:
                subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(str(e))

    # DUMP
    def cmd_start_dump(self):
        self._start_proc(DUMP_SCRIPT, "DUMP", "dump_proc", self.dump_status)

    def cmd_stop_dump(self):
        self._stop_proc("dump_proc", "DUMP", self.dump_status)

    def cmd_bt_dump(self):
        script = DUMP_BT if DUMP_BT.exists() else DUMP_BT_FALLBACK
        self._run_bt(script, "DUMP BT filters")

    def cmd_show_dump_bt(self):
        p = BACKTESTS_DIR / "latest_dump_filters.txt"
        if not p.exists():
            p = BACKTESTS_DIR / "latest_dump.txt"
        self._show_file(p)

    def cmd_push_dump_logs(self):
        def job():
            SIGNALS_DUMP_DIR.mkdir(exist_ok=True)
            (SIGNALS_DUMP_DIR / ".gitkeep").write_text("")
            self._run_cmd(["git", "add", "signals_dump"])
            r = subprocess.run(
                [find_git(), "status", "--porcelain", "signals_dump"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            if not (r.stdout or "").strip():
                self._log("Нет DUMP логов")
                return
            self._run_cmd(["git", "commit", "-m", f"signals dump {datetime.now():%Y-%m-%d %H:%M}"])
            self._run_cmd(["git", "push"])

        self._run_async(job)

    def cmd_open_dump_signals(self):
        SIGNALS_DUMP_DIR.mkdir(exist_ok=True)
        self._open_dir(SIGNALS_DUMP_DIR)

    # RAID minimal
    def cmd_start_raid(self):
        self._start_proc(RAID_SCRIPT, "RAID", "raid_proc", self.raid_status)

    def cmd_stop_raid(self):
        self._stop_proc("raid_proc", "RAID", self.raid_status)

    # shared
    def cmd_check_signals(self):
        def job():
            if not CHECK_SCRIPT.exists():
                self._log("check_signals нет")
                return
            self._run_cmd([sys.executable, "-u", str(CHECK_SCRIPT)], cwd=str(SCANNER_DIR))

        self._run_async(job)

    def cmd_paper(self):
        def job():
            if not PAPER_SCRIPT.exists():
                self._log("paper_engine.py нет — git pull")
                return
            self._log("--- Paper ---")
            try:
                p = subprocess.Popen(
                    [sys.executable, "-u", str(PAPER_SCRIPT)], cwd=str(SCANNER_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1, env=self._env())
                for line in p.stdout:
                    if line.rstrip():
                        self._log(line.rstrip())
                self._log("Paper OK" if p.wait() == 0 else "Paper fail")
            except Exception as e:
                self._log(f"Paper error: {e}")

        self._run_async(job)

    def cmd_git_pull(self):
        self._run_async(lambda: (self._log("--- git pull ---"), self._run_cmd(["git", "pull"])))

    def cmd_push_all(self):
        def job():
            BACKTESTS_DIR.mkdir(exist_ok=True)
            SIGNALS_DUMP_DIR.mkdir(exist_ok=True)
            PAPER_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "-A"])
            self._run_cmd(["git", "add", "-f", "--", "backtests", "signals", "signals_dump", "paper"])
            r = subprocess.run(
                [find_git(), "status", "--porcelain"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            dirty = (r.stdout or "").strip()
            if dirty:
                for line in dirty.splitlines()[:25]:
                    self._log("  " + line)
                self._run_cmd(["git", "commit", "-m", f"update {datetime.now():%Y-%m-%d %H:%M}"])
            else:
                self._log("Чисто")
            self._run_cmd(["git", "push"])

        self._run_async(job)

    def cmd_refresh(self):
        d_on = self.dump_proc and self.dump_proc.poll() is None
        r_on = self.raid_proc and self.raid_proc.poll() is None
        self.dump_status.set(f"DUMP: {'РАБОТАЕТ' if d_on else 'стоп'}")
        self.raid_status.set(f"RAID: {'РАБОТАЕТ' if r_on else 'стоп'}")


if __name__ == "__main__":
    ControlPanel().mainloop()
