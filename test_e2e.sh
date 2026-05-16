#!/usr/bin/env bash
# MemoBot 端对端自测脚本
# Usage: ./test_e2e.sh
# 在 memobot 目录下运行，要求 docker compose 服务全部启动完毕
set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass=0; fail=0
TOKEN=""
USER_ID=""
TEST_PHONE="138$(printf '%08d' $((RANDOM % 100000000)))"

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
  local running total
  running=$(docker compose ps --services --filter "status=running" | wc -l | tr -d ' ')
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
    no "仍带 allow-credentials: true"
  else
    ok "无 allow-credentials"
  fi
}

test_auth_send_code() {
  info "3. 发送短信验证码 (DEV_MODE)"
  local resp
  resp=$(curl -s -X POST http://localhost:8000/api/auth/phone/send-code \
    -H 'Content-Type: application/json' \
    -d "{\"phone\":\"$TEST_PHONE\"}")
  local code
  code=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dev_code',''))" 2>/dev/null || echo "")
  if [ -n "$code" ] && [ "${#code}" = "6" ]; then
    ok "获取到 6 位验证码: $code"
    AUTH_CODE="$code"
  else
    no "未获取到 dev_code: $resp"
    return 1
  fi
}

test_auth_invalid_phone() {
  info "4. 非法手机号应被拒绝"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    http://localhost:8000/api/auth/phone/send-code \
    -H 'Content-Type: application/json' \
    -d '{"phone":"abc"}')
  if [ "$code" = "400" ]; then
    ok "非法手机号返回 400"
  else
    no "非法手机号返回 $code"
  fi
}

test_auth_login() {
  info "5. 手机号 + 验证码登录"
  local resp
  resp=$(curl -s -X POST http://localhost:8000/api/auth/phone/login \
    -H 'Content-Type: application/json' \
    -d "{\"phone\":\"$TEST_PHONE\",\"code\":\"$AUTH_CODE\"}")
  TOKEN=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
  USER_ID=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user',{}).get('id',''))" 2>/dev/null || echo "")
  if [ -n "$TOKEN" ] && [ -n "$USER_ID" ]; then
    ok "登录成功，token 长度 ${#TOKEN}, user_id=$USER_ID"
  else
    no "登录失败: $resp"
    exit 1
  fi
}

test_auth_wrong_code() {
  info "6. 错误验证码应被拒绝"
  local rand_phone="139$(printf '%08d' $((RANDOM % 100000000)))"
  curl -s -X POST http://localhost:8000/api/auth/phone/send-code \
    -H 'Content-Type: application/json' -d "{\"phone\":\"$rand_phone\"}" > /dev/null
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    http://localhost:8000/api/auth/phone/login \
    -H 'Content-Type: application/json' \
    -d "{\"phone\":\"$rand_phone\",\"code\":\"000000\"}")
  if [ "$code" = "400" ]; then
    ok "错误验证码返回 400"
  else
    no "错误验证码返回 $code"
  fi
}

test_auth_me() {
  info "7. /api/auth/me 需要 token"
  local no_token_code
  no_token_code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/auth/me)
  if [ "$no_token_code" = "401" ] || [ "$no_token_code" = "403" ]; then
    ok "无 token 返回 $no_token_code"
  else
    no "无 token 返回 $no_token_code"
  fi
  local resp
  resp=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/auth/me)
  if echo "$resp" | grep -q "\"id\":\"$USER_ID\""; then
    ok "携带 token 可获取用户信息"
  else
    no "携带 token 返回异常: $resp"
  fi
}

test_memory_requires_auth() {
  info "8. memory 接口需要 token"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/memory)
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    ok "无 token memory 返回 $code"
  else
    no "无 token memory 返回 $code"
  fi
  local resp
  resp=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/memory)
  if echo "$resp" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    ok "携带 token 返回合法 JSON"
  else
    no "携带 token 返回非 JSON: $resp"
  fi
}

test_sse_stream() {
  info "9. SSE 聊天（使用 token 参数）"
  local encoded_token
  encoded_token=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$TOKEN")
  local body
  body=$(curl -s --max-time 30 \
    "http://localhost:8000/api/chat/stream?message=hello&token=$encoded_token&history=%5B%5D")
  if echo "$body" | grep -q '^data: {"content":'; then
    ok "SSE 返回 data 块"
  else
    no "SSE 未返回 data 块"; echo "$body" | head -5
  fi
  if echo "$body" | grep -q '^data: \[DONE\]'; then
    ok "SSE 返回 [DONE]"
  else
    no "SSE 未发送 [DONE]"
  fi
}

test_sse_invalid_token() {
  info "10. 错误 token 的 SSE 应返回 401"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    "http://localhost:8000/api/chat/stream?message=hi&token=bad&history=%5B%5D")
  if [ "$code" = "401" ]; then
    ok "错误 token 返回 401"
  else
    no "错误 token 返回 $code"
  fi
}

test_celery_config() {
  info "11. Celery custom_instructions 中文配置"
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
  info "12. 前端页面可访问"
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
test_auth_send_code
test_auth_invalid_phone
test_auth_login
test_auth_wrong_code
test_auth_me
test_memory_requires_auth
test_sse_stream
test_sse_invalid_token
test_celery_config
test_frontend

echo ""
echo "===================="
echo -e "通过: ${GREEN}$pass${NC}   失败: ${RED}$fail${NC}"
echo "===================="
[ "$fail" -eq 0 ] || exit 1
