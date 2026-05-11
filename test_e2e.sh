#!/usr/bin/env bash
# MemoBot 端对端自测脚本
# Usage: ./test_e2e.sh
# 在 memobot 目录下运行，要求 docker compose 服务全部启动完毕
set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass=0; fail=0

ok() { echo -e "${GREEN}✓${NC} $1"; pass=$((pass+1)); }
no() { echo -e "${RED}✗${NC} $1"; fail=$((fail+1)); }
info() { echo -e "${YELLOW}→${NC} $1"; }

wait_for_health() {
  info "等待后端就绪..."
  for i in {1..30}; do
    if curl -s http://localhost:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
      ok "后端 /health 就绪"
      return 0
    fi
    sleep 1
  done
  no "后端启动超时"; exit 1
}

test_containers() {
  info "1. 检查容器状态"
  local running
  running=$(docker compose ps --services --filter "status=running" | wc -l | tr -d ' ')
  local total
  total=$(docker compose config --services | wc -l | tr -d ' ')
  if [ "$running" = "$total" ]; then
    ok "全部 $total 个容器在运行"
  else
    no "仅 $running/$total 个容器运行"
    docker compose ps
  fi
}

test_cors() {
  info "2. 验证 CORS 配置"
  local headers
  headers=$(curl -sI -H "Origin: http://localhost:5175" http://localhost:8000/health)
  if echo "$headers" | grep -qi "access-control-allow-origin: \*"; then
    ok "allow-origin: *"
  else
    no "缺少 allow-origin"
  fi
  if echo "$headers" | grep -qi "access-control-allow-credentials: true"; then
    no "仍带 allow-credentials: true（与 allow-origin:* 冲突）"
  else
    ok "无 allow-credentials（浏览器可正常读 EventSource）"
  fi
}

test_sse_stream() {
  info "3. 测试 SSE 聊天接口"
  local body
  body=$(curl -s --max-time 30 \
    "http://localhost:8000/api/chat/stream?message=hello&user_id=e2e_test&history=%5B%5D")
  if echo "$body" | grep -q '^data: {"content":'; then
    ok "SSE 返回 data 块"
  else
    no "SSE 未返回 data 块"; echo "$body" | head -5
  fi
  if echo "$body" | grep -q '^data: \[DONE\]'; then
    ok "SSE 返回 [DONE] 终止标记"
  else
    no "SSE 未发送 [DONE]"
  fi
}

test_memory_api() {
  info "4. 测试 memory 查询接口"
  local resp
  resp=$(curl -s http://localhost:8000/api/memory/e2e_test)
  if echo "$resp" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    ok "memory 接口返回合法 JSON"
  else
    no "memory 接口返回非 JSON"; echo "$resp"
  fi
}

test_chinese_memory() {
  info "5. 验证记忆提取为中文（直接走 Mem0Engine）"
  local out
  out=$(docker compose exec -T \
    -e HTTP_PROXY= -e HTTPS_PROXY= -e ALL_PROXY= \
    -e http_proxy= -e https_proxy= -e all_proxy= \
    -e NO_PROXY= -e no_proxy= \
    backend python -c "
from app.services.mem0_engine import Mem0Engine
import time
m = Mem0Engine()
for mem in m.get_all('e2e_zh').get('results', []):
    m.client.delete(mem['id'])
m.add([
    {'role':'user','content':'我今天去爬了香山，和家人一起'},
    {'role':'assistant','content':'听起来不错'},
], user_id='e2e_zh')
time.sleep(2)
for mem in m.get_all('e2e_zh').get('results', []):
    print('MEM:', mem.get('memory',''))
" 2>&1 | grep "^MEM:" || echo "NONE")
  if [ "$out" = "NONE" ] || [ -z "$out" ]; then
    no "未提取到任何记忆"
  else
    echo "$out" | sed 's/^/    /'
    # Simple heuristic: memory contains Chinese chars
    if echo "$out" | LC_ALL=C grep -q '[^[:print:][:space:]]'; then
      ok "记忆包含中文字符"
    else
      no "记忆看起来仍是纯英文"
    fi
  fi
}

test_celery_config() {
  info "6. 验证 Celery worker 已加载最新 custom_instructions"
  local out
  out=$(docker compose exec -T \
    -e HTTP_PROXY= -e HTTPS_PROXY= -e ALL_PROXY= \
    -e http_proxy= -e https_proxy= -e all_proxy= \
    -e NO_PROXY= -e no_proxy= \
    celery python -c "
from app.services.mem0_engine import Mem0Engine
m = Mem0Engine()
ci = m.client.config.custom_instructions or ''
print('OK' if '简体中文' in ci else 'BAD')
" 2>&1 | tail -1)
  if [ "$out" = "OK" ]; then
    ok "Celery custom_instructions 包含中文要求"
  else
    no "Celery custom_instructions 异常: $out"
  fi
}

test_frontend() {
  info "7. 前端页面可访问"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5175)
  if [ "$code" = "200" ]; then
    ok "frontend 返回 200"
  else
    no "frontend 返回 $code"
  fi
}

wait_for_health
test_containers
test_cors
test_sse_stream
test_memory_api
test_chinese_memory
test_celery_config
test_frontend

echo ""
echo "===================="
echo -e "通过: ${GREEN}$pass${NC}   失败: ${RED}$fail${NC}"
echo "===================="
[ "$fail" -eq 0 ] || exit 1
