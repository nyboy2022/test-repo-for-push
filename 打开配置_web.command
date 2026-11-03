#!/bin/bash
set -e

cd "$(dirname "$0")"

PYTHON_BIN="/opt/homebrew/bin/python3.11"
if [ ! -x "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "[错误] 未检测到 Python，请先安装 Python 3.11+"
    read -n 1 -s -r -p "按任意键退出..."
    echo
    exit 1
  fi
fi

if [ ! -d ".venv311" ]; then
  echo "创建虚拟环境 .venv311 ..."
  "$PYTHON_BIN" -m venv .venv311
fi

source .venv311/bin/activate

echo "正在升级 pip..."
python -m pip install --upgrade pip >/dev/null 2>&1 || true

echo "正在检查/安装依赖..."
pip install -r requirements.txt

echo "正在打开 Web 配置界面..."
python config_web.py
