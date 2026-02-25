#!/bin/bash
set -e

# ========== 启动 Xvfb ==========
Xvfb :99 -screen 0 1280x800x24 -ac &
sleep 1
export DISPLAY=:99

# ========== 启动 Python 应用 ==========
python -u main.py &
APP_PID=$!

# ========== 浏览器进程降优先级 ==========
renice_browser_processes() {
  if ! command -v renice >/dev/null 2>&1; then
    return
  fi

  PIDS="$(pgrep -f 'chrome|chromium|chromedriver' || true)"
  if [ -z "$PIDS" ]; then
    return
  fi

  for pid in $PIDS; do
    renice -n 19 -p "$pid" >/dev/null 2>&1 || true
  done
}

# ========== health 监控 ==========
HEALTH_URL="http://localhost:7860/health"
INTERVAL=10
FAIL_LIMIT=3
FAIL_COUNT=0

echo "[watchdog] start health checking..."

while true; do
  renice_browser_processes

  if curl -fs "$HEALTH_URL" > /dev/null; then
    FAIL_COUNT=0
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "[watchdog] healthcheck failed ($FAIL_COUNT/$FAIL_LIMIT)"
  fi

  if [ "$FAIL_COUNT" -ge "$FAIL_LIMIT" ]; then
    echo "[watchdog] unhealthy, killing app..."
    kill -TERM "$APP_PID"
    sleep 2
    kill -KILL "$APP_PID" || true
    exit 1   # ⭐ 关键：让 Docker 认为容器异常退出
  fi

  sleep "$INTERVAL"
done
