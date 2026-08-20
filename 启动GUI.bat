@echo off
rem 微博爬虫 GUI 启动脚本(使用 pythonw.exe 运行,不显示命令行黑窗口)
cd /d %~dp0

set "PYTHONW=%~dp0venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

start "" "%PYTHONW%" "%~dp0weibo_crawler_gui.py"
exit /b 0
