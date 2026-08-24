#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 筛选模块: 基于 OpenAI 兼容 API(DeepSeek/通义/Kimi 等)对微博文章分类与总结

功能:
  1. AIClient       - OpenAI 兼容 chat/completions 客户端(统计 token)
  2. AIConfig       - AI 配置读写(api_key/base_url/model/阈值/提示词)
  3. AIClassifier   - 文章分类(高质量/广告/可疑/低质量)+ 标题总结
  4. AIRunner       - 事后批量分类: 扫描本地文章->逐篇AI判断->按类移动文件

提示词设计原则(节省 token):
  - 只要求返回 JSON,禁止多余解释
  - 正文截断(默认前 2000 字符)
  - 分类与总结使用独立提示词,可被用户自定义覆盖
"""

import json
import os
import re
import shutil
import logging
from datetime import datetime

logger = logging.getLogger('weibo_crawler.ai')

# 默认 API 配置(DeepSeek 兼容 OpenAI 接口;用户可改为其他兼容服务)
DEFAULT_API_BASE = "https://api.deepseek.com"
# 默认模型: deepseek-v4-flash(V4 系列轻量模型,便宜快速,适合分类/总结这类任务;
# 官方旧名 deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用)
DEFAULT_MODEL = "deepseek-v4-flash"

DEFAULT_SYSTEM_PROMPT = (
    "你是一个微博内容质量分析助手。用户会给你一篇微博文章正文,"
    "你需要判断它属于哪一类:\n"
    "- high: 高质量深度内容(有信息量、有观点、有分析价值)\n"
    "- ad: 广告或营销内容(推广商品、引流、卖课、软广等)\n"
    "- suspicious: 无法确定,可能是软广或介于两者之间\n"
    "- low: 低质量内容(水文、无信息量、纯情绪输出、蹭热点无内容)\n"
    "只返回 JSON,不要任何其他文字:\n"
    '{"category": "high|ad|suspicious|low", "ad_prob": 0-100, "quality_prob": 0-100}\n'
    "ad_prob 表示广告概率,quality_prob 表示高质量概率,均为 0-100 的整数。"
)

DEFAULT_USER_PROMPT = "请分析以下微博文章正文:\n\n{content}"

DEFAULT_SUMMARY_PROMPT = (
    "请为以下微博文章生成一个简洁的标题(不超过20个字)。"
    "只返回标题文字本身,不要引号、句号或任何其他内容。\n\n正文:\n{content}"
)

AI_CONFIG_FILE = "ai_config.json"

# 分类结果常量
CAT_HIGH = "high"          # 高质量
CAT_AD = "ad"              # 广告
CAT_SUSPICIOUS = "suspicious"  # 可疑
CAT_LOW = "low"            # 其他低质量
CAT_LABELS = {
    CAT_HIGH: "高质量", CAT_AD: "广告",
    CAT_SUSPICIOUS: "可疑", CAT_LOW: "其他低质量",
}


def app_dir():
    """获取应用程序所在目录(exe 版为 exe 目录,源码版为脚本目录)"""
    if getattr(__import__("sys"), "frozen", False):
        return os.path.dirname(os.path.abspath(__import__("sys").executable))
    return os.path.dirname(os.path.abspath(__file__))


def ai_config_path():
    """AI 配置文件路径(与程序同目录)"""
    return os.path.join(app_dir(), AI_CONFIG_FILE)


# ---------------------------------------------------------------------------
# AI 配置
# ---------------------------------------------------------------------------

class AIConfig:
    """AI 配置: 读写 ai_config.json,提供默认值"""

    DEFAULTS = {
        "api_key": "",
        "base_url": DEFAULT_API_BASE,
        "model": DEFAULT_MODEL,
        "quality_threshold": 80,     # 高质量可信度阈值(低于=非高质量)
        "ad_threshold": 70,          # 广告概率>=此值=广告
        "suspicious_low": 30,        # 广告概率>=此值且<ad_threshold=可疑
        "max_content_chars": 2000,   # 发送给AI的正文最大字符数
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": DEFAULT_USER_PROMPT,
        "summary_prompt": DEFAULT_SUMMARY_PROMPT,
    }

    def __init__(self, config_path=None):
        self.config_path = config_path or ai_config_path()
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k in self.DEFAULTS:
                        if k in saved:
                            self.data[k] = saved[k]
        except Exception as e:
            logger.warning(f"读取AI配置失败: {e}")

    def save(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存AI配置失败: {e}")
            return False

    def is_configured(self):
        return bool(self.data.get("api_key", "").strip())

    def get(self, key, default=None):
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端
# ---------------------------------------------------------------------------

class AIClient:
    """OpenAI 兼容 chat/completions 客户端"""

    def __init__(self, api_key, base_url=DEFAULT_API_BASE, model=DEFAULT_MODEL,
                 timeout=90):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(self, system_prompt, user_content, temperature=0):
        """调用 chat/completions

        返回 (回复文本, token用量dict);失败抛异常
        """
        import requests
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"AI API 错误 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage", {}) or {}
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise RuntimeError(f"AI API 返回格式异常: {str(data)[:300]}")
        return text, usage


# ---------------------------------------------------------------------------
# 分类器 / 总结器
# ---------------------------------------------------------------------------

class AIClassifier:
    """文章分类器: 调用 AI 判断文章类别与可信度"""

    def __init__(self, client, config):
        self.client = client
        self.config = config

    def _parse_result(self, text):
        """解析 AI 返回的 JSON,提取 category/ad_prob/quality_prob

        容错: 从文本中提取 JSON 片段
        """
        text = (text or "").strip()
        # 尝试直接解析
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                raise ValueError(f"AI 返回无法解析: {text[:200]}")
            try:
                data = json.loads(m.group(0))
            except Exception:
                raise ValueError(f"AI 返回 JSON 解析失败: {text[:200]}")
        category = str(data.get("category", "")).strip().lower()
        if category not in (CAT_HIGH, CAT_AD, CAT_SUSPICIOUS, CAT_LOW):
            raise ValueError(f"AI 返回未知类别: {category}")
        ad_prob = int(data.get("ad_prob", 0) or 0)
        quality_prob = int(data.get("quality_prob", 0) or 0)
        return category, ad_prob, quality_prob

    def classify(self, content):
        """分类一篇文章

        返回 (category, ad_prob, quality_prob, usage)
        """
        max_chars = int(self.config.get("max_content_chars", 2000))
        truncated = content[:max_chars]
        user_prompt = str(self.config.get("user_prompt", DEFAULT_USER_PROMPT))
        user_content = user_prompt.replace("{content}", truncated)
        system_prompt = str(self.config.get("system_prompt", DEFAULT_SYSTEM_PROMPT))
        text, usage = self.client.chat(system_prompt, user_content)
        category, ad_prob, quality_prob = self._parse_result(text)
        return category, ad_prob, quality_prob, usage

    def summarize_title(self, content):
        """AI 总结标题

        返回 (title, usage)
        """
        max_chars = int(self.config.get("max_content_chars", 2000))
        truncated = content[:max_chars]
        prompt = str(self.config.get("summary_prompt", DEFAULT_SUMMARY_PROMPT))
        user_content = prompt.replace("{content}", truncated)
        text, usage = self.client.chat(
            "你是标题生成助手。", user_content, temperature=0.7)
        title = (text or "").strip().strip('"').strip("'").strip()
        # 去掉可能的多行
        title = re.split(r"[\n\r]+", title)[0].strip()
        return title, usage


# ---------------------------------------------------------------------------
# 事后批量分类执行器
# ---------------------------------------------------------------------------

class AIRunner:
    """AI 事后分类: 扫描本地文章,逐篇调用 AI 判断,按类别移动文件

    输出目录: <filter_root>/AI_<博主名>_<类别>/<年份>年/<月份>/...
    - 保留年月目录结构,且文件名保持 <博主>_<日期>_<微博ID>.ext,
      因此"筛选"页可把 AI 分类结果作为数据源再次筛选
    - 图片/视频随文章同步移动(images/ videos/ 子目录)
    - 支持断点续跑: 已处理ID记录在 <filter_root>/ai_progress_<博主ID>.json
    - 可选 AI 总结: 生成标题写入 AI总结.txt;不保留原标题时
      文件名追加 AI 标题(<原名>_<AI标题>.ext)
    """

    CAT_DIR_NAMES = {
        CAT_HIGH: "AI_{name}_高质量",
        CAT_AD: "AI_{name}_广告",
        CAT_SUSPICIOUS: "AI_{name}_可疑",
        CAT_LOW: "AI_{name}_其他低质量",
    }

    def __init__(self, classifier, data_root=None, filter_root="筛选"):
        self.classifier = classifier
        self.config = classifier.config
        self.data_root = data_root or os.path.join(app_dir(), "DataPC")
        self.filter_root = filter_root

    def _find_user_dir(self, user_id):
        candidates = []
        for name in os.listdir(self.data_root):
            m = re.match(r"^(.+)_(\d+)$", name)
            if m and m.group(2) == user_id:
                p = os.path.join(self.data_root, name)
                if os.path.isdir(p):
                    candidates.append(p)
        if not candidates:
            return None

        def weight(p):
            year_dirs = sum(1 for x in os.listdir(p)
                            if os.path.isdir(os.path.join(p, x)) and x.endswith("年"))
            n_files = sum(len(fs) for _, _, fs in os.walk(p))
            return (year_dirs, n_files)
        return max(candidates, key=weight)

    def scan_files(self, user_id, start_date, end_date, source_format="md"):
        """扫描指定博主/日期范围/格式的文章文件(同 ArticleFilter 逻辑)"""
        user_dir = self._find_user_dir(user_id)
        if not user_dir:
            return []
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
        if start and end and start > end:
            logger.warning(f"扫描范围无效: 开始日期({start_date})晚于结束日期({end_date})")
            return []
        ext = ".docx" if source_format == "docx" else ".md"
        found = []
        for year_item in sorted(os.listdir(user_dir)):
            year_path = os.path.join(user_dir, year_item)
            if not (os.path.isdir(year_path) and year_item.endswith("年")):
                continue
            for month_item in sorted(os.listdir(year_path)):
                month_path = os.path.join(year_path, month_item)
                if not (os.path.isdir(month_path) and month_item.endswith("月")):
                    continue
                for f in os.listdir(month_path):
                    if not f.endswith(ext):
                        continue
                    # 兼容三种文件名:
                    #   新: <日期>_<AI标题>.ext (如 26-1-22_深度解析.md)
                    #   旧: <博主>_<日期>_<ID>.ext 与 <AI标题>_<日期>.ext
                    # 优先取"开头是日期"的匹配;否则取下划线日期形式的第一个匹配
                    fms = list(re.finditer(
                        r"(?:^|_)(\d{2,4})-(\d{1,2})-(\d{1,2})(?=_|\.(?:md|docx)$)", f))
                    if not fms:
                        continue
                    fm = fms[0]
                    year = int(fm.group(1))
                    if year < 100:
                        year += 2000
                    fdate = datetime(year, int(fm.group(2)), int(fm.group(3)))
                    if start and fdate < start:
                        continue
                    if end and fdate > end:
                        continue
                    found.append((os.path.join(month_path, f), fdate))
        return found

    def _read_content(self, file_path):
        """读取文章正文(从 md/docx 中提取正文部分)"""
        try:
            if file_path.endswith(".md"):
                with open(file_path, "r", encoding="utf-8") as f:
                    text = f.read()
                # 提取"正文："到"---"之间的内容
                m = re.search(r"正文[：:]\s*\n(.*?)\n\s*---", text, re.DOTALL)
                if m:
                    return m.group(1).strip()
                return text[:2000]
            elif file_path.endswith(".docx"):
                from docx import Document
                doc = Document(file_path)
                lines = [p.text for p in doc.paragraphs]
                # 找"正文："之后的内容
                parts = []
                in_body = False
                for t in lines:
                    if t.strip() == "正文：":
                        in_body = True
                        continue
                    if in_body and t.strip().startswith("—"):
                        break
                    if in_body:
                        parts.append(t)
                return "\n".join(parts).strip() or "\n".join(lines)[:2000]
        except Exception as e:
            logger.warning(f"读取正文失败 {file_path}: {e}")
        return ""

    def _progress_path(self, user_id):
        return os.path.join(self.filter_root, f"ai_progress_{user_id}.json")

    def _apply_thresholds(self, ad_prob, quality_prob):
        """按配置阈值决定最终类别(模型返回的类别仅作参考)

        - 广告概率 >= 广告阈值       -> 广告
        - 广告概率 >= 可疑下界       -> 可疑(介于两者之间)
        - 高质量概率 >= 高质量阈值   -> 高质量
        - 其余                       -> 其他低质量
        """
        ad_thr = int(self.config.get("ad_threshold", 70) or 70)
        susp_low = int(self.config.get("suspicious_low", 30) or 30)
        q_thr = int(self.config.get("quality_threshold", 80) or 80)
        if ad_prob >= ad_thr:
            return CAT_AD
        if ad_prob >= susp_low:
            return CAT_SUSPICIOUS
        if quality_prob >= q_thr:
            return CAT_HIGH
        return CAT_LOW

    def _load_progress(self, user_id):
        try:
            path = self._progress_path(user_id)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return set(data.get("processed", []))
        except Exception:
            pass
        return set()

    def _save_progress(self, user_id, processed):
        try:
            os.makedirs(self.filter_root, exist_ok=True)
            with open(self._progress_path(user_id), "w", encoding="utf-8") as f:
                json.dump({"processed": sorted(processed)}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"保存AI进度失败: {e}")

    def run(self, user_name, user_id, start_date, end_date,
            source_format="md", resume=True, keep_source=True,
            summary_enabled=False, keep_original=False,
            progress_callback=None, usage_callback=None):
        """执行事后分类

        keep_source=True   : 复制到类别目录,原文件保留在 DataPC(默认)
        keep_source=False  : 移动原文件到类别目录
        summary_enabled=True 时逐篇生成 AI 标题:
          - keep_original=True  : 文件名不变,标题记入 AI总结.txt
          - keep_original=False : 文件名重命名为 <AI标题>_<年-月-日>.ext(默认)
        progress_callback(i, total, wid) 每篇开始前调用
        usage_callback(total_tokens) 每次AI调用后调用(累计值)

        返回 dict: {"total", "high", "ad", "suspicious", "low",
                     "failed", "tokens", "output_dir", "summaries"}
        """
        files = self.scan_files(user_id, start_date, end_date, source_format)
        if not files:
            logger.warning(f"未找到博主 {user_name}({user_id}) 的 {source_format} 文件")
            return {"total": 0, "high": 0, "ad": 0, "suspicious": 0,
                    "low": 0, "failed": 0, "tokens": 0,
                    "output_dir": None, "summaries": []}

        # 预处理: 提取文件名中的微博ID与发布日期
        file_records = []
        user_dir = self._find_user_dir(user_id)
        for fpath, fdate in files:
            m = re.search(r"_([A-Za-z0-9]+)\.(?:md|docx)$", os.path.basename(fpath))
            wid = m.group(1) if m else os.path.basename(fpath)
            file_records.append((fpath, wid, fdate))

        processed = self._load_progress(user_id) if resume else set()
        total = len(file_records)
        counts = {CAT_HIGH: 0, CAT_AD: 0, CAT_SUSPICIOUS: 0, CAT_LOW: 0}
        failed = 0
        total_tokens = 0
        summaries = []  # (原文件名, AI标题, 类别)

        # 创建输出目录
        os.makedirs(self.filter_root, exist_ok=True)
        out_dirs = {cat: os.path.join(self.filter_root, name.format(name=user_name))
                    for cat, name in self.CAT_DIR_NAMES.items()}
        for d in out_dirs.values():
            os.makedirs(d, exist_ok=True)

        for i, (fpath, wid, fdate) in enumerate(file_records, 1):
            if resume and wid in processed:
                logger.info(f"[{i}/{total}] 已处理过,跳过: {wid}")
                continue
            if progress_callback:
                progress_callback(i, total, wid)

            content = self._read_content(fpath)
            if not content:
                failed += 1
                logger.warning(f"[{i}/{total}] 读取正文失败: {os.path.basename(fpath)}")
                processed.add(wid)
                continue

            try:
                category, ad_prob, quality_prob, usage = self.classifier.classify(content)
            except Exception as e:
                failed += 1
                logger.error(f"[{i}/{total}] AI分类失败 {wid}: {e}")
                # 失败也记录,避免卡死(下次可清空进度重跑)
                processed.add(wid)
                continue

            total_tokens += int(usage.get("total_tokens", 0) or 0)
            if usage_callback:
                usage_callback(total_tokens)
            # 按配置阈值决定最终类别(不直接采用模型返回的类别)
            category = self._apply_thresholds(ad_prob, quality_prob)
            counts[category] += 1
            logger.info(
                f"[{i}/{total}] {wid}: {CAT_LABELS[category]} "
                f"(广告{ad_prob}% 高质量{quality_prob}%)")

            # AI 总结(可选): 生成标题
            title = None
            if summary_enabled:
                try:
                    title, usage2 = self.classifier.summarize_title(content)
                    total_tokens += int(usage2.get("total_tokens", 0) or 0)
                    if usage_callback:
                        usage_callback(total_tokens)
                    summaries.append((os.path.basename(fpath), title, CAT_LABELS[category]))
                except Exception as e:
                    logger.warning(f"[{i}/{total}] AI总结失败 {wid}: {e}")

            # 复制/移动文件到对应类别目录(保留年月结构,便于后续筛选)
            move = not keep_source
            rel_dir = os.path.relpath(os.path.dirname(fpath), user_dir)
            dst_dir = os.path.join(out_dirs[category], rel_dir)
            os.makedirs(dst_dir, exist_ok=True)
            new_name = os.path.basename(fpath)
            if title and not keep_original:
                # 重命名: <年-月-日>_<AI标题>.ext(如 26-1-22_深度解析.md)
                # 日期放在文件名开头,资源管理器按名称排序(升/降序)即按日期排序
                _, ext = os.path.splitext(new_name)
                safe_title = self._sanitize_filename_part(title, max_len=40)
                dm = re.search(r"_(\d{2,4})-(\d{1,2})-(\d{1,2})", new_name)
                if dm:
                    date_part = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
                else:
                    date_part = (f"{fdate.year % 100}-{fdate.month}-{fdate.day}")
                new_name = f"{date_part}_{safe_title}{ext}"
            dst = os.path.join(dst_dir, new_name)
            if os.path.exists(dst):
                if keep_source:
                    # 复制模式下目标已存在: 说明该篇已分类过,跳过本次复制
                    logger.info(f"目标已存在,跳过复制: {new_name}")
                    processed.add(wid)
                    continue
                stem, ext = os.path.splitext(dst)
                dst = f"{stem}_{wid}{ext}"
            if move:
                shutil.move(fpath, dst)
            else:
                shutil.copy2(fpath, dst)

            # 同步复制媒体文件(按微博ID前缀;重命名后的文章无ID,媒体不再关联)
            self._copy_media(fpath, dst_dir, move)

            processed.add(wid)
            if resume and (i % 10 == 0 or i == total):
                self._save_progress(user_id, processed)

        if resume:
            self._save_progress(user_id, processed)

        # 写 AI总结.txt(如有总结结果)
        if summaries:
            self._write_summary_txt(out_dirs, summaries)

        result = {
            "total": total,
            "high": counts[CAT_HIGH],
            "ad": counts[CAT_AD],
            "suspicious": counts[CAT_SUSPICIOUS],
            "low": counts[CAT_LOW],
            "failed": failed,
            "tokens": total_tokens,
            "output_dir": self.filter_root,
            "summaries": summaries,
        }
        logger.info(
            f"AI分类完成: 共{total}篇 高质量{counts[CAT_HIGH]} 广告{counts[CAT_AD]} "
            f"可疑{counts[CAT_SUSPICIOUS]} 低质量{counts[CAT_LOW]} 失败{failed} "
            f"消耗{total_tokens}tokens")
        return result

    @staticmethod
    def _sanitize_filename_part(text, max_len=30):
        """清洗用于文件名的标题: 去掉 Windows 非法字符并截断"""
        text = re.sub(r'[\\/:*?"<>|\r\n]+', "", text or "")
        return (text.strip() or "无标题")[:max_len]

    @staticmethod
    def _write_summary_txt(out_dirs, summaries):
        """把 AI 总结写入每个类别目录下的 AI总结.txt"""
        lines = ["AI总结清单(文件名\tAI标题\t类别):", ""]
        for fname, title, label in summaries:
            lines.append(f"{fname}\t{title}\t{label}")
        try:
            for d in set(out_dirs.values()):
                with open(os.path.join(d, "AI总结.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
        except Exception as e:
            logger.warning(f"写入AI总结清单失败: {e}")

    @staticmethod
    def _copy_media(src_file, out_dir, move=False):
        """同步复制同目录 images/videos 下该微博的媒体文件"""
        try:
            m = re.search(r"_([A-Za-z0-9]+)\.(?:md|docx)$", os.path.basename(src_file))
            if not m:
                return
            weibo_id = m.group(1)
            src_dir = os.path.dirname(src_file)
            for sub in ("images", "videos"):
                src_sub = os.path.join(src_dir, sub)
                if not os.path.isdir(src_sub):
                    continue
                dst_sub = os.path.join(out_dir, sub)
                os.makedirs(dst_sub, exist_ok=True)
                prefix = f"{weibo_id}_"
                for fname in os.listdir(src_sub):
                    if fname.startswith(prefix):
                        s = os.path.join(src_sub, fname)
                        d = os.path.join(dst_sub, fname)
                        if move:
                            shutil.move(s, d)
                        else:
                            shutil.copy2(s, d)
        except Exception as e:
            logger.warning(f"复制媒体失败 {src_file}: {e}")
