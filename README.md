# MindMem

一款具备持久化、多层记忆能力的 AI 智能伴侣。MindMem 能够跨会话地记住你的经历、身份信息、社会关系与重要事件，让每次对话都有"认识你"的感觉。

---

## 功能特性

- **四层记忆**：情节记忆 / 用户画像 / 事件记忆 / 社会关系图
- **智能伴侣**：qwen-max 驱动，温柔知性，对话自然克制
- **记忆画廊**：可视化管理所有记忆层，支持导入/导出
- **自动冲突处理**：画像字段智能合并，附审计日志
- **历史对话**：会话自动保存，支持随时回顾
- **Prompt 透明**：每次回复可查看激活了哪些记忆

---

## 快速开始

### 前置条件

- Docker Desktop（已启动）
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

### 3. 启动服务

```bash
./start-local.sh
```

等待所有容器启动后，访问 [http://localhost:5175](http://localhost:5175)

### 4. 停止服务

```bash
./stop-local.sh
```

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
- FastAPI (Python 3.11, async)
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
L1 情节记忆  →  Mem0 + Qdrant  →  自然语言片段，语义检索
L2 用户画像  →  SQLite JSON    →  结构化属性，置信度打分
L3 事件记忆  →  SQLite + Qdrant →  结构化事件，时间索引
L4 社会关系  →  嵌套在 L2 内   →  人际关系图谱
```

每次对话结束后，Celery 异步任务自动更新四层记忆，不阻塞对话响应。

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
