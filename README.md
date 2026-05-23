# MindMem

一款具备持久化、多层记忆能力的 AI 智能伴侣。MindMem 能够跨会话地记住你的经历、身份信息、社会关系与重要事件，让每次对话都有"认识你"的感觉。

---

## 功能特性

- **四层记忆**：情节记忆 / 用户画像 / 事件记忆 / 社会关系图
- **智能伴侣**：qwen-max 驱动，温柔知性，对话自然克制
- **记忆画廊**：可视化管理所有记忆层，支持导入/导出
- **自动冲突处理**：画像字段智能合并，附审计日志
- **记忆质量治理**：情节记忆去重压缩、画像字段归一化、社会关系清洗
- **历史对话**：会话自动保存，支持随时回顾
- **Prompt 透明**：每次回复可查看激活了哪些记忆

---

## 快速开始

### 前置条件

- Docker / Docker Compose
- 阿里云 DashScope API Key（支持 qwen 系列）

### 1. 获取代码

```bash
git clone <repo-url>
cd mindmem
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```env
OPENAI_API_KEY=sk-xxxxxxxxxxxx     # 必填：DashScope API Key
JWT_SECRET=your-secret-key-here    # 建议修改：JWT 签名密钥
```

### 3. 启动服务（Docker Compose 推荐）

```bash
docker compose up -d --build
```

等待所有容器启动后，访问 [http://localhost:5175](http://localhost:5175)

### 4. 停止服务

```bash
docker compose down
```

> `start-local.sh` / `stop-local.sh` 主要用于 macOS 本地非 Docker 开发；ECS 或 Linux 环境建议使用 Docker Compose。

---

## 项目结构

```
mindmem/
├── backend/
│   ├── app/
│   │   ├── models/          # SQLAlchemy 数据模型
│   │   │   ├── user.py
│   │   │   ├── profile.py   # 用户画像 + 审计日志
│   │   │   ├── event.py     # 事件记忆
│   │   │   └── conversation.py
│   │   ├── routers/         # FastAPI 路由
│   │   │   ├── chat.py      # 流式对话
│   │   │   ├── memory.py    # 情节记忆 CRUD
│   │   │   ├── profile.py   # 画像 + 冲突日志
│   │   │   ├── events.py    # 事件 CRUD
│   │   │   └── conversations.py
│   │   └── services/
│   │       ├── mem0_engine.py      # Mem0 封装
│   │       ├── profile_engine.py   # 画像提取 + 冲突处理
│   │       └── event_engine.py     # 事件提取 + 格式化
│   ├── scripts/              # 记忆评测/修复/清洗工具
│   └── celery_worker.py     # 异步任务（记忆/画像/事件提取）
├── frontend/
│   └── src/
│       ├── views/
│       │   ├── ChatView.vue    # 主聊天页
│       │   └── MemoryView.vue  # 记忆画廊（4 标签页）
│       └── stores/
│           ├── chat.ts
│           ├── memory.ts
│           ├── profile.ts
│           └── events.ts
├── docs/
│   ├── PRD.md    # 产品需求文档
│   └── TDD.md    # 技术设计文档
└── docker-compose.yml
```

---

## 配置说明

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `OPENAI_API_KEY` | - | DashScope API Key（必填） |
| `OPENAI_BASE_URL` | dashscope.aliyuncs.com/... | API 接入点 |
| `CHAT_MODEL` | qwen-max | 对话模型 |
| `EXTRACT_MODEL` | qwen-plus | 记忆提取模型 |
| `JWT_SECRET` | memobot-dev-secret | JWT 密钥（生产环境必须修改） |
| `DEV_MODE` | true | 开发模式 |

---

## 技术栈

**后端**
- FastAPI (Python 3.12, async)
- SQLAlchemy 2 + SQLite
- Mem0 + Qdrant（向量存储）
- Celery 5 + Redis（异步任务）
- OpenAI SDK（DashScope 兼容模式）

**前端**
- Vue 3 (Composition API) + TypeScript
- Pinia 状态管理
- Element Plus UI
- Vite 5

**基础设施**
- Docker Compose（6 个服务）
- Qdrant（向量数据库）
- Redis（消息队列）

---

## 记忆架构

```
L1 情节记忆  →  候选事实提取 + 去重/合并 + Mem0/Qdrant
L2 用户画像  →  SQLite JSON + 字段白名单 + 强类型归一化
L3 事件记忆  →  SQLite + Qdrant →  结构化事件，时间索引
L4 社会关系  →  嵌套在 L2 内，统一 social.relationships，人际关系图谱
```

每次对话结束后，Celery 异步任务自动更新四层记忆，不阻塞对话响应。

### 记忆质量治理

- 情节记忆写入 Mem0 时使用 `infer=false`，避免二次抽取产生元数据噪音
- 相似度 ≥0.82 跳过，0.62-0.82 使用 LLM 合并，<0.62 新增
- 画像字段使用白名单，非法路径自动归一化或丢弃
- `social.family_structure` 已废弃，所有人际关系统一进入 `social.relationships`
- `basic.age` / `basic.birthday` 写入前做强类型校验，例如 `"83年"` 会迁移为 `birthday="1983"`
- 社会关系中 `via` 必须指向已存在的人物 key，否则自动降级为直系关系

### 运维脚本

```bash
# 一键评测四层记忆
docker compose exec backend python3 scripts/eval_memory.py

# 修复历史脏画像
docker compose exec backend python3 scripts/fix_dirty_profile.py

# 压缩冗余情节记忆（默认 dry-run，加 --apply 才写库）
docker compose exec backend python3 scripts/compact_memories.py <phone>
docker compose exec backend python3 scripts/compact_memories.py <phone> --apply

# 清洗社会关系（默认 qwen-max，支持 --model）
docker compose exec backend python3 scripts/clean_relationships.py <phone>
docker compose exec backend python3 scripts/clean_relationships.py <phone> --apply
```

---

## API 文档

启动后访问 [http://localhost:8000/docs](http://localhost:8000/docs) 查看完整 Swagger 文档。

---

## 开发指南

### 本地开发（不用 Docker）

```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Celery Worker（另开终端）
celery -A celery_worker worker --loglevel=info

# 前端（另开终端）
cd frontend
npm install
npm run dev
```

### 数据库迁移

数据库迁移在应用启动时自动执行（`app/db.py` 中的 `init_db()`）。新字段通过 `try-except` 容错，兼容已有数据。

---

## Roadmap

**P1 近期计划**
- [ ] 记忆语义搜索（全局搜索框）
- [ ] 记忆主动遗忘命令（"忘掉关于 XX 的记忆"）
- [ ] 置信度衰减可视化

**P2 中期计划**
- [ ] 多模态输入（语音、图片）
- [ ] 记忆分享与导出（Markdown 格式）
- [ ] 自定义 AI 人设

---

## License

MIT
