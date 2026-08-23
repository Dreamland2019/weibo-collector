@echo off
rem weibo-md-exporter CLI 入口(需先运行 安装.bat)
chcp 936 >nul
cd /d %~dp0

set "PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"

"%PYTHON%" "%~dp0weibo_crawler_cli.py" %*
echo.
pause