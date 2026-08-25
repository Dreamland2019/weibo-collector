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
import re
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, scrolledtext, ttk

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


class HoverTip:
    """鼠标悬停在控件上显示提示气泡(tooltip)

    用法: HoverTip(widget, "提示文字")
    """

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text or ""
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event):
        if self.tip or not self.text:
            return
        try:
            x = event.x_root + 12
            y = event.y_root + 12
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            try:
                tip.attributes("-topmost", True)
            except Exception:
                pass
            lbl = ttk.Label(tip, text=self.text, background="#FFFFE1",
                            foreground="black", relief="solid", borderwidth=1,
                            padding=6, wraplength=420, justify="left")
            lbl.pack()
            self.tip = tip
        except Exception:
            self.tip = None

    def _hide(self, event):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


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
        self.root.title("微博长文收集记录整理器 v0.5.0")
        self.root.geometry("900x760")
        self.root.minsize(840, 680)

        self.log_queue = queue.Queue()
        self.manual_req_queue = queue.Queue()   # 后台线程 -> 主线程:请求手动输入类名
        self.manual_resp_queue = queue.Queue()  # 主线程 -> 后台线程:用户输入结果
        self.wait_req_queue = queue.Queue()     # 后台线程 -> 主线程:请求用户完成操作
        self.wait_resp_queue = queue.Queue()    # 主线程 -> 后台线程:用户已确认
        self.ai_progress_queue = queue.Queue()  # 后台线程 -> 主线程:AI分类进度
        self._hint_labels = []  # 灰色提示文字列表(随窗口宽度自动换行)
        self.worker = None
        self.running = False      # 爬取任务运行中
        self.ai_running = False   # AI分类任务运行中(与爬取互斥)
        self.settings = self._load_settings()

        self._build_ui()
        # 应用上次保存的设置(日期等);首次使用保持为空
        self._apply_settings()
        # 恢复上次保存的窗口大小(用户调整过的话)
        self._apply_window_size()

        # 日志 handler
        ensure_logger()
        self.log_handler = LogQueueHandler(self.log_queue)
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger('weibo_crawler').addHandler(self.log_handler)

        # 窗口宽度变化时,让灰色提示文字自动换行(无需手动拉宽窗口)
        self.root.bind("<Configure>", self._update_hint_wrap)
        # 首次打开/切换页签后,布局完成时补触发几次(仅靠 Configure 事件,
        # 窗口不拖动时可能永远不触发,导致提示一直挤在行尾)
        self.root.bind("<<NotebookTabChanged>>",
                       lambda e: self.root.after(80, self._update_hint_wrap))
        self.root.after(120, self._update_hint_wrap)
        self.root.after(600, self._update_hint_wrap)

        # 轮询队列刷新界面
        self._poll_queues()

        # 窗口关闭时清理
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 设置持久化(记住上次所有设置) ----------

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

    def _collect_settings(self):
        """收集当前界面所有设置项(保存用)"""
        data = {
            # 任务参数
            "name": self.var_name.get(),
            "uid": self.var_uid.get(),
            # 日期
            "start_year": self.var_start_year.get(),
            "start_month": self.var_start_month.get(),
            "start_day": self.var_start_day.get(),
            "end_year": self.var_end_year.get(),
            "end_month": self.var_end_month.get(),
            "end_day": self.var_end_day.get(),
            # 高级设置
            "headless": self.var_headless.get(),
            "userdata": self.var_userdata.get(),
            "keep_browser": self.var_keep_browser.get(),
            "skip_export": self.var_skip_export.get(),
            "min_interval": self.var_min_interval.get(),
            "max_interval": self.var_max_interval.get(),
            "download_images": self.var_download_images.get(),
            "download_videos": self.var_download_videos.get(),
            "export_format": self.var_export_format.get(),
            "skip_existing": self.var_skip_existing.get(),
            "min_words": self.var_min_words.get(),
            "ai_enabled": self.var_ai_enabled.get(),
            "ai_rename": self.var_ai_rename.get(),
        }
        # 筛选页设置
        if hasattr(self, "f_var_name"):
            data.update({
                "f_name": self.f_var_name.get(),
                "f_uid": self.f_var_uid.get(),
                "f_start_year": self.f_var_start_year.get(),
                "f_start_month": self.f_var_start_month.get(),
                "f_start_day": self.f_var_start_day.get(),
                "f_end_year": self.f_var_end_year.get(),
                "f_end_month": self.f_var_end_month.get(),
                "f_end_day": self.f_var_end_day.get(),
                "f_sort_mode": self.f_var_sort_mode.get(),
                "f_use_repost": self.f_var_use_repost.get(),
                "f_use_comment": self.f_var_use_comment.get(),
                "f_use_like": self.f_var_use_like.get(),
                "f_top": self.f_var_top.get(),
                "f_format": self.f_var_format.get(),
                "f_move": self.f_var_move.get(),
                "f_filter_root": self.f_var_filter_root.get(),
                "f_source": self.f_var_source.get(),
                "f_auto_open": self.f_var_auto_open.get(),
            })
        # AI筛选页设置(仅表单字段;API/阈值/提示词存 ai_config.json,不重复保存)
        if hasattr(self, "a_var_name"):
            data.update({
                "a_name": self.a_var_name.get(),
                "a_uid": self.a_var_uid.get(),
                "a_start_year": self.a_var_start_year.get(),
                "a_start_month": self.a_var_start_month.get(),
                "a_start_day": self.a_var_start_day.get(),
                "a_end_year": self.a_var_end_year.get(),
                "a_end_month": self.a_var_end_month.get(),
                "a_end_day": self.a_var_end_day.get(),
                "a_format": self.a_var_format.get(),
                "a_filter_root": self.a_var_filter_root.get(),
                "a_summary": self.a_var_summary.get(),
                "a_keep_source": self.a_var_keep_source.get(),
                "a_keep_original": self.a_var_keep_original.get(),
            })
        # 类名覆盖
        for key, var in self.class_vars.items():
            data[f"class_{key}"] = var.get()
        return data

    def _save_settings(self):
        """保存当前所有设置到文件"""
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(self._collect_settings(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存设置失败: {e}")

    def _apply_settings(self):
        """将已加载的设置应用到界面控件(在 _build_ui 之后调用)"""
        st = self.settings
        if not st:
            return
        self.var_name.set(st.get("name", self.var_name.get()))
        self.var_uid.set(st.get("uid", self.var_uid.get()))
        self.var_headless.set(bool(st.get("headless", False)))
        self.var_userdata.set(st.get("userdata", self.var_userdata.get()))
        self.var_keep_browser.set(bool(st.get("keep_browser", False)))
        self.var_skip_export.set(bool(st.get("skip_export", False)))
        self.var_min_interval.set(st.get("min_interval", self.var_min_interval.get()))
        self.var_max_interval.set(st.get("max_interval", self.var_max_interval.get()))
        self.var_download_images.set(bool(st.get("download_images", False)))
        self.var_download_videos.set(bool(st.get("download_videos", False)))
        self.var_export_format.set(st.get("export_format", self.var_export_format.get()))
        self.var_skip_existing.set(bool(st.get("skip_existing", False)))
        self.var_min_words.set(st.get("min_words", self.var_min_words.get()))
        self.var_ai_enabled.set(bool(st.get("ai_enabled", False)))
        self.var_ai_rename.set(bool(st.get("ai_rename", False)))
        # 筛选页设置恢复
        if hasattr(self, "f_var_name"):
            self.f_var_name.set(st.get("f_name", self.f_var_name.get()))
            self.f_var_uid.set(st.get("f_uid", self.f_var_uid.get()))
            self.f_var_start_year.set(st.get("f_start_year", self.f_var_start_year.get()))
            self.f_var_start_month.set(st.get("f_start_month", self.f_var_start_month.get()))
            self.f_var_start_day.set(st.get("f_start_day", self.f_var_start_day.get()))
            self.f_var_end_year.set(st.get("f_end_year", self.f_var_end_year.get()))
            self.f_var_end_month.set(st.get("f_end_month", self.f_var_end_month.get()))
            self.f_var_end_day.set(st.get("f_end_day", self.f_var_end_day.get()))
            self.f_var_sort_mode.set(st.get("f_sort_mode", self.f_var_sort_mode.get()))
            self.f_var_use_repost.set(bool(st.get("f_use_repost", True)))
            self.f_var_use_comment.set(bool(st.get("f_use_comment", True)))
            self.f_var_use_like.set(bool(st.get("f_use_like", True)))
            self.f_var_top.set(st.get("f_top", self.f_var_top.get()))
            self.f_var_format.set(st.get("f_format", self.f_var_format.get()))
            self.f_var_move.set(bool(st.get("f_move", False)))
            self.f_var_filter_root.set(st.get("f_filter_root", self.f_var_filter_root.get()))
            self.f_var_source.set(st.get("f_source", self.f_var_source.get()))
            self.f_var_auto_open.set(bool(st.get("f_auto_open", False)))
        # AI筛选页设置恢复(仅恢复表单字段;API/阈值/提示词以 ai_config.json 为准)
        if hasattr(self, "a_var_name"):
            self.a_var_name.set(st.get("a_name", self.a_var_name.get()))
            self.a_var_uid.set(st.get("a_uid", self.a_var_uid.get()))
            self.a_var_start_year.set(st.get("a_start_year", self.a_var_start_year.get()))
            self.a_var_start_month.set(st.get("a_start_month", self.a_var_start_month.get()))
            self.a_var_start_day.set(st.get("a_start_day", self.a_var_start_day.get()))
            self.a_var_end_year.set(st.get("a_end_year", self.a_var_end_year.get()))
            self.a_var_end_month.set(st.get("a_end_month", self.a_var_end_month.get()))
            self.a_var_end_day.set(st.get("a_end_day", self.a_var_end_day.get()))
            self.a_var_format.set(st.get("a_format", self.a_var_format.get()))
            self.a_var_filter_root.set(st.get("a_filter_root", self.a_var_filter_root.get()))
            self.a_var_summary.set(bool(st.get("a_summary", True)))
            self.a_var_keep_source.set(bool(st.get("a_keep_source", True)))
            self.a_var_keep_original.set(bool(st.get("a_keep_original", False)))
        for key, var in self.class_vars.items():
            val = st.get(f"class_{key}", "")
            if val:
                var.set(val)
        # 恢复数据源下拉选项(保存的AI数据源目录可能已不存在)
        if hasattr(self, "f_source_combo"):
            self._refresh_source_options()

    # ---------- UI 构建 ----------

    def _hint(self, parent, text, pack=None, own=False, **label_kw):
        """创建灰色提示文字

        - own=False(默认): 跟随行内布局(pack 传入的参数)
        - own=True: 独占一行(Tk pack 同容器混用 side 会挤在同一行,
          所以长提示必须放在"外层容器"里并 pack(fill=x),自然排到下一行)
        """
        lbl = ttk.Label(parent, text=text, foreground="gray", **label_kw)
        self._hint_labels.append(lbl)
        lbl._hint_own_row = False
        if own:
            lbl.pack(fill="x", anchor="w")
            lbl._hint_own_row = True
        elif pack:
            lbl.pack(**pack)
        return lbl

    def _hint_icon(self, parent, text, pack=None):
        """行尾的 ⓘ 悬浮提示图标(鼠标移上去显示长提示)

        长提示不再挤占界面空间,短提示/重要提示仍用 _hint 直接显示;
        每个分号后自动换行,避免提示文字堆在一起
        """
        text = (text or "").replace(";", ";\n").replace(";", ";\n")
        lbl = ttk.Label(parent, text="ⓘ", foreground="#1a73e8",
                        cursor="hand2", font=("", 10))
        lbl._tip_text = text
        HoverTip(lbl, text)
        self._hint_labels.append(lbl)  # 无需换行管理,仅登记
        if pack:
            lbl.pack(**pack)
        return lbl

    def _update_hint_wrap(self, event=None):
        """窗口/内框宽度变化时,更新所有灰色提示的换行宽度

        - 独立行提示(长提示): 按父容器宽度换行
        - 行内提示(短提示): 按行内剩余空间换行
        """
        try:
            for lbl in self._hint_labels:
                try:
                    if not lbl.winfo_exists():
                        continue
                    parent = lbl.master
                    pw = parent.winfo_width()
                    if pw < 60:
                        continue
                    if getattr(lbl, "_hint_own_row", False):
                        w = pw - 16
                    else:
                        w = pw - lbl.winfo_x() - 6
                    if w > 60:
                        lbl.configure(wraplength=w)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _year_options():
        """年份选项: 10 年前 ~ 明年(动态,跨年后自动包含新年份)"""
        now = datetime.now()
        return [str(y) for y in range(now.year - 10, now.year + 2)]

    def _build_date_picker(self, parent, year_var, month_var, day_var, _unused=None,
                           reset_day_on_month=False):
        """构建年/月/日选择器;年份可手动输入(10年前~明年),月/日下拉

        reset_day_on_month=True 时,切换月份将日期重置为 1 日(用于开始日期)
        """
        years = self._year_options()
        months = [f"{m:02d}" for m in range(1, 13)]
        y_cb = ttk.Combobox(parent, textvariable=year_var, values=years,
                            width=6)  # 不设 readonly,允许手动输入任意年份
        y_cb.pack(side="left")
        ttk.Label(parent, text="年").pack(side="left")
        m_cb = ttk.Combobox(parent, textvariable=month_var, values=months,
                            width=4, state="readonly")
        m_cb.pack(side="left")
        ttk.Label(parent, text="月").pack(side="left")
        d_cb = ttk.Combobox(parent, textvariable=day_var,
                            width=4, state="readonly")
        d_cb.pack(side="left")
        ttk.Label(parent, text="日").pack(side="left", padx=(0, 4))

        def on_month_change(event=None):
            if reset_day_on_month:
                # 开始日期: 切换月份时日期重置为 1 日
                day_var.set("01")
            self._refresh_day_options(year_var, month_var, day_var, d_cb)

        def on_year_change(event=None):
            self._refresh_day_options(year_var, month_var, day_var, d_cb)

        m_cb.bind("<<ComboboxSelected>>", on_month_change)
        y_cb.bind("<<ComboboxSelected>>", on_year_change)
        y_cb.bind("<FocusOut>", on_year_change)
        year_var.trace_add("write", lambda *a: self._refresh_day_options(
            year_var, month_var, day_var, d_cb))
        self._refresh_day_options(year_var, month_var, day_var, d_cb)
        return d_cb

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 主容器: Notebook 三页签(爬取 / 数据筛选 / AI分类)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self.tab_crawl = ttk.Frame(self.notebook)
        self.tab_filter = ttk.Frame(self.notebook)
        self.tab_ai = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_crawl, text="  爬取  ")
        self.notebook.add(self.tab_filter, text="  数据筛选  ")
        self.notebook.add(self.tab_ai, text="  AI分类  ")

        # ============ 页签1: 爬取 ============
        t = self.tab_crawl

        # 顶部:参数输入
        frame_top = ttk.LabelFrame(t, text="任务参数", padding=10)
        frame_top.pack(fill="x", padx=10, pady=(10, 4))

        row1 = ttk.Frame(frame_top)
        row1.pack(fill="x")
        ttk.Label(row1, text="博主昵称:").pack(side="left")
        self.var_name = tk.StringVar(value="卢诗翰")
        self.name_combo = ttk.Combobox(row1, textvariable=self.var_name, width=18)
        self.name_combo.pack(side="left", padx=(4, 8))
        ttk.Label(row1, text="微博ID:").pack(side="left")
        self.var_uid = tk.StringVar(value="3276099007")
        ttk.Entry(row1, textvariable=self.var_uid, width=18).pack(side="left", padx=4)
        # 打开/刷新博主记录文件
        ttk.Button(row1, text="打开博主记录", command=self._open_blogger_record).pack(side="left", padx=(8, 2))
        ttk.Button(row1, text="刷新", command=lambda: self._reload_blogger_names(
            self.name_combo, self.var_uid, self.var_name)).pack(side="left")
        self._load_blogger_names(self.name_combo, self.var_uid, self.var_name)

        row2 = ttk.Frame(frame_top)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="开始日期:").pack(side="left")
        st = self.settings
        self.var_start_year = tk.StringVar(value=st.get("start_year", ""))
        self.var_start_month = tk.StringVar(value=st.get("start_month", ""))
        self.var_start_day = tk.StringVar(value=st.get("start_day", ""))
        self._build_date_picker(row2, self.var_start_year, self.var_start_month,
                                self.var_start_day, None, reset_day_on_month=True)
        ttk.Label(row2, text="  结束日期:").pack(side="left")
        self.var_end_year = tk.StringVar(value=st.get("end_year", ""))
        self.var_end_month = tk.StringVar(value=st.get("end_month", ""))
        self.var_end_day = tk.StringVar(value=st.get("end_day", ""))
        self._build_date_picker(row2, self.var_end_year, self.var_end_month,
                                self.var_end_day, None)
        ttk.Button(row2, text="打开月份文件夹",
                   command=lambda: self._open_month_dir(
                       self.var_name.get(), self.var_uid.get(),
                       self.var_start_year, self.var_start_month)
                   ).pack(side="left", padx=(10, 0))
        self._hint_icon(row2, "年份可手动输入(10年前~明年);切换月份自动调整日期选项",
                        pack=dict(side="left", padx=4))

        # 日期行下方的建议提示小字
        row2tip = ttk.Frame(frame_top)
        row2tip.pack(fill="x", pady=(2, 0))
        self._hint(row2tip, "建议先按 1 个月范围并显示浏览器进行爬取试验,再逐步扩大范围。",
                   pack=dict(anchor="w"), font=("", 9))
        # 任务参数区: 常用选项(下载图片/视频、导出格式、跳过已爬取、无头模式)
        row2b = ttk.Frame(frame_top)
        row2b.pack(fill="x", pady=(8, 0))
        self.var_download_images = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2b, text="下载图片", variable=self.var_download_images).pack(side="left")
        self.var_download_videos = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2b, text="下载视频", variable=self.var_download_videos).pack(side="left", padx=10)
        ttk.Label(row2b, text="导出格式:").pack(side="left", padx=(10, 0))
        self.var_export_format = tk.StringVar(value="md")
        ttk.Combobox(row2b, textvariable=self.var_export_format, values=["md", "docx"],
                     width=6, state="readonly").pack(side="left")

        row2c = ttk.Frame(frame_top)
        row2c.pack(fill="x", pady=(6, 0))
        self.var_skip_existing = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2c, text="跳过已爬取的文章(按微博ID去重)",
                        variable=self.var_skip_existing).pack(side="left")
        self.var_headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2c, text="无头模式(不显示浏览器)", variable=self.var_headless).pack(side="left")
        self._hint_icon(row2c, "重新爬取时,已存在同ID文件的不再抓取(增量补下载媒体)",
                        pack=dict(side="left", padx=4))

        # AI 实时判断(任务参数最后一行;API 在"AI筛选"页签配置)
        row2d = ttk.Frame(frame_top)
        row2d.pack(fill="x", pady=(6, 0))
        self.var_ai_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2d, text="启用AI实时判断",
                        variable=self.var_ai_enabled).pack(side="left")
        self.var_ai_rename = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2d, text="AI总结并重命名标题",
                        variable=self.var_ai_rename).pack(side="left", padx=(6, 0))
        self.btn_ai_test = ttk.Button(row2d, text="测试连接",
                                      command=lambda: self._test_ai_connection(self.btn_ai_test))
        self.btn_ai_test.pack(side="left", padx=8)
        self._hint_icon(row2d, "启用AI后:逐篇调用AI判断“高质量可信度”,低于阈值不导出也不下载媒体;"
                        "勾选“AI总结并重命名标题”后,通过的文章会复制重命名到“AI分类”目录"
                        "(如 26-1-22_标题.md,原文件保留);API配置在“AI分类”页签",
                        pack=dict(side="left", padx=4))

        # 高级设置
        frame_adv = ttk.LabelFrame(t, text="高级设置", padding=10)
        frame_adv.pack(fill="x", padx=10, pady=4)

        row3 = ttk.Frame(frame_adv)
        row3.pack(fill="x")
        ttk.Label(row3, text="用户数据目录:").pack(side="left")
        self.var_userdata = tk.StringVar(value=DEFAULT_USER_DATA_DIR)
        ttk.Entry(row3, textvariable=self.var_userdata, width=60).pack(side="left", padx=4)

        row4 = ttk.Frame(frame_adv)
        row4.pack(fill="x", pady=(6, 0))
        self.var_keep_browser = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="完成后保留浏览器", variable=self.var_keep_browser).pack(side="left")
        self.var_skip_export = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="只收集ID不导出MD", variable=self.var_skip_export).pack(side="left", padx=10)

        # 爬取间隔可自定义
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
        ttk.Label(row4b, text="秒").pack(side="left")
        self._hint(row4b, " (勿设置过短,避免触发风控)",
                   pack=dict(side="left", padx=4))

        # 最低正文字数过滤(0=不限制)
        row4e = ttk.Frame(frame_adv)
        row4e.pack(fill="x", pady=(6, 0))
        ttk.Label(row4e, text="最低正文字数:").pack(side="left")
        self.var_min_words = tk.StringVar(value="0")
        ttk.Spinbox(row4e, from_=0, to=100000, textvariable=self.var_min_words,
                    width=6).pack(side="left", padx=4)
        ttk.Label(row4e, text="字符").pack(side="left")
        self._hint_icon(row4e, "正文低于该字数的文章不导出;0=不限制;"
                        "设置后列表页预览不足该字数的微博不再进入详情页",
                        pack=dict(side="left", padx=4))

        # 类名覆盖
        row5 = ttk.Frame(frame_adv)
        row5.pack(fill="x", pady=(6, 0))
        self._hint(row5, "类名覆盖(留空则自动识别):", pack=dict(anchor="w"))
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
        frame_btn = ttk.Frame(t)
        frame_btn.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(frame_btn, text="开始爬取", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frame_btn, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(frame_btn, text="就绪", foreground="green")
        self.lbl_status.pack(side="left", padx=12)

        # ============ 页签2: 筛选 ============
        f = self.tab_filter
        frame_f1 = ttk.LabelFrame(f, text="筛选条件", padding=10)
        frame_f1.pack(fill="x", padx=10, pady=(10, 4))

        frow1 = ttk.Frame(frame_f1)
        frow1.pack(fill="x")
        ttk.Label(frow1, text="博主昵称:").pack(side="left")
        self.f_var_name = tk.StringVar(value="卢诗翰")
        self.f_name_combo = ttk.Combobox(frow1, textvariable=self.f_var_name, width=18)
        self.f_name_combo.pack(side="left", padx=(4, 8))
        ttk.Label(frow1, text="微博ID:").pack(side="left")
        self.f_var_uid = tk.StringVar(value="3276099007")
        ttk.Entry(frow1, textvariable=self.f_var_uid, width=18).pack(side="left", padx=4)
        ttk.Button(frow1, text="打开博主记录", command=self._open_blogger_record).pack(side="left", padx=(8, 2))
        ttk.Button(frow1, text="刷新", command=lambda: self._reload_blogger_names(
            self.f_name_combo, self.f_var_uid, self.f_var_name)).pack(side="left")
        self._load_blogger_names(self.f_name_combo, self.f_var_uid, self.f_var_name)

        frow2 = ttk.Frame(frame_f1)
        frow2.pack(fill="x", pady=(8, 0))
        ttk.Label(frow2, text="开始日期:").pack(side="left")
        self.f_var_start_year = tk.StringVar(value="")
        self.f_var_start_month = tk.StringVar(value="")
        self.f_var_start_day = tk.StringVar(value="")
        self._build_date_picker(frow2, self.f_var_start_year, self.f_var_start_month,
                                self.f_var_start_day, None, reset_day_on_month=True)
        ttk.Label(frow2, text="  结束日期:").pack(side="left")
        self.f_var_end_year = tk.StringVar(value="")
        self.f_var_end_month = tk.StringVar(value="")
        self.f_var_end_day = tk.StringVar(value="")
        self._build_date_picker(frow2, self.f_var_end_year, self.f_var_end_month,
                                self.f_var_end_day, None)
        ttk.Button(frow2, text="打开月份文件夹",
                   command=lambda: self._open_month_dir(
                       self.f_var_name.get(), self.f_var_uid.get(),
                       self.f_var_start_year, self.f_var_start_month)
                   ).pack(side="left", padx=(10, 0))

        # 已有月份选择(方案A): 程序读取该博主已爬取的月份目录,选起止月自动填日期
        frow2b = ttk.Frame(frame_f1)
        frow2b.pack(fill="x", pady=(4, 0))
        ttk.Label(frow2b, text="已有月份:").pack(side="left")
        ttk.Label(frow2b, text="从").pack(side="left", padx=(4, 0))
        self.f_var_month_from = tk.StringVar(value="")
        self.f_month_from_combo = ttk.Combobox(frow2b, textvariable=self.f_var_month_from,
                                               width=10, state="readonly")
        self.f_month_from_combo.pack(side="left", padx=2)
        ttk.Label(frow2b, text="到").pack(side="left")
        self.f_var_month_to = tk.StringVar(value="")
        self.f_month_to_combo = ttk.Combobox(frow2b, textvariable=self.f_var_month_to,
                                             width=10, state="readonly")
        self.f_month_to_combo.pack(side="left", padx=2)
        ttk.Button(frow2b, text="刷新月份",
                   command=self._load_month_options).pack(side="left", padx=6)
        self._hint_icon(frow2b, "自动读取该博主已爬取的月份,选择后自动填充日期范围;"
                        "也可继续手动修改上面的日期",
                        pack=dict(side="left", padx=4))

        def _on_month_from(event=None):
            m = re.match(r"(\d{4})年(\d{1,2})月", self.f_var_month_from.get() or "")
            if m:
                self.f_var_start_year.set(m.group(1))
                self.f_var_start_month.set(f"{int(m.group(2)):02d}")
                self.f_var_start_day.set("01")

        def _on_month_to(event=None):
            m = re.match(r"(\d{4})年(\d{1,2})月", self.f_var_month_to.get() or "")
            if m:
                _, end = self._month_range(m.group(1), f"{int(m.group(2)):02d}")
                y, mo, d = end.split("-")
                self.f_var_end_year.set(y)
                self.f_var_end_month.set(mo)
                self.f_var_end_day.set(d)

        self.f_month_from_combo.bind("<<ComboboxSelected>>", _on_month_from)
        self.f_month_to_combo.bind("<<ComboboxSelected>>", _on_month_to)
        # 博主昵称/ID变化时刷新月份选项
        self.f_var_uid.trace_add("write", lambda *a: self._load_month_options())

        frow3 = ttk.Frame(frame_f1)
        frow3.pack(fill="x", pady=(8, 0))
        ttk.Label(frow3, text="排序方式:").pack(side="left")
        self.f_var_sort_mode = tk.StringVar(value="stats")
        ttk.Radiobutton(frow3, text="转评赞之和", variable=self.f_var_sort_mode,
                        value="stats").pack(side="left", padx=(6, 0))
        ttk.Radiobutton(frow3, text="正文字数", variable=self.f_var_sort_mode,
                        value="word_count").pack(side="left", padx=6)
        ttk.Label(frow3, text="  输出篇数:").pack(side="left", padx=(16, 0))
        self.f_var_top = tk.StringVar(value="10")
        ttk.Spinbox(frow3, from_=1, to=500, textvariable=self.f_var_top,
                    width=5).pack(side="left", padx=4)
        ttk.Label(frow3, text="篇").pack(side="left")

        # 转评赞子指标(仅"转评赞之和"模式生效)
        frow3b = ttk.Frame(frame_f1)
        frow3b.pack(fill="x", pady=(4, 0))
        ttk.Label(frow3b, text="  计入指标:").pack(side="left")
        self.f_var_use_repost = tk.BooleanVar(value=True)
        ttk.Checkbutton(frow3b, text="转发", variable=self.f_var_use_repost).pack(side="left", padx=(6, 0))
        self.f_var_use_comment = tk.BooleanVar(value=True)
        ttk.Checkbutton(frow3b, text="评论", variable=self.f_var_use_comment).pack(side="left", padx=6)
        self.f_var_use_like = tk.BooleanVar(value=True)
        ttk.Checkbutton(frow3b, text="点赞", variable=self.f_var_use_like).pack(side="left", padx=6)
        self._hint(frow3b, "  (选择\"正文字数\"时此三项忽略)",
                   pack=dict(side="left", padx=6))

        frow4 = ttk.Frame(frame_f1)
        frow4.pack(fill="x", pady=(8, 0))
        ttk.Label(frow4, text="数据格式:").pack(side="left")
        self.f_var_format = tk.StringVar(value="md")
        ttk.Combobox(frow4, textvariable=self.f_var_format, values=["md", "docx"],
                     width=6, state="readonly").pack(side="left", padx=4)
        self.f_var_move = tk.BooleanVar(value=False)
        ttk.Checkbutton(frow4, text="移动原文件(默认复制)", variable=self.f_var_move).pack(side="left", padx=16)
        self.f_var_filter_root = tk.StringVar(value="筛选")
        ttk.Label(frow4, text="输出文件夹:").pack(side="left", padx=(16, 0))
        ttk.Entry(frow4, textvariable=self.f_var_filter_root, width=14).pack(side="left", padx=4)
        self._hint(frow4, "(在程序目录下)", pack=dict(side="left"))

        # 数据源: 爬取数据(DataPC) 或 AI分类结果(目录存在时显示)
        frow4b = ttk.Frame(frame_f1)
        frow4b.pack(fill="x", pady=(4, 0))
        ttk.Label(frow4b, text="数据源:").pack(side="left")
        self.f_var_source = tk.StringVar(value="DataPC")
        self.f_source_combo = ttk.Combobox(frow4b, textvariable=self.f_var_source,
                                           width=18, state="readonly")
        self.f_source_combo.pack(side="left", padx=4)
        self._f_source_display_map = {"DataPC": "爬取数据(DataPC)"}
        self._hint_icon(frow4b, "选择AI分类结果时,从“AI分类”或“筛选”目录下的 AI_博主_类别 中筛选",
                        pack=dict(side="left", padx=4))

        def on_source_select(event):
            disp = self.f_var_source.get()
            for key, d in self._f_source_display_map.items():
                if d == disp:
                    self.f_var_source.set(key)
                    return

        self.f_source_combo.bind("<<ComboboxSelected>>", on_source_select)
        self.f_var_filter_root.trace_add(
            "write", lambda *a: self._refresh_source_options())
        self._refresh_source_options()

        # 最后一行: 筛选后自动打开输出文件夹 + 打开当前筛选任务文件夹
        frow5 = ttk.Frame(frame_f1)
        frow5.pack(fill="x", pady=(4, 0))
        self.f_var_auto_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(frow5, text="筛选完成后自动打开输出文件夹",
                        variable=self.f_var_auto_open).pack(side="left")
        ttk.Button(frow5, text="打开当前筛选任务文件夹",
                   command=self._open_current_filter_dir).pack(side="left", padx=10)

        frame_fbtn = ttk.Frame(f)
        frame_fbtn.pack(fill="x", padx=10, pady=8)
        self.btn_filter = ttk.Button(frame_fbtn, text="开始筛选", command=self._start_filter)
        self.btn_filter.pack(side="left")
        self._hint(f, " 说明: 对本地已爬取数据按所选指标之和排序,取前N篇复制到输出文件夹,"
                      "文件名加排名序号;输出结果按 博主名_ID 分目录存放", own=True)

        # ============ 页签3: AI筛选 ============
        self._build_ai_tab()

        # ============ 日志区(三个页签共用) ============
        frame_log = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        frame_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        log_btns = ttk.Frame(frame_log)
        log_btns.pack(side="right", fill="y", padx=(0, 4))
        self.btn_open_data = ttk.Button(log_btns, text="打开数据目录",
                                        command=self._open_data_root)
        self.btn_open_filter = ttk.Button(log_btns, text="打开筛选目录",
                                          command=self._open_filter_root_dir)
        self.btn_open_ai = ttk.Button(log_btns, text="打开AI分类目录",
                                      command=self._open_ai_root_dir)
        self.btn_export_log = ttk.Button(log_btns, text="导出日志",
                                         command=self._export_log)
        for i, btn in enumerate((self.btn_open_data, self.btn_open_filter,
                                 self.btn_open_ai, self.btn_export_log)):
            btn.pack(side="top", fill="x", pady=1)
        self.txt_log = scrolledtext.ScrolledText(frame_log, height=14, state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True)

    # ---------- AI筛选页签 ----------

    def _build_ai_tab(self):
        """构建"AI筛选"页签: API设置 / 判断阈值 / AI提示词 / 事后AI分类"""
        # AI页签内容较多,包一层可滚动画布
        self.ai_canvas = tk.Canvas(self.tab_ai, highlightthickness=0)
        self.ai_scroll = ttk.Scrollbar(self.tab_ai, orient="vertical",
                                       command=self.ai_canvas.yview)
        self.ai_inner = ttk.Frame(self.ai_canvas)
        self.ai_inner.bind(
            "<Configure>",
            lambda e: self.ai_canvas.configure(
                scrollregion=self.ai_canvas.bbox("all")))
        self._ai_window_item = self.ai_canvas.create_window(
            (0, 0), window=self.ai_inner, anchor="nw")
        self.ai_canvas.configure(yscrollcommand=self.ai_scroll.set)
        self.ai_canvas.pack(side="left", fill="both", expand=True)
        self.ai_scroll.pack(side="right", fill="y")

        def _on_canvas_resize(event):
            # 让内框宽度跟随画布(否则内容会撑出可视区域,提示文字被右缘截断)
            try:
                self.ai_canvas.itemconfigure(self._ai_window_item,
                                             width=event.width)
            except Exception:
                pass
            self._update_hint_wrap()

        self.ai_canvas.bind("<Configure>", _on_canvas_resize)

        def _on_enter(_e):
            self.ai_canvas.bind_all("<MouseWheel>", _on_wheel)

        def _on_leave(_e):
            self.ai_canvas.unbind_all("<MouseWheel>")

        def _on_wheel(event):
            self.ai_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.ai_canvas.bind("<Enter>", _on_enter)
        self.ai_canvas.bind("<Leave>", _on_leave)

        a = self.ai_inner

        # ---- API设置 ----
        frame_api = ttk.LabelFrame(a, text="API设置(OpenAI兼容接口)", padding=10)
        frame_api.pack(fill="x", padx=10, pady=(10, 4))

        # API/阈值/提示词以 ai_config.json 为准(不存在时用默认值)
        from weibo_ai import AIConfig, DEFAULT_SYSTEM_PROMPT
        _ai_cfg = AIConfig()

        api_row1 = ttk.Frame(frame_api)
        api_row1.pack(fill="x")
        ttk.Label(api_row1, text="API Key:").pack(side="left")
        self.a_var_api_key = tk.StringVar(value=_ai_cfg.get("api_key", ""))
        self.a_entry_key = ttk.Entry(api_row1, textvariable=self.a_var_api_key,
                                     width=40, show="*")
        self.a_entry_key.pack(side="left", padx=4)
        self.a_btn_show_key = ttk.Button(api_row1, text="显示", width=5,
                                         command=self._a_toggle_key)
        self.a_btn_show_key.pack(side="left")
        self.a_btn_save = ttk.Button(api_row1, text="保存配置",
                                     command=self._a_save_config)
        self.a_btn_save.pack(side="left", padx=8)
        self._hint_icon(api_row1, "DeepSeek/通义/Kimi等OpenAI兼容服务均可;"
                        "密钥仅保存在程序目录 ai_config.json,请妥善保管",
                        pack=dict(side="left", padx=4))

        api_row2 = ttk.Frame(frame_api)
        api_row2.pack(fill="x", pady=(6, 0))
        ttk.Label(api_row2, text="接口地址:").pack(side="left")
        self.a_var_base_url = tk.StringVar(
            value=_ai_cfg.get("base_url", "https://api.deepseek.com"))
        ttk.Entry(api_row2, textvariable=self.a_var_base_url,
                  width=32).pack(side="left", padx=4)
        ttk.Label(api_row2, text="模型:").pack(side="left", padx=(12, 0))
        self.a_var_model = tk.StringVar(
            value=_ai_cfg.get("model", "deepseek-v4-flash"))
        ttk.Entry(api_row2, textvariable=self.a_var_model,
                  width=22).pack(side="left", padx=4)
        self.a_btn_test = ttk.Button(api_row2, text="测试连接",
                                     command=self._a_test_connection)
        self.a_btn_test.pack(side="left", padx=8)

        # ---- 判断阈值 ----
        frame_thr = ttk.LabelFrame(a, text="判断阈值", padding=10)
        frame_thr.pack(fill="x", padx=10, pady=4)

        thr_row1 = ttk.Frame(frame_thr)
        thr_row1.pack(fill="x")
        ttk.Label(thr_row1, text="高质量可信度阈值:").pack(side="left")
        self.a_var_quality = tk.StringVar(
            value=str(_ai_cfg.get("quality_threshold", 80)))
        ttk.Spinbox(thr_row1, from_=0, to=100, textvariable=self.a_var_quality,
                    width=4).pack(side="left", padx=2)
        ttk.Label(thr_row1, text="%").pack(side="left")
        self._hint(frame_thr, "   (爬取页启用AI实时判断时,低于此值不导出)", own=True)

        thr_row2 = ttk.Frame(frame_thr)
        thr_row2.pack(fill="x", pady=(4, 0))
        ttk.Label(thr_row2, text="广告概率阈值:").pack(side="left")
        self.a_var_ad = tk.StringVar(value=str(_ai_cfg.get("ad_threshold", 70)))
        ttk.Spinbox(thr_row2, from_=0, to=100, textvariable=self.a_var_ad,
                    width=4).pack(side="left", padx=2)
        ttk.Label(thr_row2, text="%   可疑下界:").pack(side="left", padx=(14, 0))
        self.a_var_susp = tk.StringVar(
            value=str(_ai_cfg.get("suspicious_low", 30)))
        ttk.Spinbox(thr_row2, from_=0, to=100, textvariable=self.a_var_susp,
                    width=4).pack(side="left", padx=2)
        ttk.Label(thr_row2, text="%").pack(side="left")
        self._hint_icon(thr_row2, "事后分类规则: 广告概率≥广告阈值→广告; "
                        "≥可疑下界→可疑; 高质量可信度≥阈值→高质量; 其余→其他低质量",
                        pack=dict(side="left", padx=4))

        # ---- AI提示词 ----
        frame_prompt = ttk.LabelFrame(a, text="AI提示词(可自定义,点击\"恢复默认\"还原)", padding=10)
        frame_prompt.pack(fill="x", padx=10, pady=4)
        self.a_txt_prompt = tk.Text(frame_prompt, height=5, wrap="word",
                                    font=("Microsoft YaHei UI", 9))
        self.a_txt_prompt.pack(fill="x")
        saved_prompt = (_ai_cfg.get("system_prompt") or "").strip()
        self.a_txt_prompt.insert("1.0", saved_prompt or DEFAULT_SYSTEM_PROMPT)
        self.a_btn_restore = ttk.Button(frame_prompt, text="恢复默认提示词",
                                        command=self._a_restore_prompts)
        self.a_btn_restore.pack(anchor="e", pady=(4, 0))

        # ---- 事后AI分类 ----
        frame_run = ttk.LabelFrame(a, text="事后AI分类(扫描本地文章→AI判断→按类复制到“AI分类/AI_博主_类别”)",
                                   padding=10)
        frame_run.pack(fill="x", padx=10, pady=4)

        run_row1 = ttk.Frame(frame_run)
        run_row1.pack(fill="x")
        ttk.Label(run_row1, text="博主昵称:").pack(side="left")
        self.a_var_name = tk.StringVar(value="卢诗翰")
        self.a_name_combo = ttk.Combobox(run_row1, textvariable=self.a_var_name, width=16)
        self.a_name_combo.pack(side="left", padx=(4, 8))
        ttk.Label(run_row1, text="微博ID:").pack(side="left")
        self.a_var_uid = tk.StringVar(value="3276099007")
        ttk.Entry(run_row1, textvariable=self.a_var_uid, width=14).pack(side="left", padx=4)
        ttk.Button(run_row1, text="打开博主记录",
                   command=self._open_blogger_record).pack(side="left", padx=(8, 2))
        ttk.Button(run_row1, text="刷新", command=lambda: self._reload_blogger_names(
            self.a_name_combo, self.a_var_uid, self.a_var_name)).pack(side="left")
        self._load_blogger_names(self.a_name_combo, self.a_var_uid, self.a_var_name)

        run_row2 = ttk.Frame(frame_run)
        run_row2.pack(fill="x", pady=(8, 0))
        ttk.Label(run_row2, text="开始日期:").pack(side="left")
        self.a_var_start_year = tk.StringVar(value="")
        self.a_var_start_month = tk.StringVar(value="")
        self.a_var_start_day = tk.StringVar(value="")
        self._build_date_picker(run_row2, self.a_var_start_year, self.a_var_start_month,
                                self.a_var_start_day, None, reset_day_on_month=True)
        ttk.Label(run_row2, text="  结束日期:").pack(side="left")
        self.a_var_end_year = tk.StringVar(value="")
        self.a_var_end_month = tk.StringVar(value="")
        self.a_var_end_day = tk.StringVar(value="")
        self._build_date_picker(run_row2, self.a_var_end_year, self.a_var_end_month,
                                self.a_var_end_day, None)

        # 数据格式/输出文件夹 单独一行(避免与日期行挤在一起)
        run_row2b = ttk.Frame(frame_run)
        run_row2b.pack(fill="x", pady=(4, 0))
        ttk.Label(run_row2b, text="数据格式:").pack(side="left")
        self.a_var_format = tk.StringVar(value="md")
        ttk.Combobox(run_row2b, textvariable=self.a_var_format, values=["md", "docx"],
                     width=5, state="readonly").pack(side="left", padx=4)
        ttk.Label(run_row2b, text="输出文件夹:").pack(side="left", padx=(16, 0))
        self.a_var_filter_root = tk.StringVar(value="AI分类")
        ttk.Entry(run_row2b, textvariable=self.a_var_filter_root,
                  width=12).pack(side="left", padx=4)
        self._hint_icon(run_row2b, "AI分类结果与数据筛选分开放置;相对路径以程序目录为基准",
                        pack=dict(side="left", padx=4))

        run_row3 = ttk.Frame(frame_run)
        run_row3.pack(fill="x", pady=(8, 0))
        self.a_var_summary = tk.BooleanVar(value=True)
        ttk.Checkbutton(run_row3, text="启用AI总结(生成标题)",
                        variable=self.a_var_summary).pack(side="left")
        self.a_var_keep_source = tk.BooleanVar(value=True)
        ttk.Checkbutton(run_row3, text="保留原文件(默认勾选,不移动)",
                        variable=self.a_var_keep_source).pack(side="left", padx=10)
        self.a_var_keep_original = tk.BooleanVar(value=False)
        ttk.Checkbutton(run_row3, text="保留原标题(不勾选则重命名为“日期_AI标题”,按名称排序即按日期)",
                        variable=self.a_var_keep_original).pack(side="left", padx=10)
        self._hint_icon(run_row3, "总结标题会写入各类别目录的 AI总结.txt",
                        pack=dict(side="left", padx=4))

        run_row4 = ttk.Frame(frame_run)
        run_row4.pack(fill="x", pady=(8, 0))
        self.btn_ai = ttk.Button(run_row4, text="开始AI分类",
                                 command=self._start_ai_classify)
        self.btn_ai.pack(side="left")
        self.btn_ai_clear = ttk.Button(run_row4, text="清空进度",
                                       command=self._a_clear_progress)
        self.btn_ai_clear.pack(side="left", padx=6)
        self.a_progress = ttk.Progressbar(run_row4, length=260, mode="determinate")
        self.a_progress.pack(side="left", padx=10)
        self.a_lbl_progress = ttk.Label(run_row4, text="")
        self.a_lbl_progress.pack(side="left", padx=4)
        self.btn_ai_open = ttk.Button(run_row4, text="打开输出文件夹",
                                      command=self._a_open_output_dir)
        self.btn_ai_open.pack(side="left", padx=6)

        run_row5 = ttk.Frame(frame_run)
        run_row5.pack(fill="x", pady=(4, 0))
        self.a_lbl_tokens = ttk.Label(run_row5, text="已消耗 tokens: 0",
                                      foreground="blue")
        self.a_lbl_tokens.pack(side="left")
        self.a_lbl_counts = ttk.Label(run_row5, text="")
        self.a_lbl_counts.pack(side="left", padx=16)

    def _a_toggle_key(self):
        """显示/隐藏 API Key"""
        if self.a_entry_key.cget("show") == "*":
            self.a_entry_key.configure(show="")
            self.a_btn_show_key.configure(text="隐藏")
        else:
            self.a_entry_key.configure(show="*")
            self.a_btn_show_key.configure(text="显示")

    def _a_save_config(self):
        """保存 AI 配置到 ai_config.json"""
        try:
            from weibo_ai import AIConfig
            cfg = AIConfig()
            cfg.data.update({
                "api_key": self.a_var_api_key.get().strip(),
                "base_url": self.a_var_base_url.get().strip() or "https://api.deepseek.com",
                "model": self.a_var_model.get().strip() or "deepseek-v4-flash",
                "quality_threshold": self._a_int(self.a_var_quality.get(), 80),
                "ad_threshold": self._a_int(self.a_var_ad.get(), 70),
                "suspicious_low": self._a_int(self.a_var_susp.get(), 30),
                "system_prompt": self.a_txt_prompt.get("1.0", "end").strip(),
            })
            if cfg.save():
                self._append_log(f"AI配置已保存到: {cfg.config_path}")
                self._save_settings()
            else:
                messagebox.showerror("保存失败", "AI配置保存失败,请检查程序目录权限")
        except Exception as e:
            logger.error(f"保存AI配置失败: {e}")
            messagebox.showerror("保存失败", f"保存AI配置失败:\n{e}")

    @staticmethod
    def _a_int(text, default):
        try:
            return max(0, min(100, int(float(text))))
        except (TypeError, ValueError):
            return default

    def _a_restore_prompts(self):
        """恢复默认提示词"""
        from weibo_ai import DEFAULT_SYSTEM_PROMPT
        self.a_txt_prompt.delete("1.0", "end")
        self.a_txt_prompt.insert("1.0", DEFAULT_SYSTEM_PROMPT)
        self._append_log("已恢复默认提示词")

    def _a_test_connection(self):
        """AI页: 用当前输入框内容测试连接"""
        self._test_ai_connection(
            self.a_btn_test,
            key=self.a_var_api_key.get().strip(),
            base=self.a_var_base_url.get().strip(),
            model=self.a_var_model.get().strip())

    def _test_ai_connection(self, btn, key=None, base=None, model=None):
        """测试 API 连接(后台线程执行);key/base/model 为 None 时读取已保存配置"""
        if key is None:
            try:
                from weibo_ai import AIConfig
                cfg = AIConfig()
                key = cfg.get("api_key", "")
                base = cfg.get("base_url", "")
                model = cfg.get("model", "")
            except Exception as e:
                messagebox.showerror("读取配置失败", f"读取AI配置失败:\n{e}")
                return
        key = (key or "").strip()
        if not key:
            messagebox.showwarning("未配置", "请先在“AI筛选”页签填写 API Key 并点击“保存配置”")
            return
        base = (base or "").strip() or "https://api.deepseek.com"
        model = (model or "").strip() or "deepseek-v4-flash"
        btn.configure(state="disabled")

        def work():
            try:
                from weibo_ai import AIClient
                client = AIClient(key, base, model, timeout=30)
                text, usage = client.chat("你是测试助手。", "请回复:连接成功", temperature=0)
                msg = (f"连接成功!\n接口: {base}\n模型: {model}\n"
                       f"回复: {text[:100]}\n消耗: {usage.get('total_tokens', '?')} tokens")
                self.root.after(0, self._ai_test_done, btn, msg)
            except Exception as e:
                self.root.after(0, self._ai_test_done, btn, f"连接失败:\n{e}")

        threading.Thread(target=work, daemon=True).start()

    def _ai_test_done(self, btn, msg):
        btn.configure(state="normal")
        self._append_log("AI连接测试: " + msg.splitlines()[0])
        messagebox.showinfo("测试连接", msg, parent=self.root)

    def _a_open_output_dir(self):
        """打开当前AI分类任务的输出文件夹(AI分类目录,内含 AI_博主_类别 子目录)"""
        try:
            root_dir = self._resolve_ai_root()
            os.makedirs(root_dir, exist_ok=True)
            os.startfile(root_dir)
            self._append_log(f"已打开AI分类输出文件夹: {root_dir}")
        except Exception as e:
            logger.error(f"打开AI分类输出文件夹失败: {e}")
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}", parent=self.root)

    def _a_clear_progress(self):
        """清空当前博主的AI分类进度(切换选项后想全部重跑时使用)"""
        uid = self.a_var_uid.get().strip()
        if not uid:
            messagebox.showwarning("提示", "请先填写微博ID", parent=self.root)
            return
        # 新位置: 博主数据目录/ai_progress.json;旧位置: 输出目录/ai_progress_<uid>.json
        removed = []
        try:
            # 遍历 DataPC 下博主目录
            base = os.path.join(app_dir(), "DataPC")
            if os.path.isdir(base):
                for d in os.listdir(base):
                    p = os.path.join(base, d, "ai_progress.json")
                    if os.path.isfile(p) and d.endswith("_" + uid):
                        os.remove(p)
                        removed.append(p)
            p = os.path.join(self._resolve_ai_root(), f"ai_progress_{uid}.json")
            if os.path.isfile(p):
                os.remove(p)
                removed.append(p)
            if removed:
                self._append_log(f"已清空AI分类进度: {len(removed)} 处")
            else:
                self._append_log("没有找到进度文件,无需清空")
        except Exception as e:
            logger.warning(f"清空AI进度失败: {e}")

    def _start_ai_classify(self):
        """事后AI分类: 校验参数并启动后台线程(与爬取任务互斥)"""
        if self.ai_running:
            return
        if self.running:
            messagebox.showinfo(
                "任务冲突", "爬取任务正在运行中,请等待其完成后再开始AI分类。\n"
                "(爬取与AI分类会同时读写数据文件,暂时不能并行)",
                parent=self.root)
            return
        name = self.a_var_name.get().strip()
        uid = self.a_var_uid.get().strip()
        try:
            start = (f"{int(self.a_var_start_year.get()):04d}-"
                     f"{int(self.a_var_start_month.get()):02d}-"
                     f"{int(self.a_var_start_day.get()):02d}")
            end = (f"{int(self.a_var_end_year.get()):04d}-"
                   f"{int(self.a_var_end_month.get()):02d}-"
                   f"{int(self.a_var_end_day.get()):02d}")
        except (ValueError, TypeError):
            messagebox.showwarning("日期错误", "请选择有效的AI分类开始/结束日期")
            return
        if start > end:
            messagebox.showwarning(
                "日期错误",
                f"开始日期({start})不能晚于结束日期({end}),请检查年月日是否填反")
            return
        if not (name and uid):
            messagebox.showwarning("参数不完整", "请填写博主昵称和微博ID")
            return

        # 保存AI配置(API/阈值/提示词)
        from weibo_ai import AIConfig
        cfg = AIConfig()
        cfg.data.update({
            "api_key": self.a_var_api_key.get().strip(),
            "base_url": self.a_var_base_url.get().strip() or "https://api.deepseek.com",
            "model": self.a_var_model.get().strip() or "deepseek-v4-flash",
            "quality_threshold": self._a_int(self.a_var_quality.get(), 80),
            "ad_threshold": self._a_int(self.a_var_ad.get(), 70),
            "suspicious_low": self._a_int(self.a_var_susp.get(), 30),
            "system_prompt": self.a_txt_prompt.get("1.0", "end").strip(),
        })
        if not cfg.is_configured():
            messagebox.showwarning("未配置API", "请先在“API设置”中填写 API Key 并保存配置")
            return
        cfg.save()

        self._save_settings()
        self._remember_blogger(uid, name)
        self.btn_ai.configure(state="disabled")
        self.btn_start.configure(state="disabled")  # 与爬取互斥
        self.ai_running = True
        self._append_log("=" * 60)
        self._append_log(f"开始AI分类: 博主={name}({uid}), 时间={start} ~ {end}, "
                         f"格式={self.a_var_format.get()}")

        kwargs = {
            "user_name": name,
            "user_id": uid,
            "start_date": start,
            "end_date": end,
            "source_format": self.a_var_format.get(),
            "summary_enabled": self.a_var_summary.get(),
            "keep_source": self.a_var_keep_source.get(),
            "keep_original": self.a_var_keep_original.get(),
            "filter_root": self._resolve_ai_root(),
            "cfg": cfg,
        }
        threading.Thread(target=self._run_ai_worker, args=(kwargs,), daemon=True).start()

    def _run_ai_worker(self, kwargs):
        try:
            from weibo_ai import AIClient, AIClassifier, AIRunner
            cfg = kwargs.pop("cfg")
            client = AIClient(cfg.get("api_key", ""), cfg.get("base_url", ""),
                              cfg.get("model", ""))
            classifier = AIClassifier(client, cfg)
            runner = AIRunner(classifier, filter_root=kwargs["filter_root"])

            def progress_cb(i, total, wid):
                self.ai_progress_queue.put(("progress", i, total, wid))

            def usage_cb(tokens):
                self.ai_progress_queue.put(("tokens", tokens))

            result = runner.run(
                kwargs["user_name"], kwargs["user_id"],
                kwargs["start_date"], kwargs["end_date"],
                source_format=kwargs["source_format"],
                keep_source=kwargs.get("keep_source", True),
                summary_enabled=kwargs["summary_enabled"],
                keep_original=kwargs.get("keep_original", False),
                progress_callback=progress_cb, usage_callback=usage_cb)
            if result["total"] == 0:
                msg = ("\nAI分类未执行: 未找到符合条件的本地文章。\n"
                       "请检查博主/日期范围/数据格式,确认已先爬取数据。")
            else:
                msg = (f"\nAI分类完成: 共 {result['total']} 篇"
                       f"(已跳过 {result.get('skipped', 0)} 篇已分类)\n"
                       f"高质量 {result['high']} | 广告 {result['ad']} | "
                       f"可疑 {result['suspicious']} | 其他低质量 {result['low']} | "
                       f"失败 {result['failed']}\n"
                       f"消耗 tokens: {result['tokens']}\n"
                       f"输出目录: {os.path.join(kwargs['filter_root'], 'AI_' + kwargs['user_name'] + '_*')}")
            self.root.after(0, self._on_ai_finish, msg, result)
        except Exception as e:
            logger.error(f"AI分类异常: {e}", exc_info=True)
            self.root.after(0, self._on_ai_finish, f"\nAI分类异常终止: {e}", None)

    def _on_ai_finish(self, msg, result):
        self._append_log(msg)
        self.btn_ai.configure(state="normal")
        self.btn_start.configure(state="normal")  # 解除与爬取的互斥
        self.ai_running = False
        self.a_progress.configure(value=0)
        if result:
            self.a_lbl_tokens.configure(text=f"已消耗 tokens: {result['tokens']}")
            self.a_lbl_counts.configure(
                text=f"高质量{result['high']} 广告{result['ad']} "
                     f"可疑{result['suspicious']} 低质量{result['low']} 失败{result['failed']}")
        self._refresh_source_options()
        messagebox.showinfo("AI分类完成" if result and result["total"] else "AI分类",
                            msg, parent=self.root)

    def _load_month_options(self):
        """读取 DataPC 下该博主已有月份目录,刷新筛选页"已有月份"下拉"""

        def _scan():
            name = self.f_var_name.get().strip()
            uid = self.f_var_uid.get().strip()
            months = []
            if name and uid:
                base = os.path.join(app_dir(), "DataPC", f"{name}_{uid}")
                if os.path.isdir(base):
                    for year_name in os.listdir(base):
                        yp = os.path.join(base, year_name)
                        if not (year_name.endswith("年") and os.path.isdir(yp)):
                            continue
                        year = year_name[:-1]
                        for month_name in os.listdir(yp):
                            mp = os.path.join(yp, month_name)
                            if month_name.endswith("月") and os.path.isdir(mp):
                                try:
                                    months.append((int(year), int(month_name[:-1])))
                                except ValueError:
                                    pass
            months.sort()
            return [f"{y}年{m}月" for y, m in months]

        try:
            values = _scan()
            self.f_month_from_combo.configure(values=values)
            self.f_month_to_combo.configure(values=values)
            cur_from = self.f_var_month_from.get()
            cur_to = self.f_var_month_to.get()
            if cur_from not in values:
                self.f_var_month_from.set("")
            if cur_to not in values:
                self.f_var_month_to.set("")
        except Exception as e:
            logger.warning(f"读取已有月份失败: {e}")

    def _refresh_source_options(self):
        """根据已有 AI 分类文件夹刷新筛选页"数据源"下拉(扫描 筛选/ 与 AI分类/ 两处)"""
        try:
            labels = {"ai_high": "高质量", "ai_ad": "广告", "ai_suspicious": "可疑"}
            roots = [self._resolve_filter_root(), self._resolve_ai_root()]
            values = ["DataPC"]
            for root in roots:
                if not os.path.isdir(root):
                    continue
                for key, label in labels.items():
                    if key in values:
                        continue
                    for name in os.listdir(root):
                        if (name.startswith("AI_") and name.endswith(f"_{label}")
                                and os.path.isdir(os.path.join(root, name))):
                            values.append(key)
                            break
            display_map = {v: ("爬取数据(DataPC)" if v == "DataPC"
                               else f"AI-{labels[v]}") for v in values}
            self._f_source_display_map = display_map
            self.f_source_combo.configure(values=list(display_map.values()))
            cur = self.f_var_source.get()
            if cur not in values:
                self.f_var_source.set("DataPC")
        except Exception as e:
            logger.warning(f"刷新数据源选项失败: {e}")

    def _resolve_ai_root(self):
        """把AI页"输出文件夹"解析为绝对路径(相对路径以程序目录为基准,默认 AI分类)"""
        try:
            val = self.a_var_filter_root.get()
        except AttributeError:
            val = "AI分类"  # AI页未构建时(如初次刷新数据源下拉)
        root = (val or "").strip() or "AI分类"
        if not os.path.isabs(root):
            root = os.path.join(app_dir(), root)
        return root

    def _resolve_filter_root(self):
        """把筛选页"输出文件夹"解析为绝对路径(相对路径以程序目录为基准)"""
        root = self.f_var_filter_root.get().strip() or "筛选"
        if not os.path.isabs(root):
            root = os.path.join(app_dir(), root)
        return root

    def _open_current_filter_dir(self):
        """打开当前筛选表单对应的输出文件夹(按博主/日期/指标匹配,容忍实际TOP数)"""
        try:
            name = self.f_var_name.get().strip()
            uid = self.f_var_uid.get().strip()
            start = (f"{int(self.f_var_start_year.get()):04d}-"
                     f"{int(self.f_var_start_month.get()):02d}-"
                     f"{int(self.f_var_start_day.get()):02d}")
            end = (f"{int(self.f_var_end_year.get()):04d}-"
                   f"{int(self.f_var_end_month.get()):02d}-"
                   f"{int(self.f_var_end_day.get()):02d}")
        except (ValueError, TypeError):
            messagebox.showwarning("日期错误", "请先选择有效的筛选日期", parent=self.root)
            return
        if not (name and uid):
            messagebox.showwarning("参数不完整", "请先填写博主昵称和微博ID", parent=self.root)
            return
        try:
            filter_root = self._resolve_filter_root()
            blogger_dir = os.path.join(filter_root, f"{name}_{uid}")
            prefix = f"{start}~{end}_"
            if os.path.isdir(blogger_dir):
                cands = [os.path.join(blogger_dir, d) for d in os.listdir(blogger_dir)
                         if d.startswith(prefix) and os.path.isdir(os.path.join(blogger_dir, d))]
                if cands:
                    target = max(cands, key=os.path.getmtime)
                    os.startfile(target)
                    self._append_log(f"已打开筛选结果文件夹: {target}")
                    return
            messagebox.showinfo(
                "未找到",
                f"未找到该筛选任务的输出文件夹:\n{blogger_dir}\n\n"
                f"请先执行筛选(条件: {start} ~ {end})", parent=self.root)
        except Exception as e:
            logger.error(f"打开当前筛选任务文件夹失败: {e}")
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}", parent=self.root)

    # ---------- 博主记录文件 ----------

    def _open_blogger_record(self):
        """用系统默认文本编辑器打开博主记录文件"""
        try:
            from weibo_crawler_core import blogger_record_path
            path = blogger_record_path()  # DataPC/博主记录.txt
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# 博主记录文件\n")
                    f.write("# 格式: 微博ID 博主昵称 (空格分隔,每行一个)\n")
                    f.write("# 可手动增删博主;程序爬取新博主后会自动追加\n")
            os.startfile(path)  # Windows 下用默认编辑器打开
            self._append_log(f"已打开博主记录文件: {path}")
        except Exception as e:
            logger.error(f"打开博主记录文件失败: {e}")
            messagebox.showerror("错误", f"无法打开博主记录文件:\n{e}")

    def _load_blogger_names(self, combo, uid_var, name_var):
        """从博主记录文件加载名称到下拉框;选中名称时自动填充ID"""
        try:
            from weibo_crawler_core import load_blogger_records
            self._blogger_records = load_blogger_records()  # {uid: name}
            names = sorted(set(self._blogger_records.values()))
            combo.configure(values=names)
            if names and not name_var.get():
                name_var.set(names[0])

            def on_select(event):
                sel = name_var.get()
                # 按名称找ID
                for uid, nm in self._blogger_records.items():
                    if nm == sel:
                        uid_var.set(uid)
                        return

            combo.bind("<<ComboboxSelected>>", on_select)
        except Exception as e:
            logger.warning(f"加载博主记录失败: {e}")
            self._blogger_records = {}

    def _reload_blogger_names(self, combo, uid_var, name_var):
        """重新从博主记录文件加载下拉框(编辑文件后点击刷新按钮调用)

        保留当前选中的昵称;若当前昵称已不在记录中,则保留输入值不清空
        """
        try:
            from weibo_crawler_core import load_blogger_records
            self._blogger_records = load_blogger_records()
            names = sorted(set(self._blogger_records.values()))
            combo.configure(values=names)
            current = name_var.get()
            # 若当前名称在记录中,自动刷新ID(名称可能对应不同ID)
            if current:
                for uid, nm in self._blogger_records.items():
                    if nm == current:
                        uid_var.set(uid)
                        break
            self._append_log(f"已刷新博主记录,共 {len(names)} 个博主")
        except Exception as e:
            logger.warning(f"刷新博主记录失败: {e}")

    def _remember_blogger(self, uid, name):
        """把新爬取的博主写入记录文件"""
        try:
            from weibo_crawler_core import save_blogger_record
            save_blogger_record(uid, name)
        except Exception as e:
            logger.warning(f"记录博主失败: {e}")

    # ---------- 日志与队列 ----------

    def _append_log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _open_filter_root_dir(self):
        """打开数据筛选输出目录(筛选文件夹)"""
        try:
            root_dir = self._resolve_filter_root()
            os.makedirs(root_dir, exist_ok=True)
            os.startfile(root_dir)
            self._append_log(f"已打开筛选目录: {root_dir}")
        except Exception as e:
            logger.error(f"打开筛选目录失败: {e}")
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}", parent=self.root)

    def _open_ai_root_dir(self):
        """打开AI分类输出目录(AI分类文件夹)"""
        try:
            root_dir = self._resolve_ai_root()
            os.makedirs(root_dir, exist_ok=True)
            os.startfile(root_dir)
            self._append_log(f"已打开AI分类目录: {root_dir}")
        except Exception as e:
            logger.error(f"打开AI分类目录失败: {e}")
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}", parent=self.root)

    def _open_data_root(self):
        """打开 DataPC 数据根目录"""
        try:
            data_root = os.path.join(app_dir(), "DataPC")
            os.makedirs(data_root, exist_ok=True)
            os.startfile(data_root)
            self._append_log(f"已打开数据目录: {data_root}")
        except Exception as e:
            logger.error(f"打开数据目录失败: {e}")
            messagebox.showerror("打开失败", f"无法打开数据目录:\n{e}", parent=self.root)

    def _open_month_dir(self, name, uid, year_var, month_var):
        """打开指定博主在所选年月的文件夹;不存在时逐级回退(博主目录->DataPC根)"""
        try:
            name = (name or "").strip()
            uid = (uid or "").strip()
            year = year_var.get().strip()
            month = month_var.get().strip()
            data_root = os.path.join(app_dir(), "DataPC")
            os.makedirs(data_root, exist_ok=True)
            if not (name and uid):
                os.startfile(data_root)
                self._append_log(f"未填写博主信息,已打开数据根目录: {data_root}")
                return
            user_dir = os.path.join(data_root, f"{name}_{uid}")
            candidates = []
            if year and month:
                try:
                    candidates.append(os.path.join(
                        user_dir, f"{year}年", f"{int(month)}月"))
                except (TypeError, ValueError):
                    pass
            candidates.append(user_dir)
            for path in candidates:
                if os.path.isdir(path):
                    os.startfile(path)
                    self._append_log(f"已打开文件夹: {path}")
                    return
            os.startfile(data_root)
            self._append_log(
                f"未找到博主 {name}({uid}) 在 {year}年{int(month) if month else '?'}月"
                f" 的数据文件夹,已打开数据根目录: {data_root}")
        except Exception as e:
            logger.error(f"打开月份文件夹失败: {e}")
            messagebox.showerror("打开失败", f"无法打开文件夹:\n{e}", parent=self.root)

    def _export_log(self):
        """导出本次运行日志到文本文件"""
        try:
            default_name = f"运行日志_{datetime.now():%Y%m%d_%H%M%S}.txt"
            path = filedialog.asksaveasfilename(
                title="导出运行日志", defaultextension=".txt",
                initialdir=app_dir(),  # 默认打开程序所在目录
                initialfile=default_name,
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
                parent=self.root)
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.txt_log.get("1.0", "end"))
            self._append_log(f"日志已导出: {path}")
        except Exception as e:
            logger.error(f"导出日志失败: {e}")
            messagebox.showerror("导出失败", f"导出日志失败:\n{e}", parent=self.root)

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

        # AI分类进度(进度条/tokens)
        try:
            while True:
                item = self.ai_progress_queue.get_nowait()
                if item[0] == "progress":
                    _, i, total, wid = item
                    self.a_progress.configure(maximum=max(total, 1), value=i)
                    self.a_lbl_progress.configure(text=f"{i}/{total} {wid}")
                elif item[0] == "tokens":
                    self.a_lbl_tokens.configure(text=f"已消耗 tokens: {item[1]}")
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
        if self.ai_running:
            messagebox.showinfo(
                "任务冲突", "AI分类任务正在运行中,请等待其完成后再开始爬取。\n"
                "(爬取与AI分类会同时读写数据文件,暂时不能并行)",
                parent=self.root)
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
        if start > end:
            messagebox.showwarning(
                "日期错误",
                f"开始日期({start})不能晚于结束日期({end}),请检查年月日是否填反")
            return

        if not (name and uid):
            messagebox.showwarning("参数不完整", "请填写博主昵称和微博ID")
            return

        # 新博主自动记入博主记录文件
        self._remember_blogger(uid, name)

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
        self.btn_ai.configure(state="disabled")  # 与AI分类互斥
        self.lbl_status.configure(text="运行中...", foreground="orange")
        self._append_log("=" * 60)
        self._append_log(f"开始任务: 博主={name}({uid}), 时间={start} ~ {end}")

        # AI 实时判断: 校验API配置
        ai_enabled = self.var_ai_enabled.get()
        ai_config = None
        if ai_enabled:
            from weibo_ai import AIConfig
            ai_config = AIConfig()
            if not ai_config.is_configured():
                messagebox.showwarning(
                    "AI未配置",
                    "已勾选“启用AI实时判断”,但尚未配置API Key。\n"
                    "请先在“AI筛选”页签填写API Key并点击“保存配置”。")
                self.running = False
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.lbl_status.configure(text="就绪", foreground="green")
                return
            self._append_log(f"已启用AI实时判断(高质量可信度阈值 "
                             f"{ai_config.get('quality_threshold')}%)")

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
            "skip_existing": self.var_skip_existing.get(),
            "min_words": int(self.var_min_words.get() or 0),
            "ai_enabled": ai_enabled,
            "ai_config": ai_config,
            "ai_rename": ai_enabled and self.var_ai_rename.get(),
            "ai_root": self._resolve_ai_root(),
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
                    f"导出成功 {result['exported']} 条, 失败 {result['failed']} 条, "
                    f"跳过 {result.get('skipped', 0)} 条"
                )
                if result.get("ai_skipped"):
                    final_msg += f", AI过滤 {result['ai_skipped']} 条"
                if result.get("ai_tokens"):
                    final_msg += f"\nAI消耗 tokens: {result['ai_tokens']}"
                final_msg += (
                    f"\nID文件: {result.get('txt_file') or '无'}\n"
                    f"MD目录: {result.get('md_dir') or '无'}"
                )
            self.root.after(0, self._on_finish, final_msg)
        except Exception as e:
            logger.error(f"任务异常: {e}", exc_info=True)
            self.root.after(0, self._on_finish, f"\n任务异常终止: {e}")

    # ---------- 筛选 ----------

    def _apply_window_size(self):
        """恢复上次关闭时的窗口大小(最大化状态一并恢复)"""
        st = self.settings
        try:
            w = int(st.get("win_w", 0) or 0)
            h = int(st.get("win_h", 0) or 0)
            if 800 <= w <= 4000 and 600 <= h <= 4000:
                self.root.geometry(f"{w}x{h}")
            if st.get("win_max"):
                self.root.state("zoomed")
        except (TypeError, ValueError):
            pass

    def _save_window_geometry(self):
        """关闭前记录当前窗口大小(最小化/隐藏时不记录)"""
        try:
            state = self.root.state()
            if state in ("iconic", "withdrawn"):
                return
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w < 400 or h < 300:
                return
            st = self._load_settings()
            st["win_w"] = w
            st["win_h"] = h
            st["win_max"] = (state == "zoomed")
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _start_filter(self):
        """筛选页: 对本地数据按转评赞筛选"""
        name = self.f_var_name.get().strip()
        uid = self.f_var_uid.get().strip()
        try:
            start = (f"{int(self.f_var_start_year.get()):04d}-"
                     f"{int(self.f_var_start_month.get()):02d}-"
                     f"{int(self.f_var_start_day.get()):02d}")
            end = (f"{int(self.f_var_end_year.get()):04d}-"
                   f"{int(self.f_var_end_month.get()):02d}-"
                   f"{int(self.f_var_end_day.get()):02d}")
        except (ValueError, TypeError):
            messagebox.showwarning("日期错误", "请选择有效的筛选开始/结束日期")
            return
        if start > end:
            messagebox.showwarning(
                "日期错误",
                f"开始日期({start})不能晚于结束日期({end}),请检查年月日是否填反")
            return
        try:
            top_n = int(self.f_var_top.get())
            if top_n < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("篇数错误", "输出篇数应为正整数")
            return

        # 记住筛选页设置
        self._save_settings()

        source = self.f_var_source.get()
        self._append_log("=" * 60)
        self._append_log(f"开始筛选: 博主={name}({uid}), 时间={start} ~ {end}, "
                         f"前{top_n}篇, 数据源={source}")
        self.btn_filter.configure(state="disabled")

        kwargs = {
            "user_name": name,
            "user_id": uid,
            "start_date": start,
            "end_date": end,
            "top_n": top_n,
            "use_repost": self.f_var_use_repost.get(),
            "use_comment": self.f_var_use_comment.get(),
            "use_like": self.f_var_use_like.get(),
            "by_word_count": self.f_var_sort_mode.get() == "word_count",
            "source_format": self.f_var_format.get(),
            "move": self.f_var_move.get(),
            "filter_root": self._resolve_filter_root(),
            "data_source": source,
            "auto_open": self.f_var_auto_open.get(),
        }
        t = threading.Thread(target=self._run_filter_worker, args=(kwargs,), daemon=True)
        t.start()

    def _run_filter_worker(self, kwargs):
        try:
            from weibo_crawler_core import ArticleFilter
            af = ArticleFilter(filter_root=kwargs["filter_root"])
            result = af.filter_top(
                kwargs["user_name"], kwargs["user_id"],
                kwargs["start_date"], kwargs["end_date"],
                top_n=kwargs["top_n"],
                use_repost=kwargs["use_repost"],
                use_comment=kwargs["use_comment"],
                use_like=kwargs["use_like"],
                source_format=kwargs["source_format"],
                move=kwargs["move"],
                by_word_count=kwargs.get("by_word_count", False),
                data_source=kwargs.get("data_source", "DataPC"),
            )
            if result["output_dir"]:
                msg = (f"\n筛选完成: 共 {len(result['items'])} 篇\n"
                       f"输出目录: {result['output_dir']}\n"
                       f"统计明细: 目录内'筛选说明.txt'")
                # 勾选"筛选完成后自动打开输出文件夹"时直接打开
                if kwargs.get("auto_open"):
                    try:
                        os.startfile(result["output_dir"])
                        self.root.after(0, self._append_log,
                                        f"已自动打开筛选结果文件夹: {result['output_dir']}")
                    except Exception as e:
                        logger.warning(f"自动打开筛选结果文件夹失败: {e}")
            else:
                msg = "\n筛选未完成: 未找到符合条件的文件,请检查博主/日期/格式"
            self.root.after(0, self._on_filter_finish, msg)
        except Exception as e:
            logger.error(f"筛选异常: {e}", exc_info=True)
            self.root.after(0, self._on_filter_finish, f"\n筛选异常终止: {e}")

    def _on_filter_finish(self, msg):
        self._append_log(msg)
        self.btn_filter.configure(state="normal")

    def _on_finish(self, msg):
        self._append_log(msg)
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_ai.configure(state="normal")  # 解除与AI分类的互斥
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
        self._save_window_geometry()  # 记录窗口大小,下次打开沿用
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
