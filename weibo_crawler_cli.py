#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微博爬虫 - 命令行入口
=====================
用法示例(爬取):
  python weibo_crawler_cli.py crawl --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30
  python weibo_crawler_cli.py crawl --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30 --no-export
  python weibo_crawler_cli.py crawl --name 卢诗翰 --uid 3276099007 --start 2026-04-01 --end 2026-04-30 --headless
  # 不带参数直接运行 -> 交互式提问(默认爬取)

用法示例(筛选本地已爬取数据):
  python weibo_crawler_cli.py filter --name 卢诗翰 --uid 3276099007 --start 2025-01-01 --end 2025-06-30 --top 10
  python weibo_crawler_cli.py filter --name 卢诗翰 --uid 3276099007 --start 2026-01-01 --end 2026-07-31 --top 5 --no-repost
  # 可选: --format docx(筛选docx文件) --move(移动而非复制) --filter-root 自定义输出目录
"""

import argparse
import logging
import os
import sys
from datetime import datetime

from weibo_crawler_core import (
    ensure_logger, ClassNameManager, WeiboPCCrawler, run_task,
    resource_path, app_dir, ArticleFilter,
)

logger = logging.getLogger('weibo_crawler')

# 默认使用程序目录下的 EdgeUserData(登录状态保存在本地,便于迁移与分享)
DEFAULT_USER_DATA_DIR = os.path.join(app_dir(), "EdgeUserData")


def parse_args():
    parser = argparse.ArgumentParser(
        description="微博爬虫:收集指定博主指定时间范围的微博并导出Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # ---- 爬取子命令 ----
    p_crawl = sub.add_parser("crawl", help="爬取微博并导出(默认)")
    p_crawl.add_argument("--name", help="博主昵称,如: 卢诗翰")
    p_crawl.add_argument("--uid", help="博主微博ID,如: 3276099007")
    p_crawl.add_argument("--start", help="开始日期 YYYY-MM-DD,如 2026-04-01")
    p_crawl.add_argument("--end", help="结束日期 YYYY-MM-DD,如 2026-04-30")
    p_crawl.add_argument("--user-data-dir", default=DEFAULT_USER_DATA_DIR,
                         help=f"Edge用户数据目录(复用登录态),默认: {DEFAULT_USER_DATA_DIR}")
    p_crawl.add_argument("--headless", action="store_true", help="无头模式(不显示浏览器窗口)")
    p_crawl.add_argument("--max-count", type=int, default=500, help="最大微博数量,默认500")
    p_crawl.add_argument("--keyword", default="", help="搜索关键词,默认空")
    p_crawl.add_argument("--no-export", action="store_true", help="只收集微博ID,不导出Markdown")
    p_crawl.add_argument("--keep-browser", action="store_true", help="完成后保留浏览器窗口")
    p_crawl.add_argument("--data-root", default=None, help="数据输出根目录,默认程序目录下DataPC")
    p_crawl.add_argument("--min-interval", type=int, default=3,
                         help="每条微博之间最小等待秒数,默认3(勿设置过短,避免风控)")
    p_crawl.add_argument("--max-interval", type=int, default=8,
                         help="每条微博之间最大等待秒数,默认8")
    p_crawl.add_argument("--download-images", action="store_true",
                         help="同时下载微博中的图片到本地")
    p_crawl.add_argument("--download-videos", action="store_true",
                         help="同时下载微博中的视频到本地")
    p_crawl.add_argument("--format", dest="export_format", default="md",
                         choices=["md", "docx"], help="导出格式,默认md(支持docx)")
    p_crawl.add_argument("--card-class", dest="card_class", help="微博卡片类名(覆盖)")
    p_crawl.add_argument("--time-class", dest="time_class", help="时间链接类名(覆盖)")
    p_crawl.add_argument("--name-class", dest="name_class", help="用户名类名(覆盖)")
    p_crawl.add_argument("--content-class", dest="content_class", help="正文类名(覆盖)")
    p_crawl.add_argument("--wrap-class", dest="wrap_class", help="转评赞容器类名(覆盖)")
    p_crawl.add_argument("--num-class", dest="num_class", help="转评赞数字类名(覆盖)")
    p_crawl.add_argument("--skip-existing", action="store_true",
                         help="跳过已存在同ID文件的微博(避免重复抓取)")
    p_crawl.add_argument("--min-words", type=int, default=0,
                         help="正文字数低于该值的文章不导出(0=不限制)")
    p_crawl.set_defaults(command="crawl")

    # ---- 筛选子命令 ----
    p_filter = sub.add_parser("filter", help="对本地已爬取数据按转评赞筛选排序")
    p_filter.add_argument("--name", help="博主昵称,如: 卢诗翰")
    p_filter.add_argument("--uid", help="博主微博ID,如: 3276099007")
    p_filter.add_argument("--start", help="开始日期 YYYY-MM-DD,如 2026-01-01")
    p_filter.add_argument("--end", help="结束日期 YYYY-MM-DD,如 2026-06-30")
    p_filter.add_argument("--top", type=int, default=10, help="输出篇数,默认10")
    p_filter.add_argument("--no-repost", action="store_true", help="不计入转发数")
    p_filter.add_argument("--no-comment", action="store_true", help="不计入评论数")
    p_filter.add_argument("--no-like", action="store_true", help="不计入点赞数")
    p_filter.add_argument("--by-word-count", action="store_true",
                          help="按正文字数排序(替代转评赞之和)")
    p_filter.add_argument("--format", dest="source_format", default="md",
                          choices=["md", "docx"], help="筛选的数据文件格式,默认md")
    p_filter.add_argument("--move", action="store_true", help="移动而非复制原文件")
    p_filter.add_argument("--data-root", default=None, help="数据根目录,默认程序目录下DataPC")
    p_filter.add_argument("--filter-root", default="筛选", help="筛选输出根目录,默认同目录下'筛选'")
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


def cmd_crawl(args):
    """爬取子命令: 收集微博并导出"""
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

    for d in (start_date, end_date):
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            logger.error(f"日期格式错误: {d},应为 YYYY-MM-DD")
            sys.exit(1)

    logger.info(f"任务参数: 博主={user_name}({user_id}), 时间={start_date} ~ {end_date}")
    logger.info(f"用户数据目录: {args.user_data_dir}")

    def manual_cb(key, current):
        return manual_callback_cli(key, current)

    def wait_cb(message):
        input(f"{message}\n完成后按回车继续...")

    cm = ClassNameManager(manual_callback=manual_cb)
    apply_class_overrides(args, cm)

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
        wait_callback=wait_cb,
        data_root=args.data_root,
        keep_browser_open=args.keep_browser,
        skip_export=args.no_export,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        download_images=args.download_images,
        download_videos=args.download_videos,
        export_format=args.export_format,
        skip_existing=args.skip_existing,
        min_words=args.min_words,
    )

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
        print(f"  导出成功: {result['exported']} 条, 失败: {result['failed']} 条, 跳过: {result['skipped']} 条")
    print("=" * 60)


def cmd_filter(args):
    """筛选子命令: 对本地已爬取数据按转评赞之和排序筛选"""
    user_name = args.name
    user_id = args.uid
    start_date = args.start
    end_date = args.end

    if not (user_id and start_date and end_date):
        print()
        print("进入交互模式(筛选):")
        af = ArticleFilter(data_root=args.data_root, filter_root=args.filter_root)
        bloggers = af.list_bloggers()
        if bloggers:
            print("DataPC 中已有的博主:")
            for nm, uid in bloggers:
                print(f"  {nm} ({uid})")
        user_name = interactive_input("博主昵称", user_name or (bloggers[0][0] if bloggers else ""))
        user_id = interactive_input("博主微博ID", user_id or (bloggers[0][1] if bloggers else ""))
        start_date = interactive_input("开始日期(YYYY-MM-DD)", start_date)
        end_date = interactive_input("结束日期(YYYY-MM-DD)", end_date)

    for d in (start_date, end_date):
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            logger.error(f"日期格式错误: {d},应为 YYYY-MM-DD")
            sys.exit(1)

    logger.info(f"筛选参数: 博主={user_name}({user_id}), 时间={start_date} ~ {end_date}, "
                f"篇数={args.top}, 格式={args.source_format}")

    af = ArticleFilter(data_root=args.data_root, filter_root=args.filter_root)
    result = af.filter_top(
        user_name, user_id, start_date, end_date,
        top_n=args.top,
        use_repost=not args.no_repost,
        use_comment=not args.no_comment,
        use_like=not args.no_like,
        source_format=args.source_format,
        move=args.move,
        by_word_count=args.by_word_count,
    )
    print()
    print("=" * 60)
    if result["output_dir"]:
        print(f"筛选完成,共 {len(result['items'])} 篇,输出到:")
        print(f"  {result['output_dir']}")
        print("文件名已按排名添加序号前缀,统计明细见文件夹内'筛选说明.txt'")
    else:
        print("筛选未完成(未找到符合条件的文件),请检查博主/日期/格式是否正确")
    print("=" * 60)


def main():
    ensure_logger()
    args = parse_args()
    if args.command == "filter":
        cmd_filter(args)
    else:
        cmd_crawl(args)


if __name__ == "__main__":
    main()
