#!/usr/bin/env python3
"""价格专员 · 测试采集工作台 — Tkinter 桌面 GUI"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import tkinter as tk

# Ensure src is on the path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT / "collectors"))

from price_specialist.catalog import DRUG_MAP
from price_specialist.test_runner import TestRunConfig, TestWorker, _progress


# ── Colour scheme ──────────────────────────────────────────────────────────
BG = "#f4f6fa"
FG = "#182235"
ACCENT = "#2563eb"
SUCCESS = "#16a34a"
FAILURE = "#dc2626"
INFO = "#2563eb"
MUTED = "#65738b"

# ── Drug selector (searchable checkbox list) ────────────────────────────────
class DrugSelector(ttk.LabelFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, text="药品选择（30 药）", padding=8, **kwargs)
        self.vars: dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter)
        ttk.Entry(top, textvariable=self.search_var, width=20).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="全选", command=self._select_all, width=6).pack(side="left", padx=2)
        ttk.Button(top, text="清除", command=self._clear_all, width=6).pack(side="left", padx=2)
        ttk.Label(top, text="搜索品牌或通用名", foreground=MUTED, font=("", 9)).pack(side="left", padx=(6, 0))

        # Scrollable frame for drug list
        canvas = tk.Canvas(self, height=240, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        # Mouse wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._populate()

    def _populate(self, filter_text=""):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.vars.clear()
        filter_lower = filter_text.lower()
        # DRUG_MAP is generic name -> brand name.
        items = sorted(DRUG_MAP.items(), key=lambda x: x[0])
        for generic, brand in items:
            if filter_lower and filter_lower not in brand.lower() and filter_lower not in generic.lower():
                continue
            var = tk.BooleanVar()
            self.vars[generic] = var
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", padx=4, pady=1)
            cb = ttk.Checkbutton(row, variable=var, text=generic)
            cb.pack(side="left")
            ttk.Label(row, text=brand, foreground=MUTED, font=("", 9)).pack(side="left", padx=(6, 0))

    def _filter(self, *_):
        self._populate(self.search_var.get())

    def _select_all(self):
        for var in self.vars.values():
            var.set(True)

    def _clear_all(self):
        for var in self.vars.values():
            var.set(False)

    def get_selected(self) -> list[str]:
        return [brand for brand, var in self.vars.items() if var.get()]


# ── Platform config (checkboxes + search modes) ────────────────────────────
class PlatformConfig(ttk.LabelFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, text="平台配置", padding=8, **kwargs)
        self.platform_vars: dict[str, tk.BooleanVar] = {}
        self.mode_vars: dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self):
        # Platform checkboxes
        plat_frame = ttk.Frame(self)
        plat_frame.pack(fill="x", pady=(0, 4))
        for name, code in [("药师帮", "yaoshibang"), ("淘宝", "taobao")]:
            var = tk.BooleanVar(value=True)
            self.platform_vars[code] = var
            ttk.Checkbutton(plat_frame, variable=var, text=f"{name} ({code})").pack(side="left", padx=(0, 12))

        # Search mode checkboxes
        mode_frame = ttk.Frame(self)
        mode_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(mode_frame, text="搜索模式:", foreground=FG).pack(side="left", padx=(0, 6))
        for mode_name, mode_code in [("全局搜索", "global_search"), ("店铺搜索", "store_search")]:
            var = tk.BooleanVar(value=True)
            self.mode_vars[mode_code] = var
            ttk.Checkbutton(mode_frame, variable=var, text=mode_name).pack(side="left", padx=(0, 8))

        # Rate policy editor
        ttk.Button(self, text="速率策略（展开编辑）", command=self._edit_rate_policies).pack(anchor="w")

    def _edit_rate_policies(self):
        win = tk.Toplevel(self)
        win.title("速率策略编辑")
        win.geometry("500x300")
        self._rate_entries = {}
        row = 0
        for platform in ("yaoshibang", "taobao"):
            ttk.Label(win, text=f"── {platform} ──", font=("", 10, "bold")).grid(row=row, column=0, columnspan=4, pady=(8, 4), sticky="w")
            row += 1
            fields = [
                ("detail_interval", "详情间隔(s)"),
                ("search_interval", "搜索间隔(s)"),
                ("batch_size", "Batch大小"),
                ("batch_cooldown", "Batch冷却(s)"),
            ]
            defaults = {"yaoshibang": (32, 45, 4, 240), "taobao": (25, 35, 5, 180)}
            self._rate_entries[platform] = {}
            for col, (field, label) in enumerate(fields):
                ttk.Label(win, text=label).grid(row=row, column=col, padx=4, sticky="e")
                var = tk.StringVar(value=str(defaults[platform][col]))
                entry = ttk.Entry(win, textvariable=var, width=8)
                entry.grid(row=row + 1, column=col, padx=4, pady=2)
                self._rate_entries[platform][field] = var
            row += 2

    def get_platforms(self) -> list[str]:
        return [code for code, var in self.platform_vars.items() if var.get()]

    def get_search_modes(self) -> list[str]:
        return [code for code, var in self.mode_vars.items() if var.get()]

    def get_rate_overrides(self) -> dict[str, dict]:
        if not hasattr(self, "_rate_entries"):
            return {}
        overrides = {}
        for platform, fields in self._rate_entries.items():
            vals = {}
            for field, var in fields.items():
                try:
                    vals[field] = float(var.get()) if "interval" in field or "cooldown" in field else int(var.get())
                except ValueError:
                    vals[field] = 0
            overrides[platform] = vals
        return overrides


# ── Collector params (search limit, etc.) ──────────────────────────────────
class CollectorConfig(ttk.LabelFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, text="采集参数", padding=8, **kwargs)
        self._build()

    def _build(self):
        row = ttk.Frame(self)
        row.pack(fill="x", pady=2)

        ttk.Label(row, text="每页搜索条数:").pack(side="left", padx=(0, 4))
        self.search_limit_var = tk.StringVar(value="5")
        ttk.Spinbox(row, from_=1, to=100, textvariable=self.search_limit_var, width=5).pack(side="left", padx=(0, 16))

        ttk.Label(row, text="最多确认候选:").pack(side="left", padx=(0, 4))
        self.max_candidates_var = tk.StringVar(value="3")
        ttk.Spinbox(row, from_=1, to=20, textvariable=self.max_candidates_var, width=5).pack(side="left", padx=(0, 16))

        ttk.Label(row, text="详情页上限:").pack(side="left", padx=(0, 4))
        self.inspect_limit_var = tk.StringVar(value="3")
        ttk.Spinbox(row, from_=1, to=20, textvariable=self.inspect_limit_var, width=5).pack(side="left")

        row2 = ttk.Frame(self)
        row2.pack(fill="x", pady=2)
        self.test_db_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, variable=self.test_db_var, text="使用测试数据库").pack(side="left", padx=(0, 16))
        self.output_var = tk.StringVar(value="")
        ttk.Label(row2, text="CSV输出目录:").pack(side="left", padx=(0, 4))
        ttk.Entry(row2, textvariable=self.output_var, width=24).pack(side="left", padx=(0, 4))
        ttk.Button(row2, text="浏览...", command=self._browse_output).pack(side="left")

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择 CSV 输出目录")
        if path:
            self.output_var.set(path)

    def get_config(self) -> TestRunConfig:
        return TestRunConfig(
            search_limit=int(self.search_limit_var.get() or 5),
            max_candidates=int(self.max_candidates_var.get() or 3),
            inspect_limit=int(self.inspect_limit_var.get() or 3),
            use_test_db=self.test_db_var.get(),
            output_root=self.output_var.get() or None,
        )


# ── Log panel (coloured, auto-scroll) ──────────────────────────────────────
class LogPanel(ttk.LabelFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, text="执行日志", padding=4, **kwargs)
        self._build()

    def _build(self):
        self.text = tk.Text(self, height=14, width=100, bg="#1e1e2e", fg="#cdd6f4",
                            insertbackground="white", font=("Menlo", 10), wrap="word",
                            state="disabled", relief="flat")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Define colour tags
        self.text.tag_configure("success", foreground="#a6e3a1")
        self.text.tag_configure("failed", foreground="#f38ba8")
        self.text.tag_configure("running", foreground="#89b4fa")
        self.text.tag_configure("info", foreground="#74c7ec")
        self.text.tag_configure("muted", foreground="#6c7086")
        self.text.tag_configure("phase_init", foreground="#cba6f7")
        self.text.tag_configure("phase_search", foreground="#89b4fa")
        self.text.tag_configure("phase_inspect", foreground="#f9e2af")
        self.text.tag_configure("phase_store", foreground="#a6e3a1")
        self.text.tag_configure("phase_done", foreground="#94e2d5")
        self.text.tag_configure("phase_export", foreground="#fab387")
        self.text.tag_configure("phase_error", foreground="#f38ba8")

    def append(self, timestamp, phase, status, message, detail=""):
        self.text.configure(state="normal")
        # Phase tag
        phase_tag = f"phase_{phase}" if phase in ("init", "search", "inspect", "store", "done", "export", "error") else "info"
        status_tag = status if status in ("success", "failed", "running") else "info"
        # Timestamp
        self.text.insert("end", f"  {timestamp} ", "muted")
        # Phase
        self.text.insert("end", f"[{phase.upper():7}] ", phase_tag)
        # Status indicator
        indicator = {"success": "✓", "failed": "✗", "running": "►"}.get(status, "·")
        self.text.insert("end", f"{indicator} ", status_tag)
        # Message
        self.text.insert("end", f"{message}\n", status_tag)
        # Detail (if any) — dimmed
        if detail:
            self.text.insert("end", f"           {detail}\n", "muted")
        self.text.configure(state="disabled")
        self.text.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# ── Result panel (per-platform progress) ───────────────────────────────────
class ResultPanel(ttk.LabelFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, text="结果摘要", padding=8, **kwargs)
        self._build()

    def _build(self):
        self.info_var = tk.StringVar(value="等待开始...")
        ttk.Label(self, textvariable=self.info_var, font=("", 10)).pack(anchor="w")

        self.platform_frames: dict[str, ttk.Frame] = {}
        self.platform_bars: dict[str, ttk.Progressbar] = {}
        self.platform_labels: dict[str, tk.StringVar] = {}
        self.drug_labels: dict[str, tk.StringVar] = {}

        self.btn_frame = ttk.Frame(self)
        self.btn_frame.pack(fill="x", pady=(4, 0))
        self.open_csv_btn = ttk.Button(self.btn_frame, text="打开 CSV 目录", command=self._open_csv, state="disabled")
        self.open_csv_btn.pack(side="left", padx=(0, 8))
        self.csv_path: str | None = None

    def add_platform(self, platform: str, total: int):
        if platform in self.platform_frames:
            return
        frame = ttk.Frame(self)
        frame.pack(fill="x", pady=2)
        self.platform_frames[platform] = frame

        label_var = tk.StringVar(value=f"{platform}: 0/{total}")
        ttk.Label(frame, textvariable=label_var, width=30, anchor="w").pack(side="left")
        self.platform_labels[platform] = label_var

        bar = ttk.Progressbar(frame, length=200, maximum=total)
        bar.pack(side="left", padx=8)
        self.platform_bars[platform] = bar

        self.drug_labels[platform] = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.drug_labels[platform], foreground=MUTED,
                  font=("", 9)).pack(side="left")

    def update_platform(self, platform: str, completed: int, failed: int, total: int,
                        drug_name: str = "", detail: str = ""):
        if platform not in self.platform_frames:
            self.add_platform(platform, total)
        self.platform_labels[platform].set(f"{platform}: {completed}/{total}  [✓{completed} ✗{failed}]")
        if total > 0:
            self.platform_bars[platform]["maximum"] = total
            self.platform_bars[platform]["value"] = completed
        if drug_name:
            self.drug_labels[platform].set(f"{drug_name} {detail}")

    def set_status(self, run_id: str, status: str, elapsed: float = 0):
        elapsed_str = f" 耗时: {elapsed:.0f}s" if elapsed else ""
        self.info_var.set(f"▸ 运行ID: {run_id[:8] if run_id else '—'}...  {elapsed_str}  状态: {status}")

    def set_csv_path(self, path: str):
        self.csv_path = path
        self.open_csv_btn.configure(state="normal")

    def _open_csv(self):
        if self.csv_path and os.path.isdir(self.csv_path):
            subprocess.run(["open", self.csv_path], check=False)

    def reset(self):
        self.info_var.set("等待开始...")
        for platform in list(self.platform_frames.keys()):
            self.platform_frames[platform].destroy()
            del self.platform_frames[platform]
            del self.platform_bars[platform]
            del self.platform_labels[platform]
            del self.drug_labels[platform]
        self.csv_path = None
        self.open_csv_btn.configure(state="disabled")


# ── Main application ───────────────────────────────────────────────────────
class TestWorkbench(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("价格专员 · 测试采集工作台")
        self.geometry("900x680+100+100")
        self.configure(bg=BG)
        self.resizable(True, True)

        self.project_root = _PROJECT_ROOT
        self.worker: TestWorker | None = None
        self._build()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # ── Top: Drug selector ──
        self.drug_selector = DrugSelector(self)
        self.drug_selector.pack(fill="x", padx=8, pady=(8, 4))

        # ── Middle left: Platform config ──
        mid = ttk.Frame(self)
        mid.pack(fill="x", padx=8, pady=4)
        self.platform_config = PlatformConfig(mid)
        self.platform_config.pack(side="left", fill="x", expand=True, padx=(0, 4))

        # ── Middle right: Collector params ──
        self.collector_config = CollectorConfig(mid)
        self.collector_config.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ── Control buttons ──
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(ctrl, text="▶ 开始采集", command=self._start_collection)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(ctrl, text="■ 停止", command=self._stop_collection, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.status_label = ttk.Label(ctrl, text="就绪", foreground=MUTED)
        self.status_label.pack(side="left", padx=(8, 0))

        # ── Log panel ──
        self.log_panel = LogPanel(self)
        self.log_panel.pack(fill="both", expand=True, padx=8, pady=4)

        # ── Result panel ──
        self.result_panel = ResultPanel(self)
        self.result_panel.pack(fill="x", padx=8, pady=(0, 8))

        # ── Poll timer ──
        self._poll_id = None

    def _start_collection(self):
        drugs = self.drug_selector.get_selected()
        if not drugs:
            messagebox.showwarning("提示", "请至少选择一个药品")
            return
        platforms = self.platform_config.get_platforms()
        if not platforms:
            messagebox.showwarning("提示", "请至少选择一个平台")
            return
        search_modes = self.platform_config.get_search_modes()
        if not search_modes:
            messagebox.showwarning("提示", "请至少选择一种搜索模式")
            return

        config = self.collector_config.get_config()
        config.drugs = drugs
        config.platforms = platforms
        config.search_modes = search_modes
        config.rate_policy_overrides = self.platform_config.get_rate_overrides()

        # Reset UI
        self.log_panel.clear()
        self.result_panel.reset()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="启动中...", foreground=INFO)

        self.log_panel.append(
            time.strftime("%H:%M:%S"), "init", "running",
            f"启动采集: {len(drugs)} 药 × {len(platforms)} 平台, 模式: {', '.join(search_modes)}",
            f"搜索条数={config.search_limit}, 候选上限={config.max_candidates}",
        )

        # Start worker
        self.worker = TestWorker(config, self.project_root)
        self.worker.start()
        self._poll_progress()

    def _stop_collection(self):
        if self.worker:
            self.worker.cancel()
            self.log_panel.append(time.strftime("%H:%M:%S"), "init", "failed", "用户已停止采集")
            self.status_label.configure(text="已停止", foreground=FAILURE)
        self._cleanup_after_run()

    def _poll_progress(self):
        if not self.worker:
            self._cleanup_after_run()
            return
        try:
            while True:
                update = self.worker.queue.get_nowait()
                self._handle_update(update)
        except queue.Empty:
            pass
        # Check if worker thread is still alive
        if self.worker.thread.is_alive():
            self._poll_id = self.after(200, self._poll_progress)
        else:
            self._cleanup_after_run()

    def _handle_update(self, update):
        # Log
        phase = update.phase
        if phase == "store_search":
            phase = "store"
        self.log_panel.append(
            update.timestamp, phase, update.status,
            update.message, update.detail or "",
        )

        # Status bar
        if update.status == "running":
            self.status_label.configure(text=f"运行中 {update.platform}: {update.message}", foreground=INFO)
        elif update.status == "success":
            self.status_label.configure(text=update.message, foreground=SUCCESS)

        # Result panel
        if update.platform:
            self.result_panel.add_platform(update.platform, update.platform_total)
            self.result_panel.update_platform(
                update.platform, update.platform_completed, update.platform_failed,
                update.platform_total, update.drug_name or "", update.detail or "",
            )
        if update.run_id:
            self.result_panel.set_status(update.run_id, update.status, update.elapsed_seconds)
        if update.phase == "export" and update.status == "success":
            self.result_panel.set_csv_path(update.output_path or "")

    def _cleanup_after_run(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.worker = None
        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None

    def _on_close(self):
        if self.worker:
            self.worker.cancel()
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = TestWorkbench()
    app.mainloop()
