#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微博PC端爬虫核心模块
====================
功能:
  1. 自动识别微博页面中的动态类名(CSS Modules 哈希类名,如 _body_m3n8j_63 -> _body_ecgcn_63)
  2. 收集指定博主在指定时间范围内的原创微博ID
  3. 逐条抓取详情并导出 Markdown 文件

类名识别策略(三级兜底):
  1. 优先使用配置文件 class_names.json 中保存的类名(上次成功使用的)
  2. 自动探测:通过 JS 在页面中按"语义特征"(时间链接、展开按钮、正文文本等)定位元素并提取类名
  3. 自动探测失败 -> 回调用户手动输入类名

用法(命令行):
  python weibo_crawler_cli.py --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30

用法(GUI):
  python weibo_crawler_gui.py
"""

import os
import re
import sys
import json
import time
import logging
import random
import urllib.parse
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from webdriver_manager.microsoft import EdgeChromiumDriverManager

logger = logging.getLogger('weibo_crawler')

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def ensure_logger(log_level=logging.INFO):
    """确保全局 logger 有输出 handler(避免重复添加)"""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(log_level)
    return logger


def app_dir():
    """获取应用程序所在目录

    - 源码运行: 脚本所在目录
    - PyInstaller 打包的 exe: exe 所在目录(而非临时解压目录 _MEIPASS)
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打包环境
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """获取资源文件路径

    - 源码运行: 脚本所在目录
    - PyInstaller 打包: 优先从临时解压目录 _MEIPASS 读取内置资源(只读);
      需要持久化的文件(如 class_names.json 保存)仍走 app_dir(exe 旁)
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bundled = os.path.join(meipass, *parts)
            if os.path.exists(bundled):
                return bundled
    return os.path.join(app_dir(), *parts)


# ---------------------------------------------------------------------------
# 类名管理:配置文件 + 自动探测 + 手动输入兜底
# ---------------------------------------------------------------------------

class ClassNameManager:
    """微博页面类名管理

    管理的类名(键名):
      card       微博卡片容器  (div._body_xxx_63)
      time       发布时间链接  (a._time_xxx_33)
      name       博主用户名    (div._name_xxx_291)
      content    微博正文      (div._wbtext_xxx_14)
      stats_wrap 转评赞容器    (div._wrap_xxx_137)
      stats_num  转评赞数字    (span._num_xxx_46)

    配置保存在 class_names.json,格式:
      {"card": "_body_ecgcn_63", "time": "...", ...}
    """

    CONFIG_FILE = "class_names.json"

    # 默认类名:用户已知的最新值,配置缺失时作为初始尝试
    DEFAULT_CLASSES = {
        "card": "_body_ecgcn_63",        # 2026-08 微博卡片类名(用户提供)
        "time": "_time_1tpft_33",
        "name": "_name_1yc79_291",
        "content": "_wbtext_q1l14_14",
        "stats_wrap": "_wrap_198pe_137",
        "stats_num": "_num_198pe_46",
    }

    # 各键对应的元素标签,用于 CSS 选择器与验证
    TAG_MAP = {
        "card": "div",
        "time": "a",
        "name": "div",
        "content": "div",
        "stats_wrap": "div",
        "stats_num": "span",
    }

    def __init__(self, config_path=None, manual_callback=None, logger=None):
        """
        manual_callback: 手动输入类名的回调函数
            签名: callback(key, current_value) -> str
            返回用户输入的类名(不含前缀),返回空串/None 表示用户放弃输入
        """
        self.logger = logger or logging.getLogger('weibo_crawler.classes')
        self.config_path = config_path or os.path.join(app_dir(), self.CONFIG_FILE)
        self.manual_callback = manual_callback
        self.classes = dict(self.DEFAULT_CLASSES)
        self.load()

    # ---------- 配置读写 ----------

    def load(self):
        """从配置文件加载类名,文件不存在或损坏时使用默认值"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key in self.DEFAULT_CLASSES:
                        val = data.get(key)
                        if val and isinstance(val, str):
                            self.classes[key] = val
                    self.logger.info(f"已从配置文件加载类名: {self.config_path}")
        except Exception as e:
            self.logger.warning(f"读取类名配置文件失败,使用默认类名: {e}")

    def save(self):
        """保存当前类名到配置文件"""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.classes, f, ensure_ascii=False, indent=2)
            self.logger.info(f"类名已保存到: {self.config_path}")
            return True
        except Exception as e:
            self.logger.error(f"保存类名配置文件失败: {e}")
            return False

    def get(self, key):
        return self.classes.get(key, "")

    def set(self, key, value):
        value = (value or "").strip().lstrip(".")
        if value:
            self.classes[key] = value

    # ---------- 类名解析(核心):配置 -> 探测 -> 手动 ----------

    def resolve(self, driver, key, uid=None, username=None, no_data_check=None):
        """确保 key 对应的类名在当前页面有效

        流程:
          1. 当前配置的类名若能在页面找到有效元素 -> 直接使用
          2. 否则自动探测(JS 按语义特征查找)
          3. 探测失败 -> 若 no_data_check 回调返回 True(页面没有目标数据,
             如博主该时间段无微博),跳过手动输入直接返回 None
          4. 否则 -> 调用 manual_callback 让用户输入

        返回有效类名(不含点号),全部失败返回 None
        """
        tag = self.TAG_MAP.get(key, "div")
        current = self.get(key)

        # 第1步:验证现有类名
        if current:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, f"{tag}.{current}")
                if els:
                    # 对 card / time 做附加验证(必须包含目标用户的微博链接)
                    if key == "card" and uid:
                        ok = any(self._element_has_user_link(el, uid) for el in els[:5])
                        if ok:
                            return current
                    elif key == "time" and uid:
                        ok = any(self._element_has_user_link(el, uid) for el in els[:5])
                        if ok:
                            return current
                    else:
                        return current
                self.logger.info(f"类名 {key}={current} 在页面中未找到有效元素,尝试自动探测")
            except Exception as e:
                self.logger.warning(f"验证类名 {key}={current} 时出错: {e}")

        # 第2步:自动探测
        detected = self._probe(driver, key, uid, username)
        if detected:
            self.set(key, detected)
            self.save()
            self.logger.info(f"已自动探测到类名 {key}: {detected}")
            return detected

        # 第3步:若页面根本没有目标数据(如博主该时间段无微博),跳过手动输入
        if no_data_check is not None:
            try:
                if no_data_check():
                    self.logger.info(
                        f"页面中未发现该博主的微博数据,跳过类名 {key} 的手动输入")
                    return None
            except Exception as e:
                self.logger.warning(f"检查页面数据状态时出错: {e}")

        # 第4步:手动输入
        self.logger.warning(f"自动探测类名 {key} 失败,需要手动输入")
        if self.manual_callback:
            while True:
                try:
                    val = self.manual_callback(key, current or "")
                except KeyboardInterrupt:
                    return None
                val = (val or "").strip().lstrip(".")
                if not val:
                    self.logger.warning(f"用户放弃输入类名 {key}")
                    return None
                # 验证用户输入的类名
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, f"{tag}.{val}")
                    if els:
                        if key in ("card", "time") and uid:
                            if any(self._element_has_user_link(el, uid) for el in els[:5]):
                                self.set(key, val)
                                self.save()
                                return val
                            self.logger.warning(f"输入的类名 {val} 未找到目标用户的链接,请确认")
                            continue
                        self.set(key, val)
                        self.save()
                        return val
                    self.logger.warning(f"输入的类名 {val} 在页面上找不到元素,请重新输入")
                except Exception as e:
                    self.logger.warning(f"验证手动输入的类名 {val} 时出错: {e}")
                    continue
        return None

    @staticmethod
    def _element_has_user_link(el, uid):
        """判断元素内部是否包含指向 weibo.com/{uid}/{mid} 的时间链接"""
        try:
            links = el.find_elements(By.CSS_SELECTOR, "a[href*='/{}']".format(uid))
            for link in links:
                href = link.get_attribute("href") or ""
                title = link.get_attribute("title") or ""
                if f"/{uid}/" in href and re.match(r"\d{4}-\d{1,2}-\d{1,2}", title):
                    return True
        except Exception:
            pass
        return False

    # ---------- 自动探测(JS) ----------

    def _probe(self, driver, key, uid=None, username=None):
        """按语义特征在页面中探测类名,返回类名或 None"""
        try:
            if key == "card":
                return driver.execute_script(self._JS_CARD, uid or "")
            if key == "time":
                return driver.execute_script(self._JS_TIME, uid or "")
            if key == "name":
                return driver.execute_script(self._JS_NAME, username or "")
            if key == "content":
                return driver.execute_script(self._JS_CONTENT)
            if key == "stats_wrap":
                return driver.execute_script(self._JS_STATS)["wrap"]
            if key == "stats_num":
                return driver.execute_script(self._JS_STATS)["num"]
        except Exception as e:
            self.logger.warning(f"自动探测类名 {key} 时出错: {e}")
        return None

    # 探测卡片类名:找指向 uid 的时间链接(a[title] 为日期),向上找 class 含 _body_ 的元素
    _JS_CARD = r"""
    return (function(uid){
      if(!uid) return null;
      var links = document.querySelectorAll('a');
      var counts = {};
      var bodyRe = /_body_[A-Za-z0-9]+_\d+/;
      for (var i=0;i<links.length;i++){
        var a = links[i];
        var href = a.getAttribute('href')||'';
        var title = a.getAttribute('title')||'';
        if (href.indexOf('/'+uid+'/')===-1) continue;
        if (!/^\d{4}-\d{1,2}-\d{1,2}/.test(title)) continue;
        var el = a;
        while (el && el!==document.body){
          var cls = el.getAttribute('class')||'';
          if (cls && typeof cls==='string'){
            var m = cls.match(bodyRe);
            if (m){ counts[m[0]]=(counts[m[0]]||0)+1; break; }
          }
          el = el.parentElement;
        }
      }
      var best=null,bn=0;
      for (var k in counts){ if(counts[k]>bn){bn=counts[k];best=k;} }
      return best;
    })(arguments[0])
    """

    # 探测时间链接类名:找指向 uid 且 title 为日期的 a,取 class 中 _time_ 开头者
    _JS_TIME = r"""
    return (function(uid){
      if(!uid) return null;
      var links = document.querySelectorAll('a');
      var counts = {};
      var timeRe = /_time_[A-Za-z0-9]+_\d+/;
      for (var i=0;i<links.length;i++){
        var a = links[i];
        var href = a.getAttribute('href')||'';
        var title = a.getAttribute('title')||'';
        if (href.indexOf('/'+uid+'/')===-1) continue;
        if (!/^\d{4}-\d{1,2}-\d{1,2}/.test(title)) continue;
        var cls = a.getAttribute('class')||'';
        if (cls && typeof cls==='string'){
          var m = cls.match(timeRe);
          if (m){ counts[m[0]]=(counts[m[0]]||0)+1; }
        }
      }
      var best=null,bn=0;
      for (var k in counts){ if(counts[k]>bn){bn=counts[k];best=k;} }
      return best;
    })(arguments[0])
    """

    # 探测用户名类名:找文本等于博主名的元素,class 中 _name_ 开头者
    _JS_NAME = r"""
    return (function(username){
      if(!username) return null;
      var els = document.querySelectorAll('div, span, h1, h2, h3');
      var counts = {};
      var nameRe = /_name_[A-Za-z0-9]+_\d+/;
      for (var i=0;i<els.length;i++){
        var el = els[i];
        var txt = (el.innerText||'').trim();
        if (txt !== username && txt.indexOf(username) === -1) continue;
        if (txt.length > username.length + 20) continue; // 避免命中大块文本容器
        var cls = el.getAttribute('class')||'';
        if (cls && typeof cls==='string'){
          var m = cls.match(nameRe);
          if (m){ counts[m[0]]=(counts[m[0]]||0)+1; }
        }
      }
      var best=null,bn=0;
      for (var k in counts){ if(counts[k]>bn){bn=counts[k];best=k;} }
      return best;
    })(arguments[0])
    """

    # 探测正文类名(详情页):class 含 _wbtext_ 且文本较长的元素
    _JS_CONTENT = r"""
    return (function(){
      var els = document.querySelectorAll('div');
      var counts = {};
      var re = /_wbtext_[A-Za-z0-9]+_\d+/;
      for (var i=0;i<els.length;i++){
        var el = els[i];
        var cls = el.getAttribute('class')||'';
        if (!cls || typeof cls!=='string') continue;
        var m = cls.match(re);
        if (!m) continue;
        var txt = (el.innerText||'').trim();
        if (txt.length >= 5){ counts[m[0]]=(counts[m[0]]||0)+1; }
      }
      var best=null,bn=0;
      for (var k in counts){ if(counts[k]>bn){bn=counts[k];best=k;} }
      return best;
    })()
    """

    # 探测转评赞容器/数字类名(详情页):i.woo-font--* 图标的祖先 div 中 _wrap_ 开头者,
    # 以及其内部 class 含 _num_ 的 span
    _JS_STATS = r"""
    return (function(){
      var icons = document.querySelectorAll('i.woo-font--retweet, i.woo-font--comment, i.woo-font--like, i.woo-font--like2');
      var wrapCounts = {}, numCounts = {};
      var wrapRe = /_wrap_[A-Za-z0-9]+_\d+/;
      var numRe = /_num_[A-Za-z0-9]+_\d+/;
      var wraps = [];
      for (var i=0;i<icons.length;i++){
        var el = icons[i].parentElement;
        while (el && el!==document.body){
          var cls = el.getAttribute('class')||'';
          if (cls && typeof cls==='string'){
            var m = cls.match(wrapRe);
            if (m){ wrapCounts[m[0]]=(wrapCounts[m[0]]||0)+1; wraps.push(el); break; }
          }
          el = el.parentElement;
        }
      }
      for (var j=0;j<wraps.length;j++){
        var spans = wraps[j].querySelectorAll('span');
        for (var k=0;k<spans.length;k++){
          var scls = spans[k].getAttribute('class')||'';
          if (scls && typeof scls==='string'){
            var m2 = scls.match(numRe);
            if (m2){ numCounts[m2[0]]=(numCounts[m2[0]]||0)+1; }
          }
        }
      }
      function bestOf(o){ var b=null,n=0; for (var k in o){ if(o[k]>n){n=o[k];b=k;} } return b; }
      return {wrap: bestOf(wrapCounts), num: bestOf(numCounts)};
    })()
    """


# ---------------------------------------------------------------------------
# 微博 PC 端爬虫(收集 ID + 导出 Markdown)
# ---------------------------------------------------------------------------

class WeiboPCCrawler:
    """微博 PC 端爬虫

    职责:
      - 收集指定用户指定时间范围内的原创微博 ID -> 保存 txt
      - 从 ID 列表抓取详情 -> 导出 Markdown 文件
      - 所有页面类名通过 ClassNameManager 动态解析
    """

    def __init__(self, headless=False, user_data_dir=None,
                 class_manager=None, wait_callback=None, manual_callback=None):
        self.user_data_dir = user_data_dir
        self.headless = headless
        # manual_callback: 手动输入类名的回调,签名 (key, current) -> str
        self.manual_callback = manual_callback
        self.classes = class_manager or ClassNameManager(manual_callback=manual_callback)
        # wait_callback: 等待用户操作(如手动登录后继续)的回调,签名 (message) -> None
        self.wait_callback = wait_callback
        self.driver = self.setup_driver(headless)
        self.wait = WebDriverWait(self.driver, 10)

    # ---------- 浏览器驱动 ----------

    def setup_driver(self, headless):
        """设置 Edge 浏览器选项

        驱动管理策略(按优先级):
          1. Selenium Manager(Selenium 4.6+ 内置,自动下载匹配版本,无需联网配置)
          2. webdriver-manager(旧方案,失败时忽略)
          3. 系统默认驱动
        启动前会自动清理上次异常退出遗留的锁文件(DevToolsActivePort / Singleton*)
        """
        edge_options = EdgeOptions()
        edge_options.use_chromium = True

        if headless:
            edge_options.add_argument('--headless')

        edge_options.add_argument('--disable-gpu')
        edge_options.add_argument('--no-sandbox')
        edge_options.add_argument('--disable-dev-shm-usage')
        edge_options.add_argument('--window-size=1920,1080')
        edge_options.add_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')

        if self.user_data_dir:
            edge_options.add_argument(f"--user-data-dir={self.user_data_dir}")
            logger.info(f"使用用户数据目录: {self.user_data_dir}")

        edge_options.add_argument("--disable-blink-features=AutomationControlled")
        edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        edge_options.add_experimental_option('useAutomationExtension', False)

        self._cleanup_stale_locks()

        # 方案1: Selenium Manager(内置,自动管理驱动,官方源 msedgedriver.microsoft.com)
        try:
            driver = webdriver.Edge(options=edge_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            return driver
        except Exception as e:
            logger.warning(f"使用 Selenium Manager 创建 Edge 驱动失败: {e}")

        # 方案2: webdriver-manager(依次尝试国内镜像与官方源)
        try:
            mirrors = [
                "https://registry.npmmirror.com/-/binary/edgedriver",
                "https://msedgedriver.microsoft.com",
            ]
            last_err = None
            for mirror in mirrors:
                try:
                    service = EdgeService(
                        EdgeChromiumDriverManager(url=mirror).install())
                    driver = webdriver.Edge(service=service, options=edge_options)
                    driver.execute_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    return driver
                except Exception as e:
                    last_err = e
                    logger.warning(f"使用镜像 {mirror} 下载驱动失败: {e}")
            raise last_err
        except Exception as e:
            logger.error(f"创建 Edge 驱动失败: {e}")
            return self.setup_driver_fallback(headless)

    def _cleanup_stale_locks(self):
        """清理上次异常退出遗留的浏览器锁文件(DevToolsActivePort / Singleton*)"""
        if not self.user_data_dir:
            return
        try:
            for name in ("DevToolsActivePort", "DevToolsActivePort.lock",
                         "SingletonLock", "SingletonCookie", "SingletonSocket"):
                path = os.path.join(self.user_data_dir, name)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        logger.info(f"已清理遗留锁文件: {path}")
                    except Exception as e:
                        logger.warning(f"清理锁文件失败(可能仍被占用): {path} - {e}")
        except Exception as e:
            logger.warning(f"清理遗留锁文件时出错: {e}")

    def setup_driver_fallback(self, headless):
        """备用方法:使用系统默认的 Edge 驱动"""
        try:
            edge_options = EdgeOptions()
            edge_options.use_chromium = True

            if headless:
                edge_options.add_argument('--headless')

            edge_options.add_argument('--disable-gpu')
            edge_options.add_argument('--no-sandbox')
            edge_options.add_argument('--disable-dev-shm-usage')
            edge_options.add_argument('--window-size=1920,1080')
            edge_options.add_argument(
                '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')

            if self.user_data_dir:
                edge_options.add_argument(f"--user-data-dir={self.user_data_dir}")

            edge_options.add_argument("--disable-blink-features=AutomationControlled")
            edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            edge_options.add_experimental_option('useAutomationExtension', False)

            driver = webdriver.Edge(options=edge_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            return driver
        except Exception as e:
            logger.error(f"备用方法也失败: {e}")
            raise Exception("无法创建 Edge 驱动,请确保已安装 Edge 浏览器")

    # ---------- 登录 ----------

    def check_login_status(self):
        """检查是否已登录微博"""
        try:
            self.driver.get("https://weibo.com")
            time.sleep(3)
            login_elements = self.driver.find_elements(By.XPATH, "//a[contains(text(), '登录')]")
            if login_elements:
                logger.info("检测到未登录状态")
                return False
            user_elements = self.driver.find_elements(By.XPATH, "//a[contains(@href, '/u/')]")
            if user_elements:
                logger.info("检测到已登录状态")
                return True
            return False
        except Exception as e:
            logger.error(f"检查登录状态时出错: {e}")
            return False

    def manual_login(self):
        """手动登录微博,等待用户在浏览器中完成登录

        若当前为无头模式(headless),自动重启为有头模式,
        否则用户看不到浏览器窗口无法扫码登录。
        """
        logger.info("请手动登录微博...")

        # 无头模式下无法显示登录界面,自动切换为有头模式
        if self.headless:
            logger.warning("当前为无头模式,无法显示浏览器供登录,自动重启为有头模式...")
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = self.setup_driver(headless=False)
            self.wait = WebDriverWait(self.driver, 10)
            self.headless = False
            logger.info("已重启为有头模式,请在弹出的浏览器窗口中完成登录")

        self.driver.get("https://weibo.com/login.php")
        if self.wait_callback:
            self.wait_callback("请在浏览器中完成登录,然后点击「确定」继续...")
        else:
            input("请在浏览器中完成登录,然后按回车键继续...")
        if self.check_login_status():
            logger.info("登录成功")
            return True
        logger.warning("登录可能未成功,请检查")
        return False

    # ---------- URL 构建 ----------

    @staticmethod
    def date_to_timestamp(date_str):
        """将日期字符串转换为微博时间戳(北京时间 0 点对应的 UTC 时间戳)"""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            utc_dt = dt - timedelta(hours=8)
            return int(utc_dt.timestamp())
        except Exception as e:
            logger.error(f"日期转换失败: {date_str}, 错误: {e}")
            return None

    def build_search_url(self, user_id, is_ori=1, is_forward=1, is_text=1, is_pic=1,
                         is_video=1, is_music=1, keyword="", start_date=None, end_date=None):
        """构建带高级搜索参数的微博用户页 URL"""
        base_url = f"https://weibo.com/u/{user_id}"
        params = {}
        if is_ori:
            params["is_ori"] = 1
        if is_forward:
            params["is_forward"] = 1
        if is_text:
            params["is_text"] = 1
        if is_pic:
            params["is_pic"] = 1
        if is_video:
            params["is_video"] = 1
        if is_music:
            params["is_music"] = 1
        if keyword:
            params["key_word"] = keyword
        if start_date:
            ts = self.date_to_timestamp(start_date)
            if ts:
                params["start_time"] = ts
        if end_date:
            ts = self.date_to_timestamp(end_date)
            if ts:
                params["end_time"] = ts
        if params:
            return f"{base_url}?{urllib.parse.urlencode(params)}"
        return base_url

    # ---------- 类名解析辅助 ----------

    def _page_has_user_links(self, uid):
        """诊断辅助:页面上是否存在指向该用户的微博时间链接

        用于区分两种情况:
          - 有链接但探测不到类名 -> 类名结构确实变了,需要手动输入
          - 没有链接 -> 该时间段内博主可能没有发表微博
        """
        try:
            return bool(self.driver.execute_script(self._JS_HAS_USER_LINKS, uid))
        except Exception as e:
            logger.warning(f"检查页面微博链接时出错: {e}")
            return False

    _JS_HAS_USER_LINKS = r"""
    return (function(uid){
      if(!uid) return false;
      var links = document.querySelectorAll('a');
      for (var i=0;i<links.length;i++){
        var a = links[i];
        var href = a.getAttribute('href')||'';
        var title = a.getAttribute('title')||'';
        if (href.indexOf('/'+uid+'/')===-1) continue;
        if (/^\d{4}-\d{1,2}-\d{1,2}/.test(title)) return true;
      }
      return false;
    })(arguments[0])
    """

    def resolve_card_class(self, uid):
        """解析微博卡片类名

        失败时先诊断页面状态,给出针对性提示:
          - 页面没有该博主的微博链接 -> 可能该时间段没有发表微博
          - 有链接但探测失败 -> 类名结构变化,需要手动输入
        """
        cls = self.classes.resolve(
            self.driver, "card", uid=uid,
            no_data_check=lambda: not self._page_has_user_links(uid))
        if not cls:
            if not self._page_has_user_links(uid):
                raise RuntimeError(
                    "页面上未找到该博主的微博:博主可能在此时间段内没有发表微博"
                    "(或页面尚未加载完成),请确认时间范围是否正确")
            raise RuntimeError(
                "无法确定微博卡片类名:自动探测失败且未获得手动输入,"
                "请检查页面类名是否变化后重试")
        return cls

    def resolve_time_class(self, uid):
        """解析时间链接类名,失败时同样先诊断页面状态"""
        cls = self.classes.resolve(
            self.driver, "time", uid=uid,
            no_data_check=lambda: not self._page_has_user_links(uid))
        if not cls:
            if not self._page_has_user_links(uid):
                raise RuntimeError(
                    "页面上未找到该博主的微博:博主可能在此时间段内没有发表微博"
                    "(或页面尚未加载完成),请确认时间范围是否正确")
            raise RuntimeError(
                "无法确定时间链接类名:自动探测失败且未获得手动输入,"
                "请检查页面类名是否变化后重试")
        return cls

    # ---------- 收集阶段 ----------

    def get_username(self, user_id, fallback_name=""):
        """从页面获取用户名"""
        try:
            time.sleep(2)
            name_cls = self.classes.resolve(self.driver, "name", username=fallback_name)
            if name_cls:
                els = self.driver.find_elements(By.CSS_SELECTOR, f"div.{name_cls}")
                for el in els:
                    txt = el.text.strip()
                    if txt:
                        logger.info(f"获取到用户名: {txt}")
                        return txt
            return fallback_name or f"user_{user_id}"
        except Exception as e:
            logger.error(f"获取用户名失败: {e}")
            return fallback_name or f"user_{user_id}"

    def click_search_button(self):
        """点击页面上的搜索按钮,确保高级搜索条件生效"""
        try:
            search_buttons = self.driver.find_elements(
                By.XPATH, "//button[.//span[text()='搜索']]")
            if search_buttons:
                search_buttons[0].click()
                logger.info("已点击搜索按钮")
                time.sleep(3)
                return True
            logger.warning("找不到搜索按钮,直接使用 URL 参数")
            return False
        except Exception as e:
            logger.warning(f"点击搜索按钮时出错: {e}")
            return False

    def collect_weibo_ids(self, user_id, start_date=None, end_date=None,
                          max_count=500, keyword="", is_ori=1, is_forward=0,
                          is_text=1, is_pic=1, is_video=1, is_music=1,
                          user_name=None):
        """收集指定用户指定时间范围内的微博 ID(原创/转发可选)

        返回 (username, weibo_ids)
        """
        # 登录检查
        if not self.check_login_status():
            logger.warning("未检测到登录状态,尝试手动登录")
            if not self.manual_login():
                logger.error("登录失败,无法继续")
                return None, []

        search_url = self.build_search_url(
            user_id, is_ori, is_forward, is_text, is_pic,
            is_video, is_music, keyword, start_date, end_date)
        logger.info(f"正在访问URL: {search_url}")
        self.driver.get(search_url)
        time.sleep(5)

        # 解析类名(在列表页上自动探测);若失败,先刷新页面再重试一次
        # (避免懒加载导致页面暂无卡片而误判"该时间段没有微博")
        try:
            card_cls = self.resolve_card_class(user_id)
        except RuntimeError as e:
            logger.warning(f"首次解析卡片类名失败: {e};刷新页面重试一次...")
            self.driver.refresh()
            time.sleep(5)
            card_cls = self.resolve_card_class(user_id)

        try:
            time_cls = self.resolve_time_class(user_id)
        except RuntimeError as e:
            logger.warning(f"首次解析时间类名失败: {e};刷新页面重试一次...")
            self.driver.refresh()
            time.sleep(5)
            time_cls = self.resolve_time_class(user_id)

        logger.info(f"使用的类名 -> 卡片: {card_cls}, 时间链接: {time_cls}")

        # 获取用户名(传入预期用户名以便探测)
        username = self.get_username(user_id, fallback_name=user_name or user_id)

        # 边滚动边收集微博 ID
        weibo_ids = self._scroll_and_collect(user_id, card_cls, time_cls, max_count)
        return username, weibo_ids

    def _scroll_and_collect(self, user_id, card_cls, time_cls, max_count=500):
        """渐进式滚动收集微博 ID"""
        weibo_ids = []
        processed_ids = set()
        scroll_attempts = 0
        max_scroll_attempts = 200
        no_progress_count = 0
        max_no_progress = 5

        current_scroll_position = 0
        last_scroll_height = self.driver.execute_script("return document.body.scrollHeight")
        card_selector = f"div.{card_cls}"

        while (len(weibo_ids) < max_count and
               scroll_attempts < max_scroll_attempts and
               no_progress_count < max_no_progress):

            new_ids = self._extract_ids_from_cards(card_selector, time_cls, user_id, processed_ids)
            new_count = 0
            for wid in new_ids:
                if wid not in weibo_ids:
                    weibo_ids.append(wid)
                    processed_ids.add(wid)
                    new_count += 1
                    logger.info(f"找到符合条件的微博ID: {wid} (总数: {len(weibo_ids)})")
                    if len(weibo_ids) >= max_count:
                        break

            if new_count == 0:
                no_progress_count += 1
            else:
                no_progress_count = 0

            if len(weibo_ids) >= max_count:
                break

            window_height = self.driver.execute_script("return window.innerHeight")
            scroll_amount = window_height * random.uniform(0.4, 0.5)
            new_scroll_position = min(current_scroll_position + scroll_amount, last_scroll_height)
            self.driver.execute_script(f"window.scrollTo(0, {new_scroll_position});")
            time.sleep(random.uniform(1, 1.5))

            current_scroll_position = new_scroll_position
            new_scroll_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_scroll_height > last_scroll_height:
                last_scroll_height = new_scroll_height
                no_progress_count = 0

            scroll_attempts += 1
            if current_scroll_position >= last_scroll_height - window_height:
                logger.info("已到达或接近页面底部")
                no_progress_count = max_no_progress

        logger.info(f"滚动完成,共尝试 {scroll_attempts} 次,找到 {len(weibo_ids)} 个微博ID")
        return weibo_ids[:max_count]

    def _extract_ids_from_cards(self, card_selector, time_cls, user_id, processed_ids):
        """从当前页面卡片中提取微博 ID(含展开按钮、非转发)"""
        weibo_ids = []
        try:
            cards = self.driver.find_elements(By.CSS_SELECTOR, card_selector)
            logger.info(f"当前可见 {len(cards)} 个微博卡片")
        except Exception as e:
            logger.warning(f"查找微博卡片失败: {e}")
            return weibo_ids

        time_selector = f"a.{time_cls}"
        for i, card in enumerate(cards):
            try:
                # 必须有"展开"按钮(说明是长文)
                expand_buttons = card.find_elements(
                    By.XPATH, ".//span[contains(@class, 'expand') and contains(text(), '展开')]")
                if not expand_buttons:
                    continue
                # 跳过转发微博
                repost_elements = card.find_elements(
                    By.XPATH, ".//div[contains(@class, 'repost') or contains(text(), '转发微博')]")
                if repost_elements:
                    continue
                # 时间链接
                time_links = card.find_elements(By.CSS_SELECTOR, time_selector)
                if not time_links:
                    continue
                href = time_links[0].get_attribute("href") or ""
                if f"/{user_id}/" in href:
                    parts = href.split(f"/{user_id}/")
                    if len(parts) > 1:
                        wid = parts[1].split("?")[0]
                        if wid and wid not in processed_ids:
                            weibo_ids.append(wid)
                            processed_ids.add(wid)
            except Exception as e:
                logger.warning(f"处理微博卡片 {i} 时出错: {e}")
                continue
        return weibo_ids

    # ---------- 详情阶段 ----------

    def get_weibo_detail(self, weibo_id, user_id):
        """获取单条微博的详细信息(打开新标签页)"""
        url = f"https://weibo.com/{user_id}/{weibo_id}"
        logger.info(f"正在访问微博: {url}")

        try:
            self.driver.execute_script(f"window.open('{url}');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)

            # 在详情页解析正文/转评赞类名(自动探测,失败则手动输入)
            content_cls = self.classes.resolve(self.driver, "content")
            self.classes.resolve(self.driver, "stats_wrap")
            self.classes.resolve(self.driver, "stats_num")

            publish_time = self.extract_publish_time()
            topics = self.extract_topics()
            content = self.extract_content(content_cls)
            repost_count, comment_count, like_count = self.extract_stats()
            save_time = datetime.now().strftime("%y-%m-%d %H:%M")

            return {
                "url": url,
                "publish_time": publish_time,
                "topics": topics,
                "content": content,
                "repost_count": repost_count,
                "comment_count": comment_count,
                "like_count": like_count,
                "save_time": save_time,
                "weibo_id": weibo_id,
            }
        except Exception as e:
            logger.error(f"获取微博内容时出现错误: {e}")
            return None
        finally:
            if len(self.driver.window_handles) > 1:
                self.driver.close()
                self.driver.switch_to.window(self.driver.window_handles[0])

    def extract_publish_time(self):
        """提取发布时间"""
        selectors = []
        time_cls = self.classes.get("time")
        if time_cls:
            selectors.append(f"a.{time_cls}")
        selectors += ["a[title*='-']", "span.time", "div.wb-info span"]
        for selector in selectors:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if els:
                    txt = els[0].text.strip()
                    if txt:
                        return txt
                    title = els[0].get_attribute("title")
                    if title:
                        return title
            except Exception:
                continue
        return "未知时间"

    def extract_topics(self):
        """提取词条(话题)"""
        try:
            topics = []
            for el in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='weibo?q=%23']"):
                txt = el.text.strip()
                if txt and txt.startswith('#') and txt.endswith('#'):
                    topics.append(txt)
            return " ".join(topics) if topics else "无"
        except Exception as e:
            logger.error(f"提取词条失败: {e}")
            return "无"

    def extract_content(self, content_cls=None):
        """提取正文内容"""
        selectors = []
        if content_cls:
            selectors.append(f"div.{content_cls}")
        selectors += ["div.wbtext", "div.weibo-text", "div.text"]
        for selector in selectors:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if els:
                    txt = els[0].text.strip()
                    if txt:
                        return txt
            except Exception:
                continue
        # 兜底:整个页面文本
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text
            return body[:1000] if len(body) > 1000 else body
        except Exception as e:
            logger.error(f"提取微博内容时出现错误: {e}")
            return "无法提取内容"

    def extract_stats(self):
        """提取转发、评论、点赞数(多策略:动态类名优先,旧选择器兜底)"""
        repost_count = comment_count = like_count = "0"
        wrap_cls = self.classes.get("stats_wrap")
        num_cls = self.classes.get("stats_num")

        # 策略1: 通过图标 + 动态 wrap/num 类名提取转发/评论/点赞
        try:
            if wrap_cls and num_cls:
                icon_to_key = {
                    "woo-font--retweet": "repost",
                    "woo-font--comment": "comment",
                    "woo-font--like": "like",
                    "woo-font--like2": "like",
                }
                for icon_cls, key in icon_to_key.items():
                    try:
                        icons = self.driver.find_elements(By.CSS_SELECTOR, f"i.{icon_cls}")
                        if not icons:
                            continue
                        parent = icons[0].find_element(
                            By.XPATH, f"./ancestor::div[contains(@class, '{wrap_cls}')]")
                        nums = parent.find_elements(By.CSS_SELECTOR, f"span.{num_cls}")
                        if nums:
                            val = nums[0].text.strip()
                            if key == "repost":
                                repost_count = val
                            elif key == "comment":
                                comment_count = val
                            else:
                                like_count = val
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"提取统计数据时出现异常: {e}")

        # 策略2(兜底): 使用旧的稳定选择器
        # 点赞数:button.woo-like-main span.woo-like-count
        if like_count == "0":
            try:
                like_els = self.driver.find_elements(
                    By.CSS_SELECTOR, "button.woo-like-main span.woo-like-count")
                if like_els:
                    val = like_els[0].text.strip()
                    if val:
                        like_count = val
            except Exception:
                pass
        # 转发/评论:通过 wrap/num 类名直接匹配
        if repost_count == "0" and wrap_cls and num_cls:
            try:
                wraps = self.driver.find_elements(By.CSS_SELECTOR, f"div.{wrap_cls}")
                for w in wraps[:6]:
                    try:
                        cls = w.get_attribute("class") or ""
                        num_els = w.find_elements(By.CSS_SELECTOR, f"span.{num_cls}")
                        if not num_els:
                            continue
                        val = num_els[0].text.strip()
                        if "retweet" in cls and repost_count == "0":
                            repost_count = val
                        elif "cur" in cls and comment_count == "0":
                            comment_count = val
                    except Exception:
                        continue
            except Exception:
                pass

        return repost_count, comment_count, like_count

    # ---------- Markdown 导出 ----------

    @staticmethod
    def generate_markdown(detail):
        """根据详情生成 Markdown 内容"""
        md = f">URL：{detail['url']}\n"
        md += f">发布时间：{detail['publish_time']}\n"
        md += f">词条：{detail['topics']}\n"
        md += f">正文字数：{len(detail['content'])}字符\n\n"
        md += "---\n\n正文：\n"
        md += f"{detail['content']}\n\n"
        md += "---\n\n"
        md += f">转发数：{detail['repost_count']}\n"
        md += f">评论数：{detail['comment_count']}\n"
        md += f">点赞数：{detail['like_count']}\n"
        md += f">保存时间：{detail['save_time']}"
        return md

    def export_markdowns(self, weibo_ids, user_id, user_name, base_dir,
                         min_interval=3, max_interval=8, progress_callback=None,
                         overwrite=False):
        """逐条抓取详情并导出 Markdown

        overwrite=True 时同名文件直接覆盖(重新抓取场景);
        否则已存在则加序号避免覆盖。

        返回 (成功数, 失败数)
        """
        output_dir = base_dir  # 已由调用方创建
        ok_count = 0
        fail_count = 0
        total = len(weibo_ids)

        for i, weibo_id in enumerate(weibo_ids):
            logger.info(f"正在处理第 {i + 1}/{total} 个微博: {weibo_id}")
            if progress_callback:
                progress_callback(i + 1, total, weibo_id)

            detail = self.get_weibo_detail(weibo_id, user_id)
            if not detail:
                fail_count += 1
                logger.error(f"无法获取微博 {weibo_id} 的详情")
                continue

            md_content = self.generate_markdown(detail)

            # 文件名: 用户名_日期_微博ID.md (日期来自发布时间)
            save_time = detail['publish_time']
            date_part = save_time.split()[0] if save_time and save_time != "未知时间" else "unknown"
            output_filename = f"{user_name}_{date_part}_{weibo_id}.md"
            output_path = os.path.join(output_dir, output_filename)

            if not overwrite:
                counter = 1
                original = output_path
                while os.path.exists(output_path):
                    name, ext = os.path.splitext(original)
                    output_path = f"{name}_{counter}{ext}"
                    counter += 1

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(md_content)
            ok_count += 1
            logger.info(f"已保存: {os.path.basename(output_path)}")

            if i < total - 1:
                sleep_time = random.randint(min_interval, max_interval)
                logger.info(f"等待 {sleep_time} 秒后处理下一个...")
                time.sleep(sleep_time)

        logger.info(f"导出完成: 成功 {ok_count} 条, 失败 {fail_count} 条")
        return ok_count, fail_count

    def get_user_weibo_list(self, user_id, is_ori=1, is_forward=1, is_text=1, is_pic=1,
                            is_video=1, is_music=1, keyword="", start_date=None, end_date=None,
                            max_count=50, pause=False):
        """兼容旧版 weibo_selenium_PC.py 的接口:获取用户微博列表

        与 collect_weibo_ids 等价;pause=True 时在收集完成后暂停等待用户确认
        """
        if not self.check_login_status():
            logger.warning("未检测到登录状态,尝试手动登录")
            if not self.manual_login():
                logger.error("登录失败,无法继续")
                return None, []

        search_url = self.build_search_url(
            user_id, is_ori, is_forward, is_text, is_pic,
            is_video, is_music, keyword, start_date, end_date)
        logger.info(f"正在访问URL: {search_url}")
        self.driver.get(search_url)
        time.sleep(3)
        self.click_search_button()
        time.sleep(5)

        username = self.get_username(user_id, fallback_name=user_id)

        card_cls = self.resolve_card_class(user_id)
        time_cls = self.resolve_time_class(user_id)
        weibo_ids = self._scroll_and_collect(user_id, card_cls, time_cls, max_count)

        if pause:
            if self.wait_callback:
                self.wait_callback("程序已暂停,请检查浏览器中的页面状态,然后点击「确定」继续...")
            else:
                input("程序已暂停,请检查浏览器中的页面状态。按回车键继续或关闭浏览器...")

        return username, weibo_ids

    # ---------- 文件辅助 ----------

    def save_weibo_ids_to_file(self, weibo_ids, filename):
        """将微博 ID 保存到文本文件,用逗号分隔"""
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, "w", encoding="utf-8") as f:
                f.write(",".join(weibo_ids))
            logger.info(f"微博ID已保存到文件: {filename}")
            return True
        except Exception as e:
            logger.error(f"保存微博ID到文件时出错: {e}")
            return False

    def close(self, force_close=True):
        """关闭浏览器"""
        try:
            if force_close:
                self.driver.quit()
                logger.info("浏览器已关闭")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 一键任务:收集 + 导出
# ---------------------------------------------------------------------------

def run_task(user_id, user_name, start_date, end_date,
             headless=False, user_data_dir=None, max_count=500,
             keyword="", is_ori=1, is_forward=0, is_text=1, is_pic=1,
             is_video=1, is_music=1, manual_callback=None,
             wait_callback=None, progress_callback=None, data_root=None,
             keep_browser_open=False, skip_export=False):
    """一键爬取任务:收集指定时间范围的微博ID并导出 Markdown

    参数:
      user_id / user_name    博主微博ID与昵称
      start_date / end_date  时间范围(YYYY-MM-DD,含两端)
      headless               无头模式
      user_data_dir          Edge 用户数据目录(复用登录态)
      manual_callback        手动输入类名的回调,签名 (key, current) -> str
      wait_callback          等待用户操作的回调(如手动登录后继续),签名 (message) -> None
      progress_callback      进度回调(用于 GUI 显示)
      data_root              数据输出根目录;None 时默认程序目录下的 DataPC
      keep_browser_open      完成后是否保留浏览器窗口
      skip_export            只收集ID,不导出Markdown

    返回 dict:
      {"username", "weibo_ids", "txt_file", "md_dir", "exported", "failed"}
    """
    ensure_logger()
    if not data_root:
        data_root = os.path.join(app_dir(), "DataPC")
    crawler = WeiboPCCrawler(headless=headless, user_data_dir=user_data_dir,
                             wait_callback=wait_callback,
                             manual_callback=manual_callback)

    # 日期 +1 天:抵消微博时间换算导致的实际搜索范围偏移(沿用原脚本经验)
    start_search = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    end_search = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    result = {
        "username": user_name, "weibo_ids": [], "txt_file": None,
        "md_dir": None, "exported": 0, "failed": 0,
    }

    try:
        # 1. 收集 ID
        username, weibo_ids = crawler.collect_weibo_ids(
            user_id=user_id,
            start_date=start_search,
            end_date=end_search,
            max_count=max_count,
            keyword=keyword,
            is_ori=is_ori, is_forward=is_forward,
            is_text=is_text, is_pic=is_pic,
            is_video=is_video, is_music=is_music,
            user_name=user_name,
        )
        if not weibo_ids:
            logger.warning(
                f"未收集到任何微博:博主 {user_name}({user_id}) 在 "
                f"{start_date} ~ {end_date} 期间可能没有发表符合条件(原创长文)的微博,"
                f"或该时间段内博主处于禁言/停更状态。请确认时间范围后重试。")
            return result
        if username and username != user_id:
            result["username"] = username

        # 2. 保存 ID 到 txt(文件名沿用用户输入的原始日期)
        data_dir = os.path.join(data_root, f"{result['username']}_{user_id}")
        os.makedirs(data_dir, exist_ok=True)
        txt_file = os.path.join(data_dir, f"{result['username']}_{user_id}_{start_date}_{end_date}.txt")
        crawler.save_weibo_ids_to_file(weibo_ids, txt_file)
        result["weibo_ids"] = weibo_ids
        result["txt_file"] = txt_file

        if skip_export:
            return result

        # 3. 导出 Markdown(重跑时覆盖同名文件,避免生成 _1 副本)
        md_dir = os.path.join(data_dir, f"{result['username']}_{user_id}_{start_date}_{end_date}")
        os.makedirs(md_dir, exist_ok=True)
        result["md_dir"] = md_dir
        ok_count, fail_count = crawler.export_markdowns(
            weibo_ids, user_id, result["username"], md_dir,
            progress_callback=progress_callback, overwrite=True)
        result["exported"] = ok_count
        result["failed"] = fail_count

    except RuntimeError as e:
        # 预期内的业务错误(如博主该时间段无微博、类名无法确定),只提示不打印堆栈
        logger.error(f"任务未完成: {e}")
        result["error"] = str(e)
    except Exception as e:
        logger.error(f"程序执行过程中出现错误: {e}", exc_info=True)
        result["error"] = str(e)
    finally:
        if not keep_browser_open:
            crawler.close(force_close=True)

    return result


if __name__ == "__main__":
    # 简单自测:查看默认类名配置
    ensure_logger()
    cm = ClassNameManager()
    logger.info(f"当前类名配置: {json.dumps(cm.classes, ensure_ascii=False, indent=2)}")
