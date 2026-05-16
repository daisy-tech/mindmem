#!/usr/bin/env bash
# 本地启动（无需 Docker）：Redis + Qdrant + 后端 + Celery + 前端
set -euo pipefail
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:$PATH"
ROOT="$(pwd)"

if [ ! -f .env ]; then
  echo "缺少 .env，请先配置 OPENAI_API_KEY"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "警告: .env 中 OPENAI_API_KEY 为空，登录可用，但聊天/记忆需要有效 Key"
fi

# Redis
if ! redis-cli ping >/dev/null 2>&1; then
  echo "→ 启动 Redis..."
  brew services start redis
  sleep 1
fi

# Qdrant
_qdrant_ok() { curl -sf http://localhost:6333/ >/dev/null 2>&1; }
if [ -f "$ROOT/backend/data/qdrant.pid" ]; then
  _qp=$(cat "$ROOT/backend/data/qdrant.pid")
  if ! kill -0 "$_qp" 2>/dev/null; then
    rm -f "$ROOT/backend/data/qdrant.pid"
  fi
fi
if ! _qdrant_ok; then
  echo "→ 启动 Qdrant..."
  mkdir -p "$ROOT/backend/data/qdrant_storage"
  nohup "$ROOT/.tools/qdrant/qdrant" \
    --config-path "$ROOT/backend/data/qdrant.yaml" \
    > "$ROOT/backend/data/qdrant.log" 2>&1 &
  echo $! > "$ROOT/backend/data/qdrant.pid"
  for _ in {1..30}; do
    _qdrant_ok && break
    sleep 1
  done
fi
if ! _qdrant_ok; then
  echo "错误: Qdrant 未就绪，请查看 backend/data/qdrant.log"
  exit 1
fi

export QDRANT_HOST=localhost
export REDIS_URL=redis://localhost:6379/0
export USER_DB_PATH="$ROOT/backend/data/memobot.db"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export DEV_MODE="${DEV_MODE:-true}"

mkdir -p "$(dirname "$USER_DB_PATH")"

# Python venv
if [ ! -d backend/.venv ]; then
  python3.12 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements.txt
fi

# Celery
if [ ! -f backend/data/celery.pid ] || ! kill -0 "$(cat backend/data/celery.pid)" 2>/dev/null; then
  echo "→ 启动 Celery..."
  cd backend
  nohup .venv/bin/celery -A celery_worker worker --loglevel=info \
    > data/celery.log 2>&1 &
  echo $! > data/celery.pid
  cd "$ROOT"
fi

# Backend
if [ ! -f backend/data/backend.pid ] || ! kill -0 "$(cat backend/data/backend.pid)" 2>/dev/null; then
  echo "→ 启动后端 http://localhost:8000 ..."
  cd backend
  nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \
    > data/backend.log 2>&1 &
  echo $! > data/backend.pid
  cd "$ROOT"
fi

# Frontend
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi
if [ ! -f frontend/data/frontend.pid ] || ! kill -0 "$(cat frontend/data/frontend.pid)" 2>/dev/null; then
  mkdir -p frontend/data
  echo "→ 启动前端 http://localhost:5173 ..."
  cd frontend
  nohup npm run dev -- --host 0.0.0.0 --port 5173 \
    > data/dev.log 2>&1 &
  echo $! > data/frontend.pid
  cd "$ROOT"
fi

echo ""
echo "=========================================="
echo "  MemoBot 本地已启动"
echo "  前端:  http://localhost:5173"
echo "  后端:  http://localhost:8000/health"
echo "  停止:  ./stop-local.sh"
echo "=========================================="
