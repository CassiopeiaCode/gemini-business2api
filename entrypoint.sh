#!/bin/bash
set -e

# 启动 Xvfb 在后台
Xvfb :99 -screen 0 1280x800x24 -ac &

# 等待 Xvfb 启动
sleep 1

# 设置 DISPLAY 环境变量
export DISPLAY=:99

# 启动 Python 应用（使用 nice 19 降低优先级）
exec nice -n 19 python -u main.py