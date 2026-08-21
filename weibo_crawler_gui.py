#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微博爬虫 - 图形界面入口
======================
运行:
  python weibo_crawler_gui.py

功能:
  - 输入博主昵称、微博ID、开始/结束日期,一键运行
  - 类名自动识别;自动识别失败时弹出对话框手动输入类名
  - 高级设置:无头模式、用户数据目录、手动覆盖类名
  - 日志实时显示
"""

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, scrolledtext, ttk

from weibo_crawler_core import (
    ensure_logger, ClassNameManager, run_task, resource_path, app_dir,
)

# Windows 下隐藏后台控制台窗口(用 python.exe 启动时仍会弹出 cmd 黑窗口,这里将其隐藏)
if sys.platform == "win32":
    try:
        import ctypes
        _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE
    except Exception:
        pass

logger = logging.getLogger('weibo_crawler')

# 默认使用程序目录下的 EdgeUserData(登录状态保存在本地,便于迁移与分享)
DEFAULT_USER_DATA_DIR = os.path.join(app_dir(), "EdgeUserData")

CLASS_KEY_NAMES = {
    "card": "微博卡片类名(div)",
    "time": "时间链接类名(a)",
    "name": "用户名类名(div)",
    "content": "正文类名(div)",
    "stats_wrap": "转评赞容器类名(div)",
    "stats_num": "转评赞数字类名(span)",
}

CLASS_KEY_HINTS = {
    "card": "如: _body_ecgcn_63",
    "time": "如: _time_1tpft_33",
    "name": "如: _name_1yc79_291",
    "content": "如: _wbtext_q1l14_14",
    "stats_wrap": "如: _wrap_198pe_137",
    "stats_num": "如: _num_198pe_46",
}


class LogQueueHandler(logging.Handler):
    """将日志记录推送到队列,由 GUI 主线程刷新显示"""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class WeiboCrawlerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("微博爬虫 - 收集微博并导出Markdown")
        self.root.geometry("860x680")
        self.root.minsize(760, 600)

        self.log_queue = queue.Queue()
        self.manual_req_queue = queue.Queue()   # 后台线程 -> 主线程:请求手动输入类名
        self.manual_resp_queue = queue.Queue()  # 主线程 -> 后台线程:用户输入结果
        self.wait_req_queue = queue.Queue()     # 后台线程 -> 主线程:请求用户完成操作
        self.wait_resp_queue = queue.Queue()    # 主线程 -> 后台线程:用户已确认
        self.worker = None
        self.running = False
        self.settings = self._load_settings()

        self._build_ui()

        # 日志 handler
        ensure_logger()
        self.log_handler = LogQueueHandler(self.log_queue)
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger('weibo_crawler').addHandler(self.log_handler)

        # 轮询队列刷新界面
        self._poll_queues()

        # 窗口关闭时清理
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 设置持久化(记住上次选择的日期等) ----------

    def _settings_path(self):
        """设置文件路径:与程序同目录(exe 版在 exe 旁,源码版在脚本旁)"""
        return os.path.join(app_dir(), "gui_settings.json")

    def _load_settings(self):
        """加载上次保存的设置,文件不存在或损坏时返回空字典"""
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self):
        """保存当前设置(日期等)到文件"""
        try:
            data = {
                "start_year": self.var_start_year.get(),
                "start_month": self.var_start_month.get(),
                "start_day": self.var_start_day.get(),
                "end_year": self.var_end_year.get(),
                "end_month": self.var_end_month.get(),
                "end_day": self.var_end_day.get(),
            }
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存设置失败: {e}")

    # ---------- UI 构建 ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 顶部:参数输入
        frame_top = ttk.LabelFrame(self.root, text="任务参数", padding=10)
        frame_top.pack(fill="x", padx=10, pady=(10, 4))

        row1 = ttk.Frame(frame_top)
        row1.pack(fill="x")
        ttk.Label(row1, text="博主昵称:").pack(side="left")
        self.var_name = tk.StringVar(value="卢诗翰")
        ttk.Entry(row1, textvariable=self.var_name, width=18).pack(side="left", padx=(4, 20))
        ttk.Label(row1, text="微博ID:").pack(side="left")
        self.var_uid = tk.StringVar(value="3276099007")
        ttk.Entry(row1, textvariable=self.var_uid, width=18).pack(side="left", padx=4)

        row2 = ttk.Frame(frame_top)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="开始日期:").pack(side="left")

        # 日期选择器:年/月/日下拉框,自动处理每月天数(大小月/闰年)
        # 初始值:优先读取上次保存的设置;首次使用则为空,由用户自行选择
        now = datetime.now()
        years = [str(y) for y in range(now.year - 3, now.year + 1)]
        months = [f"{m:02d}" for m in range(1, 13)]
        st = self.settings

        self.var_start_year = tk.StringVar(value=st.get("start_year", ""))
        self.var_start_month = tk.StringVar(value=st.get("start_month", ""))
        self.var_start_day = tk.StringVar(value=st.get("start_day", ""))
        ttk.Combobox(row2, textvariable=self.var_start_year, values=years,
                     width=6, state="readonly").pack(side="left")
        ttk.Label(row2, text="年").pack(side="left")
        start_month_cb = ttk.Combobox(row2, textvariable=self.var_start_month, values=months,
                                      width=4, state="readonly")
        start_month_cb.pack(side="left")
        ttk.Label(row2, text="月").pack(side="left")
        self.start_day_cb = ttk.Combobox(row2, textvariable=self.var_start_day,
                                         width=4, state="readonly")
        self.start_day_cb.pack(side="left")
        ttk.Label(row2, text="日", foreground="gray").pack(side="left", padx=(0, 20))

        ttk.Label(row2, text="结束日期:").pack(side="left")
        self.var_end_year = tk.StringVar(value=st.get("end_year", ""))
        self.var_end_month = tk.StringVar(value=st.get("end_month", ""))
        self.var_end_day = tk.StringVar(value=st.get("end_day", ""))
        ttk.Combobox(row2, textvariable=self.var_end_year, values=years,
                     width=6, state="readonly").pack(side="left")
        ttk.Label(row2, text="年").pack(side="left")
        end_month_cb = ttk.Combobox(row2, textvariable=self.var_end_month, values=months,
                                    width=4, state="readonly")
        end_month_cb.pack(side="left")
        ttk.Label(row2, text="月").pack(side="left")
        self.end_day_cb = ttk.Combobox(row2, textvariable=self.var_end_day,
                                       width=4, state="readonly")
        self.end_day_cb.pack(side="left")
        ttk.Label(row2, text="日").pack(side="left")
        ttk.Label(row2, text="  (日的选项随年月自动调整)",
                  foreground="gray").pack(side="left", padx=6)

        # 初始化日的选项,并在年/月变化时刷新
        self._refresh_day_options(self.var_start_year, self.var_start_month,
                                  self.var_start_day, self.start_day_cb)
        self._refresh_day_options(self.var_end_year, self.var_end_month,
                                  self.var_end_day, self.end_day_cb)
        start_month_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_day_options(
            self.var_start_year, self.var_start_month, self.var_start_day, self.start_day_cb))
        self.var_start_year.trace_add("write", lambda *a: self._refresh_day_options(
            self.var_start_year, self.var_start_month, self.var_start_day, self.start_day_cb))
        end_month_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_day_options(
            self.var_end_year, self.var_end_month, self.var_end_day, self.end_day_cb))
        self.var_end_year.trace_add("write", lambda *a: self._refresh_day_options(
            self.var_end_year, self.var_end_month, self.var_end_day, self.end_day_cb))

        # 高级设置
        frame_adv = ttk.LabelFrame(self.root, text="高级设置", padding=10)
        frame_adv.pack(fill="x", padx=10, pady=4)

        row3 = ttk.Frame(frame_adv)
        row3.pack(fill="x")
        self.var_headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="无头模式(不显示浏览器)", variable=self.var_headless).pack(side="left")
        ttk.Label(row3, text="  用户数据目录:").pack(side="left")
        self.var_userdata = tk.StringVar(value=DEFAULT_USER_DATA_DIR)
        ttk.Entry(row3, textvariable=self.var_userdata, width=46).pack(side="left", padx=4)

        row4 = ttk.Frame(frame_adv)
        row4.pack(fill="x", pady=(6, 0))
        self.var_keep_browser = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="完成后保留浏览器", variable=self.var_keep_browser).pack(side="left")
        self.var_skip_export = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="只收集ID不导出MD", variable=self.var_skip_export).pack(side="left", padx=10)

        # 路线图更新1: 爬取间隔可自定义
        row4b = ttk.Frame(frame_adv)
        row4b.pack(fill="x", pady=(6, 0))
        ttk.Label(row4b, text="爬取间隔(秒):").pack(side="left")
        self.var_min_interval = tk.StringVar(value="3")
        ttk.Spinbox(row4b, from_=1, to=60, textvariable=self.var_min_interval,
                    width=4).pack(side="left", padx=2)
        ttk.Label(row4b, text="~").pack(side="left")
        self.var_max_interval = tk.StringVar(value="8")
        ttk.Spinbox(row4b, from_=1, to=120, textvariable=self.var_max_interval,
                    width=4).pack(side="left", padx=2)
        ttk.Label(row4b, text="秒  (提示:勿设置过短,避免触发风控)",
                  foreground="gray").pack(side="left", padx=6)

        # 路线图更新3/4: 图片视频下载 + 导出格式
        row4c = ttk.Frame(frame_adv)
        row4c.pack(fill="x", pady=(6, 0))
        self.var_download_images = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4c, text="下载图片", variable=self.var_download_images).pack(side="left")
        self.var_download_videos = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4c, text="下载视频", variable=self.var_download_videos).pack(side="left", padx=10)
        ttk.Label(row4c, text="导出格式:").pack(side="left", padx=(10, 0))
        self.var_export_format = tk.StringVar(value="md")
        ttk.Combobox(row4c, textvariable=self.var_export_format, values=["md", "docx"],
                     width=6, state="readonly").pack(side="left")

        # 类名覆盖
        row5 = ttk.Frame(frame_adv)
        row5.pack(fill="x", pady=(6, 0))
        ttk.Label(row5, text="类名覆盖(留空则自动识别):", foreground="gray").pack(anchor="w")
        self.class_vars = {}
        grid = ttk.Frame(frame_adv)
        grid.pack(fill="x", pady=(4, 0))
        cm = ClassNameManager()
        for i, key in enumerate(["card", "time", "name", "content", "stats_wrap", "stats_num"]):
            r, c = divmod(i, 3)
            cell = ttk.Frame(grid)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 14), pady=2)
            ttk.Label(cell, text=CLASS_KEY_NAMES[key] + ":").pack(anchor="w")
            var = tk.StringVar(value=cm.get(key) or "")
            ttk.Entry(cell, textvariable=var, width=24).pack(anchor="w")
            self.class_vars[key] = var

        # 控制按钮
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(frame_btn, text="开始爬取", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frame_btn, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(frame_btn, text="就绪", foreground="green")
        self.lbl_status.pack(side="left", padx=12)

        # 日志区
        frame_log = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        frame_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt_log = scrolledtext.ScrolledText(frame_log, height=14, state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True)

    # ---------- 日志与队列 ----------

    def _append_log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _poll_queues(self):
        """主线程周期性检查队列:日志刷新 / 手动输入类名请求 / 等待确认请求"""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass

        try:
            while True:
                key = self.manual_req_queue.get_nowait()
                self._show_manual_dialog(key)
        except queue.Empty:
            pass

        try:
            while True:
                msg = self.wait_req_queue.get_nowait()
                self._show_wait_dialog(msg)
        except queue.Empty:
            pass

        self.root.after(150, self._poll_queues)

    def _show_manual_dialog(self, key):
        """主线程弹出手动输入类名对话框(由后台线程请求触发)"""
        current = self.class_vars.get(key, tk.StringVar()).get() or ""
        dialog = tk.Toplevel(self.root)
        dialog.title("手动输入类名")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("460x190")

        ttk.Label(dialog, text="自动识别类名失败,请手动输入:", font=("", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        ttk.Label(dialog, text=f"目标: {CLASS_KEY_NAMES.get(key, key)}", foreground="blue").pack(anchor="w", padx=14)
        ttk.Label(dialog, text=f"提示: {CLASS_KEY_HINTS.get(key, '')}", foreground="gray").pack(anchor="w", padx=14)

        var = tk.StringVar(value=current)
        entry = ttk.Entry(dialog, textvariable=var, width=44)
        entry.pack(fill="x", padx=14, pady=8)
        entry.focus_set()

        def confirm():
            val = var.get().strip()
            if not val:
                messagebox.showwarning("提示", "类名不能为空", parent=dialog)
                return
            self.manual_resp_queue.put(val)
            dialog.destroy()

        def cancel():
            self.manual_resp_queue.put(None)
            dialog.destroy()

        row = ttk.Frame(dialog)
        row.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(row, text="确定", command=confirm).pack(side="right")
        ttk.Button(row, text="取消", command=cancel).pack(side="right", padx=8)
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    # ---------- 运行控制 ----------

    def manual_callback(self, key, current):
        """后台线程调用:请求主线程弹窗,并等待用户输入"""
        # 若窗口已关闭,直接放弃
        if not self.root.winfo_exists():
            return None
        self.manual_req_queue.put(key)
        try:
            return self.manual_resp_queue.get(timeout=600)
        except queue.Empty:
            return None

    def wait_callback(self, message):
        """后台线程调用:弹出提示对话框,等待用户完成操作(如手动登录)后继续"""
        if not self.root.winfo_exists():
            return
        self.wait_req_queue.put(message)
        try:
            self.wait_resp_queue.get(timeout=1800)
        except queue.Empty:
            pass

    def _show_wait_dialog(self, message):
        """主线程弹出"请完成操作"对话框"""
        messagebox.showinfo("请完成操作", message, parent=self.root)
        self.wait_resp_queue.put(True)

    @staticmethod
    def _days_in_month(year, month):
        """返回某年某月的天数(自动处理大小月与闰年)"""
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)
        return (next_month - timedelta(days=1)).day

    def _refresh_day_options(self, year_var, month_var, day_var, day_cb):
        """根据当前选中的年/月刷新"日"下拉选项(如 2 月只有 1~28/29)"""
        try:
            year = int(year_var.get())
            month = int(month_var.get())
            days = [f"{d:02d}" for d in range(1, self._days_in_month(year, month) + 1)]
        except (ValueError, TypeError):
            days = [f"{d:02d}" for d in range(1, 32)]
        day_cb.configure(values=days)
        # 年月未选择时,日保持为空,不自动填充
        if not year_var.get() or not month_var.get():
            return
        # 当前选中的日若超出新月份天数(如 31 日 -> 2 月),自动修正
        if day_var.get() not in days:
            day_var.set(days[-1])

    @staticmethod
    def _month_range(year_str, month_str):
        """根据年月计算该月的起止日期(自动处理每月天数)"""
        year = int(year_str)
        month = int(month_str)
        start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)
        last_day = (next_month - timedelta(days=1)).day
        end = f"{year:04d}-{month:02d}-{last_day:02d}"
        return start, end

    def _start(self):
        if self.running:
            return
        name = self.var_name.get().strip()
        uid = self.var_uid.get().strip()

        # 从年/月/日选择器解析日期
        try:
            start = (f"{int(self.var_start_year.get()):04d}-"
                     f"{int(self.var_start_month.get()):02d}-"
                     f"{int(self.var_start_day.get()):02d}")
            end = (f"{int(self.var_end_year.get()):04d}-"
                   f"{int(self.var_end_month.get()):02d}-"
                   f"{int(self.var_end_day.get()):02d}")
        except (ValueError, TypeError):
            messagebox.showwarning("日期错误", "请选择有效的开始/结束日期")
            return

        if not (name and uid):
            messagebox.showwarning("参数不完整", "请填写博主昵称和微博ID")
            return

        # 记住本次选择的日期,下次打开自动填入
        self._save_settings()

        # 读取类名覆盖
        cm = ClassNameManager(manual_callback=self.manual_callback)
        changed = False
        for key, var in self.class_vars.items():
            val = var.get().strip()
            if val:
                cm.set(key, val)
                changed = True
        if changed:
            cm.save()

        # 解析间隔范围
        try:
            min_interval = int(self.var_min_interval.get())
            max_interval = int(self.var_max_interval.get())
            if min_interval < 1 or max_interval < min_interval:
                raise ValueError
        except ValueError:
            messagebox.showwarning("间隔错误", "请正确填写爬取间隔(最小>=1,最大>=最小)")
            return

        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="运行中...", foreground="orange")
        self._append_log("=" * 60)
        self._append_log(f"开始任务: 博主={name}({uid}), 时间={start} ~ {end}")

        kwargs = {
            "user_id": uid,
            "user_name": name,
            "start_date": start,
            "end_date": end,
            "headless": self.var_headless.get(),
            "user_data_dir": self.var_userdata.get().strip() or None,
            "manual_callback": self.manual_callback,
            "wait_callback": self.wait_callback,
            "data_root": None,
            "keep_browser_open": self.var_keep_browser.get(),
            "skip_export": self.var_skip_export.get(),
            "min_interval": min_interval,
            "max_interval": max_interval,
            "download_images": self.var_download_images.get(),
            "download_videos": self.var_download_videos.get(),
            "export_format": self.var_export_format.get(),
        }

        self.worker = threading.Thread(target=self._run_worker, args=(kwargs,), daemon=True)
        self.worker.start()

    def _run_worker(self, kwargs):
        try:
            result = run_task(**kwargs)
            if result.get("error"):
                final_msg = (
                    f"\n任务未完成: {result['error']}\n"
                    f"收集到微博: {len(result['weibo_ids'])} 条"
                )
            else:
                final_msg = (
                    f"\n任务完成: 收集 {len(result['weibo_ids'])} 条微博, "
                    f"导出成功 {result['exported']} 条, 失败 {result['failed']} 条\n"
                    f"ID文件: {result.get('txt_file') or '无'}\n"
                    f"MD目录: {result.get('md_dir') or '无'}"
                )
            self.root.after(0, self._on_finish, final_msg)
        except Exception as e:
            logger.error(f"任务异常: {e}", exc_info=True)
            self.root.after(0, self._on_finish, f"\n任务异常终止: {e}")

    def _on_finish(self, msg):
        self._append_log(msg)
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        is_error = "任务未完成" in msg or "任务异常" in msg
        self.lbl_status.configure(text="未完成" if is_error else "完成",
                                  foreground="red" if is_error else "green")
        messagebox.showinfo("任务未完成" if is_error else "任务完成", msg)

    def _stop(self):
        if self.running:
            # 无法安全中止爬虫线程,提示用户
            messagebox.showinfo(
                "停止",
                "当前版本不支持安全中止正在运行的爬虫,请等待当前步骤结束。\n"
                "如需强制退出,请直接关闭本窗口(浏览器会被释放)。")
            self.lbl_status.configure(text="提示: 等待当前步骤结束", foreground="red")

    def _on_close(self):
        self.running = False
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    root = tk.Tk()
    app = WeiboCrawlerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
