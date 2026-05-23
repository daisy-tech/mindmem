# MindMem 1.0 技术设计文档

> 版本：1.0 | 日期：2026-05-23

---

## 1. 系统架构

### 1.1 整体架构

```
┌─────────────────────────────────────────────────┐
│                    Frontend                      │
│          Vue 3 + Pinia + Element Plus            │
│                   Port 5175                      │
└────────────────────┬────────────────────────────┘
                     │ HTTP / SSE
┌────────────────────▼────────────────────────────┐
│                   Backend                        │
│            FastAPI (Async)  Port 8000            │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐ │
│  │  chat.py │  │memory.py │  │profile/events  │ │
│  └────┬─────┘  └────┬─────┘  └───────┬────────┘ │
│       │             │                 │          │
│  ┌────▼─────────────▼─────────────────▼────────┐ │
│  │           Services Layer                    │ │
│  │  mem0_engine  profile_engine  event_engine  │ │
│  └──────────────────────┬──────────────────────┘ │
└─────────────────────────┼───────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
┌────────▼───┐  ┌─────────▼──────┐  ┌─────▼──────┐
│   SQLite   │  │     Qdrant     │  │   Redis    │
│ User/Profile│  │Vector Storage  │  │Celery Broker│
│ Events/Conv │  │ (Mem0+Events)  │  │            │
└────────────┘  └────────────────┘  └─────┬──────┘
                                           │
                                  ┌────────▼──────┐
                                  │ Celery Worker │
                                  │ + Beat        │
                                  └───────────────┘
```

### 1.2 服务清单

| 服务 | 镜像/框架 | 端口 | 职责 |
|------|---------|------|------|
| backend | FastAPI (Python 3.11) | 8000 | API 服务 |
| celery | Celery 5 | - | 异步记忆提取 |
| celery-beat | Celery Beat | - | 定时任务（记忆衰减） |
| qdrant | qdrant/qdrant | 6333 | 向量数据库 |
| redis | redis:7-alpine | 6379 | Celery Broker |
| frontend | Vite + Vue 3 | 5175 | 前端 SPA |

---

## 2. 记忆架构（四层）

### 2.1 L1 情节记忆（Episodic Memory）

- **存储**：Qdrant（向量） + Mem0 managed
- **格式**：自然语言片段，带 user_id 过滤
- **检索**：语义相似度搜索（top_k=500）
- **去重策略**：
  - 相似度 > 0.92 → 跳过
  - 0.78 ~ 0.92 → 更新已有记忆
  - < 0.78 → 新增
- **注入 Prompt**：根据当前对话语义检索 top-N 相关记忆

### 2.2 L2 用户画像（User Profile）

- **存储**：SQLite `user_profiles` 表（`profile_json` 字段，嵌套 JSON）
- **结构**：
  ```json
  {
    "profile": {
      "basic": { "name": {"value": "xxx", "confidence": 0.9, "updated_at": "..."} },
      "career": { "job_title": {...}, "skills": {...} },
      "interests": {...},
      "habits": {...},
      "goals_pains": {...},
      "social": { "relationships": {...} },
      "values_attitudes": {...},
      "interaction_history": {...}
    }
  }
  ```
- **提取**：Celery 任务 `extract_and_update_profile`，LLM（qwen-plus）解析对话
- **冲突处理**：三类字段策略（见下节）
- **审计日志**：`memory_audit_logs` 表，记录每次字段变更

**冲突处理策略**

```python
ACCUMULATIVE_FIELDS = {
    "career.skills", "career.work_pain_points",
    "goals_pains.current_pains", "goals_pains.short_term_goals",
    "goals_pains.long_term_goals", "interests.long_term",
    "interests.short_term", "interaction_history.frequent_topics",
}

OVERRIDE_FIELDS = {
    "basic.name", "basic.age", "basic.location",
    "basic.gender", "basic.birthday",
    "career.job_title", "career.industry",
}
# 其余字段为 merge 策略（LLM 辅助深度合并）
```

### 2.3 L3 事件记忆（Event Memory）

- **存储**：SQLite `user_events` 表 + Qdrant（向量去重）
- **事件类型**：plan / achievement / pain_point / experience / feedback / status_change
- **提取**：Celery 任务 `extract_and_store_events`，LLM 结构化提取
- **去重**：
  1. SQLite 关键词预过滤（正则提取关键词）
  2. Qdrant 向量相似度 > 0.75 → 更新 mention_count，不新建
- **注入 Prompt**：importance ≥ 0.5 或 type='plan'，最多 15 条

### 2.4 L4 社会关系图（Relationship Graph）

- **存储**：嵌套在 L2 画像的 `social.relationships` 字段内（JSON 对象）
- **结构示例**：
  ```json
  {
    "妻子": {"value": "小明", "relation": "配偶"},
    "同事_张三": {"value": "张三", "relation": "同事", "note": "技术 leader"}
  }
  ```
- **前端渲染**：SVG 树状图，以用户为根节点

---

## 3. 数据库设计

### 3.1 SQLite 表结构

**users**
```sql
id          TEXT PK   -- UUID
username    TEXT UNIQUE
password    TEXT       -- bcrypt hashed
created_at  TEXT
```

**user_profiles**
```sql
id          TEXT PK
user_id     TEXT FK → users.id
profile_json TEXT     -- 嵌套画像 JSON
created_at  TEXT
updated_at  TEXT
```

**memory_audit_logs**
```sql
id          INTEGER PK AUTOINCREMENT
user_id     TEXT
field_path  TEXT       -- e.g. "career.skills"
old_value   TEXT
new_value   TEXT
action      TEXT       -- added/appended/replaced/merged/confirmed/replaced_lower_conf
created_at  TEXT
```

**user_events**
```sql
event_id          TEXT PK    -- SHA256 摘要
user_id           TEXT
event_type        TEXT       -- plan/achievement/pain_point/...
summary           TEXT
details_json      TEXT
related_json      TEXT
occurred_at       TEXT       -- YYYY-MM-DD
detected_at       TEXT
last_referenced_at TEXT
importance        REAL
status            TEXT       -- active/superseded/expired/archived
mention_count     INTEGER
created_at        TEXT
updated_at        TEXT
```

**conversations**
```sql
id          TEXT PK    -- UUID
user_id     TEXT
title       TEXT
messages_json TEXT     -- list of {role, content}
created_at  TEXT
updated_at  TEXT
```

---

## 4. API 设计

### 4.1 认证

```
POST /api/auth/register   -- 注册
POST /api/auth/login      -- 登录，返回 JWT token
GET  /api/auth/me         -- 获取当前用户
```

### 4.2 对话

```
POST /api/chat/stream     -- 流式对话（SSE）
  Body: { message: string, history: [{role, content}] }
  Events:
    data: {"type": "text", "content": "..."}
    data: {"type": "prompt", "content": "..."}  // 激活的记忆 prompt
    data: {"type": "done"}
```

### 4.3 记忆管理

```
GET    /api/memories             -- 获取所有情节记忆
POST   /api/memories/search      -- 语义搜索记忆
DELETE /api/memories/{memory_id} -- 删除记忆
POST   /api/memories/import      -- 批量导入 JSON
GET    /api/memories/export      -- 导出所有记忆 JSON
```

### 4.4 用户画像

```
GET    /api/profile              -- 获取用户画像
POST   /api/profile/refresh      -- 手动触发画像刷新
GET    /api/profile/conflict-log -- 自动处理记录（分页）
```

### 4.5 事件记忆

```
GET    /api/events               -- 获取事件列表（支持 type/status 过滤）
DELETE /api/events/{id}          -- 删除事件
PATCH  /api/events/{id}/archive  -- 归档事件
```

### 4.6 历史对话

```
GET    /api/conversations              -- 获取会话列表
GET    /api/conversations/{id}         -- 获取单条会话
DELETE /api/conversations/{id}         -- 删除会话
```

---

## 5. 异步任务设计

### 5.1 Celery 任务

| 任务名 | 触发时机 | 执行内容 |
|--------|---------|---------|
| `extract_and_store_memory` | 对话结束 | 调用 Mem0 存储情节记忆，执行语义去重 |
| `extract_and_update_profile` | 对话结束 | LLM 提取画像字段，自动冲突处理，写审计日志 |
| `extract_and_store_events` | 对话结束 | LLM 提取事件，双层去重，更新 mention_count |
| `decay_all_profiles` | 每天 03:00 | 遍历所有画像字段，对 30 天未更新字段降低置信度 |

### 5.2 任务流程（对话结束后）

```
StreamingResponse 完成
    │
    ├── extract_and_store_memory.delay(user_id, messages)
    ├── extract_and_update_profile.delay(user_id, messages, session_id)
    └── extract_and_store_events.delay(user_id, messages)
```

---

## 6. LLM 使用策略

| 场景 | 模型 | 原因 |
|------|------|------|
| 对话生成 | qwen-max | 语言质量要求高，需要自然表达 |
| 画像提取 | qwen-plus | 结构化 JSON 输出，质量优先（关系/事件抽取易出错） |
| 事件提取 | qwen-plus | 结构化 JSON 输出，质量优先 |
| 对话标题生成 | qwen-plus | 简单摘要任务，统一模型简化运维 |

---

## 7. 前端架构

### 7.1 技术栈

- **框架**：Vue 3 (Composition API) + TypeScript
- **状态管理**：Pinia（4 个 store：chat / memory / profile / events）
- **UI 组件**：Element Plus
- **路由**：Vue Router 4
- **构建**：Vite 5

### 7.2 页面结构

```
/login      -- 登录页
/chat       -- 主聊天页（左侧对话 + 右侧历史抽屉 + 底部 Prompt 抽屉）
/memory     -- 记忆画廊（4 标签页：情节/画像/事件/关系图）
```

### 7.3 Store 职责

| Store | 核心状态 | 核心方法 |
|-------|---------|---------|
| chat | messages, conversations, currentId | sendMessage, loadConversations, newConversation |
| memory | memories, loading | fetchMemories, deleteMemory, importMemories |
| profile | profileData, conflictLog | fetchProfile, fetchConflictLog |
| events | events, loading | fetchEvents, deleteEvent, archiveEvent |

---

## 8. 部署架构

### 8.1 Docker Compose 服务依赖

```
frontend ──→ backend ──→ qdrant
                   └──→ redis ──→ celery
                              └──→ celery-beat
```

### 8.2 数据持久化

| 卷名 | 挂载路径 | 内容 |
|------|---------|------|
| memobot_data | /app/data | SQLite 数据库文件 |
| qdrant_data | /qdrant/storage | 向量数据 |
| redis_data | /data | Redis AOF/RDB |

### 8.3 环境变量

| 变量名 | 默认值 | 说明 |
|--------|-------|------|
| OPENAI_API_KEY | - | DashScope API Key（必填） |
| OPENAI_BASE_URL | dashscope.aliyuncs.com | API 接入点 |
| CHAT_MODEL | qwen-max | 对话模型 |
| EXTRACT_MODEL | qwen-plus | 提取模型 |
| JWT_SECRET | memobot-dev-secret | JWT 签名密钥（生产必须修改） |
| REDIS_URL | redis://redis:6379/0 | Celery Broker |
| QDRANT_HOST | qdrant | Qdrant 服务地址 |
| USER_DB_PATH | /app/data/memobot.db | SQLite 文件路径 |
| DEV_MODE | true | 开发模式（跳过部分校验） |

---

## 9. 关键设计决策

### 9.1 为什么用 SQLite 而非 PostgreSQL？

情节记忆已存 Qdrant，结构化数据量（用户、事件、会话）在百万行以内，SQLite 零运维成本且满足需求。

### 9.2 为什么画像存 JSON 而非关系型字段？

画像结构动态扩展，不同用户字段覆盖率差异大，JSON blob + Python 解析成本最低，查询主要为全量读取。

### 9.3 为什么事件去重用双层？

- SQLite 关键词过滤：O(1) 快速排除明显不相关记录，降低 Qdrant 查询量
- Qdrant 向量相似度：捕获语义相似但表述不同的重复事件（0.75 阈值覆盖度量变体）

### 9.4 为什么画像冲突不需要用户确认？

AI 伴侣场景下用户不应被频繁打扰确认信息。三类字段策略（累加/覆盖/合并）覆盖 95% 以上更新场景，审计日志提供事后透明度。
