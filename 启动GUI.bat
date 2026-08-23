@echo off
rem ============================================================
rem  weibo-md-exporter GUI 启动脚本
rem  1) 优先使用本目录 venv;2) 无 venv 则提示先运行 安装.bat
rem ============================================================
chcp 936 >nul
cd /d %~dp0

if exist "%~dp0venv\Scripts\pythonw.exe" (
    start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0weibo_crawler_gui.py"
    exit /b 0
)

rem 无 venv: 尝试系统 pythonw(需已装依赖)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0weibo_crawler_gui.py"
    exit /b 0
)

echo.
echo [错误] 未找到可用的 Python 环境。
echo 请先双击运行 安装.bat 完成环境配置,再运行本脚本。
echo.
pause
exit /b 1