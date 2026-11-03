@echo off
chcp 65001 >nul
title Discord 消息转发

cd /d "%~dp0"

echo ========================================
echo   Discord 消息转发 - 一键启动
echo ========================================
echo.

:: 检查 Python 是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 安装依赖
echo [1/2] 正在检查/安装依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo [2/2] 正在启动...
echo.
python discord_forwarder.py

pause
