#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

stop_pid() {
  local f="$1"
  if [ -f "$f" ]; then
    local pid
    pid=$(cat "$f")
    kill "$pid" 2>/dev/null && echo "已停止 PID $pid ($f)" || true
    rm -f "$f"
  fi
}

stop_pid frontend/data/frontend.pid
stop_pid backend/data/backend.pid
stop_pid backend/data/celery.pid
stop_pid backend/data/qdrant.pid

echo "完成。Redis 仍由 brew services 管理，可用: brew services stop redis"
