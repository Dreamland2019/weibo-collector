@echo off
rem ============================================================
rem  weibo-md-exporter 一键安装脚本 (Windows)
rem  功能: 检测Python -> 创建虚拟环境 -> 安装依赖
rem  驱动: 程序运行时由 Selenium Manager 自动下载,无需手动安装
rem ============================================================
chcp 936 >nul
cd /d %~dp0
setlocal enabledelayedexpansion

echo.
echo ============================================
echo   weibo-md-exporter 一键安装
echo ============================================
echo.

rem ---------- 1. 检测 Python ----------
set "PYTHON_CMD="
set "PY_OK=0"
where python >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%i in ('python -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"' 2^>nul) do set "PY_OK=%%i"
    if "!PY_OK!"=="1" set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    where py >nul 2>nul
    if %errorlevel%==0 (
        for /f "delims=" %%i in ('py -3 -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"' 2^>nul) do set "PY_OK=%%i"
        if "!PY_OK!"=="1" set "PYTHON_CMD=py -3"
    )
)

if not defined PYTHON_CMD (
    echo [错误] 未检测到 Python 3.10 或更高版本。
    echo.
    echo 请先安装 Python:
    echo   1. 打开 https://www.python.org/downloads/
    echo   2. 下载 Python 3.12 并安装
    echo   3. 安装时务必勾选 "Add python.exe to PATH"
    echo   4. 安装完成后重新运行本脚本
    echo.
    pause
    exit /b 1
)

echo [1/3] 检测到 Python: %PYTHON_CMD%
echo.

rem ---------- 2. 创建虚拟环境 ----------
if not exist "venv" (
    echo [2/3] 正在创建虚拟环境...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [2/3] 虚拟环境已存在,跳过
)
echo.

rem ---------- 3. 安装依赖 ----------
echo [3/3] 正在安装依赖(首次约需1-2分钟,请耐心等待)...
"venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [警告] 使用清华镜像安装失败,尝试官方源...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo.
echo ============================================
echo   安装完成!
echo.
echo   使用方式:
echo     1. 双击 启动GUI.bat   -^> 图形界面
echo     2. 如需命令行: 命令行爬取.bat
echo.
echo   首次运行会弹出浏览器,请扫码登录微博一次
echo   (登录状态保存在本地,下次无需再登录)
echo ============================================
echo.
pause