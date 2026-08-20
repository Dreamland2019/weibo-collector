#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微博爬虫 - 命令行入口
=====================
用法示例:
  # 全参数指定
  python weibo_crawler_cli.py --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30

  # 只收集微博ID,不导出Markdown
  python weibo_crawler_cli.py --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30 --no-export

  # 使用无头模式 + 手动指定卡片类名(跳过自动探测)
  python weibo_crawler_cli.py --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30 --headless --card-class _body_ecgcn_63

  # 不带参数直接运行 -> 交互式提问
  python weibo_crawler_cli.py

可选参数(类名覆盖,用于自动探测失败时手动指定):
  --card-class     微博卡片类名(如 _body_ecgcn_63)
  --time-class     时间链接类名(如 _time_1tpft_33)
  --name-class     用户名类名(如 _name_1yc79_291)
  --content-class  正文类名(如 _wbtext_q1l14_14)
  --wrap-class     转评赞容器类名(如 _wrap_198pe_137)
  --num-class      转评赞数字类名(如 _num_198pe_46)
"""

import argparse
import logging
import os
import sys
from datetime import datetime

from weibo_crawler_core import (
    ensure_logger, ClassNameManager, WeiboPCCrawler, run_task,
    resource_path,
)

logger = logging.getLogger('weibo_crawler')

# 默认使用项目目录下的 EdgeUserData(登录状态保存在本地,便于迁移与分享)
DEFAULT_USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EdgeUserData")


def parse_args():
    parser = argparse.ArgumentParser(
        description="微博爬虫:收集指定博主指定时间范围的微博并导出Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--name", help="博主昵称,如: 卢诗翰")
    parser.add_argument("--uid", help="博主微博ID,如: 3276099007")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD,如 2026-04-01")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD,如 2026-04-30")

    parser.add_argument("--user-data-dir", default=DEFAULT_USER_DATA_DIR,
                        help=f"Edge用户数据目录(复用登录态),默认: {DEFAULT_USER_DATA_DIR}")
    parser.add_argument("--headless", action="store_true", help="无头模式(不显示浏览器窗口)")
    parser.add_argument("--max-count", type=int, default=500, help="最大微博数量,默认500")
    parser.add_argument("--keyword", default="", help="搜索关键词,默认空")
    parser.add_argument("--no-export", action="store_true", help="只收集微博ID,不导出Markdown")
    parser.add_argument("--keep-browser", action="store_true", help="完成后保留浏览器窗口")
    parser.add_argument("--data-root", default="DataPC", help="数据输出根目录,默认DataPC")

    # 类名覆盖参数(自动探测失败时手动指定)
    parser.add_argument("--card-class", dest="card_class", help="微博卡片类名(覆盖)")
    parser.add_argument("--time-class", dest="time_class", help="时间链接类名(覆盖)")
    parser.add_argument("--name-class", dest="name_class", help="用户名类名(覆盖)")
    parser.add_argument("--content-class", dest="content_class", help="正文类名(覆盖)")
    parser.add_argument("--wrap-class", dest="wrap_class", help="转评赞容器类名(覆盖)")
    parser.add_argument("--num-class", dest="num_class", help="转评赞数字类名(覆盖)")
    return parser.parse_args()


def interactive_input(prompt, default=None):
    """交互式输入,支持默认值"""
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val or default
    return input(f"{prompt}: ").strip()


def manual_callback_cli(key, current):
    """类名手动输入回调(CLI 版):提示用户输入类名"""
    key_names = {
        "card": "微博卡片类名(div)",
        "time": "时间链接类名(a)",
        "name": "用户名类名(div)",
        "content": "正文类名(div)",
        "stats_wrap": "转评赞容器类名(div)",
        "stats_num": "转评赞数字类名(span)",
    }
    print()
    print("=" * 50)
    print("!! 自动识别类名失败,请手动输入类名 !!")
    print(f"   目标: {key_names.get(key, key)}")
    print(f"   当前值: {current or '(空)'}")
    print("   提示: 在浏览器中按 F12 打开开发者工具,选中元素查看 class 属性")
    print("=" * 50)
    try:
        val = input(f"请输入{key_names.get(key, key)}的类名(直接回车放弃): ").strip()
    except EOFError:
        # 无交互终端(如后台运行)时视为放弃输入
        logger.warning("当前环境无法交互输入,视为放弃手动输入类名")
        return ""
    return val


def apply_class_overrides(args, class_manager):
    """将命令行指定的类名覆盖写入类名管理器"""
    overrides = {
        "card": args.card_class,
        "time": args.time_class,
        "name": args.name_class,
        "content": args.content_class,
        "stats_wrap": args.wrap_class,
        "stats_num": args.num_class,
    }
    changed = False
    for key, val in overrides.items():
        if val:
            class_manager.set(key, val)
            changed = True
    if changed:
        class_manager.save()


def main():
    ensure_logger()
    args = parse_args()

    # 参数收集:有缺省时交互式提问
    user_name = args.name
    user_id = args.uid
    start_date = args.start
    end_date = args.end

    if not (user_id and start_date and end_date):
        print()
        print("进入交互模式(也可直接使用命令行参数,见 --help):")
        user_name = interactive_input("博主昵称", user_name or "卢诗翰")
        user_id = interactive_input("博主微博ID", user_id or "3276099007")
        today = datetime.now().strftime("%Y-%m-%d")
        start_date = interactive_input("开始日期(YYYY-MM-DD)", start_date)
        end_date = interactive_input("结束日期(YYYY-MM-DD)", end_date or today)

    # 简单校验日期格式
    for d in (start_date, end_date):
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            logger.error(f"日期格式错误: {d},应为 YYYY-MM-DD")
            sys.exit(1)

    logger.info(f"任务参数: 博主={user_name}({user_id}), 时间={start_date} ~ {end_date}")
    logger.info(f"用户数据目录: {args.user_data_dir}")

    # 手动输入回调(自动探测失败时触发)
    def manual_cb(key, current):
        return manual_callback_cli(key, current)

    # 类名管理器(先应用命令行覆盖)
    cm = ClassNameManager(manual_callback=manual_cb)
    apply_class_overrides(args, cm)

    # 运行任务
    result = run_task(
        user_id=user_id,
        user_name=user_name,
        start_date=start_date,
        end_date=end_date,
        headless=args.headless,
        user_data_dir=args.user_data_dir,
        max_count=args.max_count,
        keyword=args.keyword,
        manual_callback=manual_cb,
        data_root=args.data_root,
        keep_browser_open=args.keep_browser,
        skip_export=args.no_export,
    )

    # 结果输出
    print()
    print("=" * 60)
    print("任务完成,结果汇总:")
    print(f"  博主: {result['username']} ({user_id})")
    print(f"  时间: {start_date} ~ {end_date}")
    if result.get("error"):
        print(f"  状态: 未完成 - {result['error']}")
    print(f"  收集到微博: {len(result['weibo_ids'])} 条")
    if result.get("txt_file"):
        print(f"  ID文件: {result['txt_file']}")
    if result.get("md_dir"):
        print(f"  MD目录: {result['md_dir']}")
        print(f"  导出成功: {result['exported']} 条, 失败: {result['failed']} 条")
    print("=" * 60)


if __name__ == "__main__":
    main()
