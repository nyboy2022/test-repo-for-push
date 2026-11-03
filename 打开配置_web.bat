@echo off
chcp 65001 >nul
title Discord Channel Message Forwarder - 配置 (Web)

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

pip install -r requirements.txt -q

python config_web.py
