#!/usr/bin/env python3
"""DUMP + LAB + VAL-FADE. RAID removed. Signals via Paper."""

import os, sys, subprocess, threading, queue, shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

ROOT = Path(__file__).resolve().parent
SCANNER_DIR = ROOT / "scanner"
SIGNALS_DUMP_DIR = ROOT / "signals_dump"
SIGNALS_LAB_DIR = ROOT / "signals_dump_lab"
SIGNALS_VAL_DIR = ROOT / "signals_val"
PAPER_DIR = ROOT / "paper"
BACKTESTS_DIR = ROOT / "backtests"

DUMP_SCRIPT = SCANNER_DIR / "dump_scanner.py"
LAB_SCRIPT = SCANNER_DIR / "dump_scanner_lab.py"
VAL_SCRIPT = SCANNER_DIR / "val_scanner.py"
VAL_PAPER = SCANNER_DIR / "val_paper.py"
PAPER_SCRIPT = SCANNER_DIR / "paper_engine.py"
DUMP_PAPER_FILE = PAPER_DIR / "latest.txt"
VAL_PAPER_FILE = PAPER_DIR / "val_latest.txt"
VAL_STATS = PAPER_DIR / "live_stats.xlsx"


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
        self.title("DUMP / VAL — Control")
        self.geometry("1040x660")
        self.minsize(800, 520)
        self.configure(bg="#1e1e1e")
        self.dump_proc = None
        self.lab_proc = None
        self.val_proc = None
        self.log_queue = queue.Queue()
        self._build_ui()
        self.after(100, self._poll_log)
        self._log(f"Панель | {ROOT}")
        self._log(f"DUMP: {DUMP_SCRIPT.name} | LAB: {LAB_SCRIPT.name} | VAL: {VAL_SCRIPT.name}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=6, font=("Segoe UI", 9))
        style.configure("TLabel", background="#1e1e1e", foreground="#ddd", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground="#7dd3fc")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#aaa")
        ttk.Label(self, text="DUMP Control  ·  VAL-FADE", style="Header.TLabel").pack(pady=(10, 4))
        st = ttk.Frame(self)
        st.pack(fill="x", padx=12)
        self.dump_status = tk.StringVar(value="DUMP: стоп")
        self.lab_status = tk.StringVar(value="LAB: стоп")
        self.val_status = tk.StringVar(value="VAL: стоп")
        ttk.Label(st, textvariable=self.dump_status, style="Status.TLabel").pack(side="left", padx=8)
        ttk.Label(st, textvariable=self.lab_status, style="Status.TLabel").pack(side="left", padx=8)
        ttk.Label(st, textvariable=self.val_status, style="Status.TLabel").pack(side="left", padx=8)
        cols = ttk.Frame(self)
        cols.pack(fill="x", padx=12, pady=8)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)
        cols.columnconfigure(2, weight=1)
        dump_col = tk.Frame(cols, bg="#2e2a1a", padx=8, pady=8)
        dump_col.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        tk.Label(dump_col, text="● DUMP v0.2b", bg="#2e2a1a", fg="#fbbf24",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        for text, cmd in [
            ("▶ Старт DUMP", self.cmd_start_dump),
            ("■ Стоп DUMP", self.cmd_stop_dump),
            ("Paper DUMP", self.cmd_paper_dump),
            ("Push логов DUMP", self.cmd_push_dump_logs),
        ]:
            ttk.Button(dump_col, text=text, command=cmd).pack(fill="x", pady=2)
        lab_col = tk.Frame(cols, bg="#12303a", padx=8, pady=8)
        lab_col.grid(row=0, column=1, sticky="nsew", padx=4)
        tk.Label(lab_col, text="◇ LAB skip-Asia", bg="#12303a", fg="#7dd3fc",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        for text, cmd in [
            ("▶ Старт LAB", self.cmd_start_lab),
            ("■ Стоп LAB", self.cmd_stop_lab),
            ("Push логов LAB", self.cmd_push_lab_logs),
        ]:
            ttk.Button(lab_col, text=text, command=cmd).pack(fill="x", pady=2)
        val_col = tk.Frame(cols, bg="#3a1520", padx=8, pady=8)
        val_col.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        tk.Label(val_col, text="● VAL-FADE", bg="#3a1520", fg="#f9a8d4",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        for text, cmd in [
            ("▶ Старт VAL", self.cmd_start_val),
            ("■ Стоп VAL", self.cmd_stop_val),
            ("Paper VAL", self.cmd_paper_val),
            ("Push логов VAL", self.cmd_push_val_logs),
        ]:
            ttk.Button(val_col, text=text, command=cmd).pack(fill="x", pady=2)
        shared = ttk.Frame(self)
        shared.pack(fill="x", padx=12, pady=(4, 4))
        for text, cmd in [
            ("Git Pull", self.cmd_git_pull),
            ("Push all", self.cmd_push_all),
            ("signals_dump", self.cmd_open_dump_signals),
            ("signals_val", self.cmd_open_val_signals),
            ("Статус", self.cmd_refresh),
        ]:
            ttk.Button(shared, text=text, command=cmd).pack(side="left", padx=4)
        ttk.Label(self, text="Лог", style="Status.TLabel").pack(anchor="w", padx=16)
        self.log_box = scrolledtext.ScrolledText(
            self, height=16, bg="#121212", fg="#d4d4d4",
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

    def _start_proc(self, script, label, attr, status_var):
        proc = getattr(self, attr)
        if proc and proc.poll() is None:
            self._log(f"{label} уже запущен"); return
        if not script.exists():
            messagebox.showerror("Ошибка", f"Нет файла:\n{script}\ngit pull"); return
        def job():
            self._log(f"--- старт {script.name} ---")
            try:
                p = subprocess.Popen([sys.executable, "-u", str(script)], cwd=str(SCANNER_DIR),
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
                self._log(f"fail: {e}"); status_var.set(f"{label}: ошибка")
        self._run_async(job)

    def _stop_proc(self, attr, label, status_var):
        proc = getattr(self, attr)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            status_var.set(f"{label}: стоп"); self._log(f"{label} остановлен")
        else:
            self._log(f"{label} не запущен")

    def _show_file(self, path):
        def job():
            if not path.exists():
                self._log(f"нет {path}"); return
            for line in path.read_text(encoding="utf-8").splitlines():
                self._log(line)
        self._run_async(job)

    def _open_dir(self, path):
        path = Path(path); path.mkdir(exist_ok=True)
        self._log(f"Открываю {path}")
        try:
            os.startfile(str(path)) if sys.platform.startswith("win") else subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(str(e))

    def _open_file(self, path):
        if not path.exists():
            self._log(f"нет {path}"); return
        try:
            os.startfile(str(path)) if sys.platform.startswith("win") else subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            self._log(str(e))

    def cmd_start_dump(self):
        self._start_proc(DUMP_SCRIPT, "DUMP", "dump_proc", self.dump_status)
    def cmd_stop_dump(self):
        self._stop_proc("dump_proc", "DUMP", self.dump_status)
    def cmd_start_lab(self):
        self._start_proc(LAB_SCRIPT, "LAB", "lab_proc", self.lab_status)
    def cmd_stop_lab(self):
        self._stop_proc("lab_proc", "LAB", self.lab_status)
    def cmd_start_val(self):
        self._start_proc(VAL_SCRIPT, "VAL", "val_proc", self.val_status)
    def cmd_stop_val(self):
        self._stop_proc("val_proc", "VAL", self.val_status)
    def cmd_open_dump_signals(self):
        self._open_dir(SIGNALS_DUMP_DIR)
    def cmd_open_val_signals(self):
        self._open_dir(SIGNALS_VAL_DIR)

    def cmd_push_dump_logs(self):
        def job():
            SIGNALS_DUMP_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "signals_dump"])
            r = subprocess.run([find_git(), "status", "--porcelain", "signals_dump"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            if not (r.stdout or "").strip():
                self._log("Нет DUMP логов"); return
            self._run_cmd(["git", "commit", "-m", f"signals dump {datetime.now():%Y-%m-%d %H:%M}"])
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_push_lab_logs(self):
        def job():
            SIGNALS_LAB_DIR.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "signals_dump_lab"])
            r = subprocess.run([find_git(), "status", "--porcelain", "signals_dump_lab"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            if not (r.stdout or "").strip():
                self._log("Нет LAB логов"); return
            self._run_cmd(["git", "commit", "-m", f"signals lab {datetime.now():%Y-%m-%d %H:%M}"])
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_push_val_logs(self):
        def job():
            SIGNALS_VAL_DIR.mkdir(exist_ok=True)
            (SIGNALS_VAL_DIR / ".gitkeep").write_text("")
            self._run_cmd(["git", "add", "signals_val"])
            r = subprocess.run([find_git(), "status", "--porcelain", "signals_val"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=self._env())
            if not (r.stdout or "").strip():
                self._log("Нет VAL логов"); return
            self._run_cmd(["git", "commit", "-m", f"signals val {datetime.now():%Y-%m-%d %H:%M}"])
            self._run_cmd(["git", "push"])
        self._run_async(job)

    def cmd_paper_dump(self):
        def job():
            self._log("--- Paper DUMP ---")
            if PAPER_SCRIPT.exists():
                self._run_cmd([sys.executable, "-u", str(PAPER_SCRIPT)], cwd=str(SCANNER_DIR))
            if DUMP_PAPER_FILE.exists():
                for line in DUMP_PAPER_FILE.read_text(encoding="utf-8").splitlines():
                    self._log(line)
            else:
                self._log(f"нет {DUMP_PAPER_FILE}")
        self._run_async(job)

    def cmd_paper_val(self):
        def job():
            self._log("--- Paper VAL ---")
            PAPER_DIR.mkdir(exist_ok=True)
            if VAL_PAPER.exists():
                self._run_cmd([sys.executable, "-u", str(VAL_PAPER)], cwd=str(SCANNER_DIR))
            if VAL_PAPER_FILE.exists():
                for line in VAL_PAPER_FILE.read_text(encoding="utf-8").splitlines():
                    self._log(line)
            else:
                self._log(f"нет {VAL_PAPER_FILE}")
            if VAL_STATS.exists():
                self._open_file(VAL_STATS)
        self._run_async(job)

    def cmd_git_pull(self):
        self._run_async(lambda: (self._log("--- git pull ---"), self._run_cmd(["git", "pull"])))

    def cmd_push_all(self):
        def job():
            for d in (BACKTESTS_DIR, SIGNALS_DUMP_DIR, SIGNALS_VAL_DIR, PAPER_DIR):
                d.mkdir(exist_ok=True)
            self._run_cmd(["git", "add", "-A"])
            self._run_cmd(["git", "add", "-f", "--", "backtests", "signals_dump", "signals_dump_lab", "signals_val", "paper"])
            r = subprocess.run([find_git(), "status", "--porcelain"],
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
        l_on = self.lab_proc and self.lab_proc.poll() is None
        v_on = self.val_proc and self.val_proc.poll() is None
        self.dump_status.set(f"DUMP: {'РАБОТАЕТ' if d_on else 'стоп'}")
        self.lab_status.set(f"LAB: {'РАБОТАЕТ' if l_on else 'стоп'}")
        self.val_status.set(f"VAL: {'РАБОТАЕТ' if v_on else 'стоп'}")
        self._log(f"Статус: DUMP={'ON' if d_on else 'off'} LAB={'ON' if l_on else 'off'} VAL={'ON' if v_on else 'off'}")


if __name__ == "__main__":
    ControlPanel().mainloop()
