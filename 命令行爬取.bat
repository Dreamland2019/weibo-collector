@echo off
rem 微博爬虫 CLI 交互模式(直接双击即可按提示输入博主/日期运行)
cd /d %~dp0

set "PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"

"%PYTHON%" "%~dp0weibo_crawler_cli.py" %*
echo.
pause
