# MindMem 1.0 技术设计文档

> 版本：1.2 | 日期：2026-06-06
>
> v1.2 增量：
> - 新增 `memory_deprecations` 表 + `correction_engine` 模块 → §3.1 / §11
> - 新增 `chat_audit_v1` 审计文件 + Real-Chat-Eval pipeline → §12
> - Prompt Composer 全量瘦身（平均 -45% tokens）→ §6.4
> - LLM 全链路升级至 Qwen3.7（`qwen3.7-max` 聊天 / `qwen3.7-plus` 后台）+ `enable_thinking=false` → §7 / §9.3
> - Memory Router query 收窄为只取最近 1 句 user 消息（避免历史稀释 Mem0 检索）→ §6.2

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
| backend | FastAPI (Python 3.12) | 8000 | API 服务 |
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
  - 相似度 ≥0.82 → 跳过
  - 0.62 ~ 0.82 → LLM 合并已有记忆
  - <0.62 → 新增
- **写入策略**：
  - `extract_and_store_memory` 先调用 `qwen-plus` 提取候选事实
  - 候选事实写入 Mem0 时使用 `infer=False`，避免 Mem0 再次抽取/改写
  - LLM 合并使用 `_llm_merge()`，输出一句干净事实，不做字符串拼接
  - 写入前通过 `_strip_subject_prefix()` 去掉"用户dxj"/"dxj的"/"他"等主语前缀
  - 候选提取失败时跳过写入，不降级为 Mem0 直接抽取
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
- **审计日志**：`memory_audit_log` 表，记录每次字段变更
- **路径防御**：
  - `_ALLOWED_FIELD_PATHS` 白名单限制允许写入的叶子字段
  - `_coerce_fact_path()` 规整 LLM 偶发非法路径，如 `social.relationships.妻子`
  - `social.family_structure` 已废弃，写入时自动迁移到 `social.relationships`
- **强类型与自愈**：
  - `basic.age` 只允许整数年龄
  - `"83年"`、`83`、`1983` 等出生年份表达归一化到 `basic.birthday`
  - `_heal_profile()` 修复历史脏数据，包括 age/birthday、relationships 外层污染和 family_structure 残留

**冲突处理策略**

```python
ACCUMULATIVE_FIELDS = {
    "career.skills", "career.work_pain_points",
    "goals_pains.current_pains", "goals_pains.short_term_goals",
    "goals_pains.long_term_goals", "interests.long_term",
    "interests.short_term", "interaction_history.frequent_topics",
}

OVERRIDE_FIELDS = {
    "basic.name", "basic.birthday", "basic.location",
    "basic.gender",
    "career.job_title", "career.industry",
}
# 其余字段为 merge 策略（LLM 辅助深度合并）
```

**关键归一化函数**

| 函数 | 作用 |
|------|------|
| `_coerce_fact_path` | 将非法 dimension_path 规整到合法叶子字段，或丢弃 |
| `_normalize_relationships` | 去掉无效 key，修复 `via=用户/我/本人`，校验 via 是否指向真实节点 |
| `_smart_merge_relationships` | 合并社会关系时保留更详细描述，避免短文本覆盖长文本 |
| `_heal_profile` | 修复历史脏画像，供提取流程和运维脚本复用 |

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
    "妻子": "配偶，和我一样大，全职在家带小孩",
    "儿子": "9岁半",
    "小孙孙": {"rel": "儿子的同学，爱听相声", "via": "儿子"},
    "邻居老爷爷": "邻居，80岁，养狗叫可乐"
  }
  ```
- **前端渲染**：SVG 树状图，以用户为根节点
- **关系规则**：
  - 直系关系：value 为字符串
  - 间接关系：value 为 `{ "rel": "...", "via": "中间人名" }`
  - `via` 必须指向 relationships 中另一个真实存在的 key
  - `via` 指向用户本人或不存在节点时，后端归一化为直系关系
  - 前端兼容对象、数组和 JSON 字符串等历史格式

---

## 3. 数据库设计

### 3.1 SQLite 表结构

**users**
```sql
id          TEXT PK   -- UUID
phone       TEXT UNIQUE INDEX
nickname    TEXT
created_at  DATETIME
```

**user_profiles**
```sql
user_id      TEXT PK
profile_json TEXT     -- 嵌套画像 JSON
last_updated DATETIME
```

**memory_audit_log**
```sql
id          INTEGER PK AUTOINCREMENT
user_id     TEXT
dimension_path TEXT    -- e.g. "career.skills"
old_value   TEXT
new_value   TEXT
action      TEXT       -- added/appended/replaced/merged/confirmed/replaced_lower_conf
session_id  TEXT
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

**memory_deprecations** *(v1.2 新增)*
```sql
id                          INTEGER PK AUTOINCREMENT
user_id                     TEXT INDEX
source                      TEXT       -- episodic / event / profile / entity
ref_id                      TEXT INDEX -- mem0_id / event_id / dimension_path / entity_text
original_text               TEXT
reason                      TEXT
correction_conversation_id  TEXT
correction_turn_id          TEXT
llm_confidence              REAL
action                      TEXT       -- deprecate / update / audit_only
new_text                    TEXT       -- 仅 action=update 时填
deprecated_at               DATETIME
restored_at                 DATETIME   -- 软撤销时填
```

设计要点：
- `(user_id, source, ref_id, restored_at IS NULL)` 用作"当前生效的 deprecation"语义查询索引；ORM 层不强制唯一约束，避免重复纠错时报错
- `source='entity'` 是"被否定的实体词"（用户说"小鹏不是动物"→ banned\_entity=`小鹏`），写入/激活时硬过滤
- 仅由 `correction_engine.run_cleanup_for_correction()` 写入，主链路代码只读

---

## 4. API 设计

### 4.1 认证

```
POST /api/auth/phone/send-code -- 发送手机号验证码（DEV_MODE 下返回 dev_code）
POST /api/auth/phone/login     -- 手机号 + 验证码登录，返回 JWT token
GET  /api/auth/me              -- 获取当前用户
```

### 4.2 对话

```
POST /api/chat/stream     -- 流式对话（SSE）
  Body: { message: string, history: [{role, content}] }
  Events:
    data: {"type": "prompt", "memories": [...], "system": "..."}  // 激活的记忆 prompt
    data: {"type": "content", "content": "..."}
    data: [DONE]
```

### 4.3 记忆管理

```
GET    /api/memory             -- 获取所有情节记忆
DELETE /api/memory/{memory_id} -- 删除记忆
POST   /api/memory/import      -- 批量导入文本记忆
```

### 4.4 用户画像

```
GET    /api/profile              -- 获取用户画像
POST   /api/profile/field        -- 手动设置字段（运维/调试）
DELETE /api/profile/field        -- 删除字段
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
GET    /api/conversations/{id}/audit   -- 导出 chat_audit_v1 JSON (v1.2)
```

### 4.7 评测实验室 *(v1.2)*

```
GET    /api/eval/personas              -- 列出可用 persona（合成评测）
POST   /api/eval/run                   -- 跑合成评测（smoke/full）
GET    /api/eval/runs                  -- 获取历史评测报告列表
GET    /api/eval/runs/{run_id}         -- 获取单次评测报告

POST   /api/eval/chat-review/{conv_id} -- 对单个真实会话跑 L0+L1 评估
GET    /api/eval/chat-review/{conv_id} -- 取已跑过的评估结果（可缓存）
```

---

## 5. 异步任务设计

### 5.1 Celery 任务

| 任务名 | 触发时机 | 执行内容 |
|--------|---------|---------|
| `extract_and_store_memory` | 对话结束（非 correction intent） | 调用 Mem0 存储情节记忆，执行语义去重；写入前 `banned_entities` 硬过滤 |
| `extract_and_update_profile` | 对话结束（非 correction intent） | LLM 提取画像字段，自动冲突处理，写审计日志；写入前 `banned_entities` 硬过滤 |
| `extract_and_store_events` | 对话结束（非 correction intent） | LLM 提取事件，双层去重，更新 mention_count；写入前 `banned_entities` 硬过滤 |
| **`run_correction_cleanup_task`** *(v1.2 新增)* | 对话结束（intent == correction） | LLM 判断 episodic / event / profile 候选 → 软删除/更新 + 写入 banned entities |
| `decay_all_profiles` | 每天 03:00 | 遍历所有画像字段，对 30 天未更新字段降低置信度 |

**关键约束**：当本轮 intent == `correction` 时，**跳过 `extract_*`，只跑 `run_correction_cleanup_task`**。  
原因：用户在纠错时，assistant 的回复里仍可能复述错误事实的相关词。如果照常跑 extract，会把"被纠正的错误"当成"新事实"再学一遍，造成记忆永久污染。

### 5.2 任务流程（对话结束后）

```
StreamingResponse 完成
    │
    ├── if intent == "correction":
    │       └── run_correction_cleanup_task.delay(user_id, user_msg, assistant_msg,
    │                                              conv_id, turn_id)
    │
    └── else:
            ├── extract_and_store_memory.delay(user_id, messages)
            ├── extract_and_update_profile.delay(user_id, messages, session_id)
            └── extract_and_store_events.delay(user_id, messages)
```

详见 §11 在线记忆纠错管线。

---

### 5.3 记忆质量治理脚本

| 脚本 | 默认模型 | 写库方式 | 用途 |
|------|----------|----------|------|
| `scripts/eval_memory.py` | - | 只读 | 读取 SQLite + Mem0/Qdrant，输出四层记忆质量报告 |
| `scripts/fix_dirty_profile.py` | - | 直接写 SQLite | 修复历史画像脏数据（age/birthday、family_structure、relationships 错位） |
| `scripts/compact_memories.py` | qwen-max | dry-run，`--apply` 后写入 Mem0 | 压缩情节记忆，删除重复和元数据噪音 |
| `scripts/clean_relationships.py` | qwen-max | dry-run，`--apply` 后写入 SQLite | 语义清洗社会关系，合并重复 key，补回明确细节 |

脚本均以 dry-run 优先，关键清洗任务默认使用 `qwen-max`，降低 LLM 推断和编造概率；高频在线提取仍使用 `qwen-plus` 控制成本。

---

## 6. Memory Use v1 设计

### 6.1 MemoBot 记忆人格

v1 引入三类记忆人格，用于控制记忆表达边界、事件主动跟进程度和追问强度。人格不改变事实正确性和敏感信息硬边界。

```python
class MemoryPersonality(str, Enum):
    INTROVERT = "introvert"
    BALANCED = "balanced"
    EXTROVERT = "extrovert"
```

**配置草案**

```python
PERSONALITY_CONFIG = {
    "introvert": {
        "label": "内向型",
        "max_explicit_memories": 1,
        "allow_casual_memory": False,
        "plan_followup": "asked_only",
        "pain_point_policy": "background_only",
        "question_style": "low",
    },
    "balanced": {
        "label": "中性型",
        "max_explicit_memories": 2,
        "allow_casual_memory": False,
        "plan_followup": "once",
        "pain_point_policy": "triggered_only",
        "question_style": "medium",
    },
    "extrovert": {
        "label": "外向型",
        "max_explicit_memories": 3,
        "allow_casual_memory": True,
        "plan_followup": "active_once",
        "pain_point_policy": "soft_triggered",
        "question_style": "high",
    },
}
```

**持久化方案**
- v1 可先保存到用户画像：`interaction_history.memory_personality`
- 后续若设置项增多，可迁移到独立 `user_settings` 表
- 默认值：`balanced`
- 切换人格只影响之后的对话，不修改历史记忆

### 6.2 记忆路由器

v1 采用规则优先的 Memory Router，不引入独立 LLM Router。它位于 `chat.py` 和各记忆服务之间，负责生成 `MemoryRoute` 和 `MemoryContext`。

> **v1.5 计划**：见 [Memory-Router-v1.5.md](./Memory-Router-v1.5.md)。架构拆为三层：Layer 1 硬规则 → Layer 2 小模型 intent 分类 → Layer 3 策略查表（本节下表不变）。

**输入**

```python
class MemoryRouteInput(BaseModel):
    user_id: str
    message: str
    recent_history: list[ChatMessage]  # 最近 3-5 轮
    personality: MemoryPersonality
    profile_summary: str | None = None
    relationship_keys: list[str] = []
```

**输出**

```python
class MemoryUsage(str, Enum):
    EXPLICIT_OK = "explicit_ok"
    BACKGROUND_ONLY = "background_only"
    FOLLOW_UP_ONCE = "follow_up_once"
    AVOID_UNLESS_ASKED = "avoid_unless_asked"


class MemoryRoute(BaseModel):
    intent: str
    memory_depth: Literal["minimal", "focused", "broad", "safe_focused", "event_focused"]
    load_layers: list[str]
    query: str
    sensitive_mode: bool = False
    max_explicit_memories: int
```

**上下文结构**

```python
class RoutedMemory(BaseModel):
    source: Literal["profile", "relationship", "event", "episodic"]
    text: str
    usage: MemoryUsage
    reason: str
    score: float = 0.0


class MemoryContext(BaseModel):
    route: MemoryRoute
    stable_profile: list[RoutedMemory]
    relevant_relationships: list[RoutedMemory]
    relevant_events: list[RoutedMemory]
    relevant_memories: list[RoutedMemory]
    background_only: list[RoutedMemory]
    do_not_mention: list[RoutedMemory]
```

**意图路由规则**

| intent | 触发信号 | load_layers | memory_depth | 默认 usage |
|--------|----------|-------------|--------------|-------------|
| `casual` | 问候、闲聊开场 | profile_basic | minimal | background_only |
| `self_summary` | "你了解我吗"、"你记得什么" | profile, relationships, events, episodic | broad | explicit_ok |
| `relationship_topic` | 人名、亲属称谓、代词 | relationships, episodic, events | focused | explicit_ok / background_only |
| `emotional_support` | 烦、累、压力、焦虑等 | profile_basic, episodic, events | safe_focused | background_only |
| `plan_followup` | 明天、下周、准备、计划 | events, episodic | event_focused | follow_up_once |
| `preference_request` | 推荐、适合、怎么学等 | profile, episodic | focused | explicit_ok |
| `correction` | 不是、记错、忘掉 | profile, episodic | focused | avoid_unless_asked |
| `knowledge_task` | 技术/工具/通用知识问题 | profile_basic | minimal | background_only |

**检索 query 生成（v1.2 调整）**
- **历史窗口收窄为最近 1 句 user 消息**：之前用 3-5 轮 history 拼 query，会把"养了一只猫"这种短而精的关键词稀释到 Mem0 召不回（语义被旁支带偏）
- 当前 query = `user 当前消息 + 最近 1 条 user 历史（如有）+ 识别出的人物/主题`
- 例：用户说"她最近还是很累"，最近上下文提到"老婆"，query 仅扩展为"妻子 很累"，**不再附带** "全职带孩子、工作忙、家庭分担" 这类推断词（推断由 Chat 模型在 prompt 里完成，不在检索阶段做）

**规则优先的 intent 打分**

v1 不调用独立 LLM Router，而是使用规则打分 + 优先级覆盖。每个 intent 初始分数为 0，命中特征后加分，最终取最高优先级的高分 intent。

```python
INTENT_PRIORITIES = [
    "correction",
    "self_summary",
    "knowledge_task",
    "emotional_support",
    "relationship_topic",
    "plan_followup",
    "preference_request",
    "casual",
]

def score_intents(message: str, context_text: str, relationship_keys: list[str]) -> dict[str, int]:
    scores = defaultdict(int)

    if contains_any(message, ["你了解我", "你记得我", "我身边", "总结一下我"]):
        scores["self_summary"] += 5

    if contains_any(message, relationship_keys):
        scores["relationship_topic"] += 4

    if contains_any(message, ["她", "他", "孩子", "老婆", "妻子", "儿子", "邻居"]):
        scores["relationship_topic"] += 3

    if contains_any(message, ["烦", "累", "压力", "焦虑", "撑不住", "难受"]):
        scores["emotional_support"] += 4

    if contains_any(message, ["明天", "下周", "计划", "准备", "打算", "要去", "开始"]):
        scores["plan_followup"] += 3

    if contains_any(message, ["推荐", "适合", "怎么学", "怎么安排"]):
        scores["preference_request"] += 3

    if contains_any(message, ["你记错", "不是", "忘掉", "删除", "不对"]):
        scores["correction"] += 10

    # 上下文补偿：当前句子很短，但最近上下文能指向人物或主题
    if contains_any(message, ["她", "他", "这事", "那个"]) and contains_any(context_text, ["老婆", "妻子", "儿子"]):
        scores["relationship_topic"] += 5

    return scores
```

**默认保守策略**
- 若所有 intent 分数都低于阈值，落到 `casual`
- 若命中多个 intent，按 `INTENT_PRIORITIES` 决定
- `correction` 永远最高优先级，避免系统在用户纠错时继续使用错误记忆
- `knowledge_task` 命中时默认不加载隐私记忆，只保留交互风格

**记忆层加载映射**

| layer | 说明 | 使用场景 |
|-------|------|----------|
| `profile_basic` | 姓名、城市、职业、称呼/交流偏好 | casual、knowledge_task |
| `profile` | 完整结构化画像 | self_summary、preference_request |
| `profile_style` | 只影响表达方式的偏好 | knowledge_task |
| `relationships` | `social.relationships` | relationship_topic、self_summary |
| `events` | 事件记忆候选池 | plan_followup、emotional_support、self_summary |
| `episodic` | Mem0 情节记忆检索 | focused/broad 类场景 |

### 6.3 MemoryContextBuilder

`MemoryContextBuilder` 根据 `MemoryRoute` 拉取各层记忆，并为每条记忆计算分数和使用方式。Router 只做决策，不直接访问数据库和向量库。

**处理流程**

```text
MemoryRoute
  → load stable profile
  → load relationships
  → filter events by event_policy
  → search episodic memories by route.query
  → score and rank
  → assign MemoryUsage
  → truncate by personality limits
  → return MemoryContext
```

**排序公式**

```text
final_score =
  semantic_score * 0.45
+ recency_score * 0.20
+ importance_score * 0.20
+ intent_match_score * 0.15
```

字段来源：
- `semantic_score`：Mem0/Qdrant search score；画像/关系可由规则赋值
- `recency_score`：越近越高，超过 30 天显著降权
- `importance_score`：事件 `importance`、画像置信度、关系字段置信度
- `intent_match_score`：记忆类型与 route.intent 的匹配度

**usage 标注规则**

| 条件 | usage |
|------|-------|
| 用户明确问"你记得什么/你了解我吗" | `explicit_ok` |
| 当前话题直接涉及某人/某事且非敏感 | `explicit_ok` |
| `pain_point`、压力、家庭负担、负面情绪 | `background_only` |
| `plan` 在跟进窗口内且未跟进 | `follow_up_once` |
| archived / superseded / 低置信度 / 用户纠正为错误 | `avoid_unless_asked` 或不注入 |

**截断规则**
- `explicit_ok` 数量受人格 `max_explicit_memories` 限制
- `background_only` 最多 3 条
- `follow_up_once` 最多 1 条
- `events` 每轮最多注入 3 条
- `episodic` 每轮最多注入 5 条候选，最终可显性使用数量仍受人格限制

### 6.4 Prompt 编排（v1.2 瘦身版）

Memory Router 输出后，Prompt 不再简单拼接完整画像/事件/记忆，而是按使用策略分区。**v1.2 全量瘦身**，平均 prompt 长度从 ~2200 字 / ~3300 tokens 降至 ~1000 字 / ~1500 tokens（**节省 ~45%**）。

**分区结构（按 intent 动态渲染，空块直接省略）**

```text
{BASE_PERSONA}          # ~200 字，人设 + 6 类禁用句式

【当前时间】2026-06-06 22:54 · 晚上 · 周六

【你已知关于用户的事】（仅供理解，不主动复述）        # stable_profile，空则省
- ...

【本轮可以提及的具体记忆】                              # explicit_pool，空则省
- ...

【可轻问一次的近期事件】（不要列清单）                   # follow_up_once，空则省
- ...

【你大致还记得这些（默认不主动说出）】                   # background_only，空则省
- ...

【本轮】                                                # 只剩对 LLM 真正有用的最小集
- 意图：{intent_label}；人格：{persona_tone}
- 显性记忆最多 {max_explicit_memories} 条
- [可选] 敏感场景：承接当下感受，不主动翻旧账

{INTENT_GUIDES[intent]}  # 一次只注入一条；按 intent 选

≪硬边界（不可破）≫
- 不编造任何记忆里没有的事实；不替用户/关系人推断心理状态
- 不主动暴露健康/财务/家庭矛盾等敏感信息；不连续提同一痛点
- 敏感场景（情绪/纠错/质问）下：人格"主动性"被覆盖
```

**瘦身的关键决策**

| 决策 | 原因 |
|---|---|
| 整段删除 `BACKGROUND_USAGE_RULES`（350 字） | 其规则已分散到 BASE_PERSONA "不主动复述" + memory_challenge 指引 + HARD_RULES 三处，删除避免重复 |
| `INTENT_GUIDES[correction]` 700 → 350 字 | 合并"硬性输出结构 / 强禁止 / 后续处理"三段，去掉重复 ✅/❌ 示例 |
| 空块（stable/explicit/followup/background）不再渲染"（无）" | 减少噪音，让 LLM 把注意力放在有内容的块上 |
| 本轮规则去掉 `intent_id`/`memory_depth`/`personality_label`/`event_policy` | 这些只对调试有用，对生成无意义；仍完整保留在 `prompt_meta.route` 供前端展示 |

**特定 intent 关键约束（一字不丢）**

| intent | 不可缺失的约束 |
|---|---|
| `correction` | 第一人称承认 + **复述用户给出的正确事实** + 整段禁止再次提及被纠正实体词 + 后续自然停下 |
| `memory_challenge` | "池里没有的实体绝不能说"、"只有 1 条相关时只复述这 1 条" |
| `emotional_support` | 一句话承接具体画面、禁通用建议（深呼吸/早点睡） |
| `casual` | 不主动提具体旧记忆 |
| `knowledge_task` | 直接答、不带个人记忆 |

**SSE prompt 事件结构**

SSE `type=prompt` 中除原始 system prompt 外，提供完整 `prompt_meta`：

```json
{
  "type": "prompt",
  "version": "turn_meta_v1",
  "system": "<system prompt 字符串>",
  "memories": ["兼容旧前端的扁平 memory 列表"],
  "route": {
    "intent": "relationship_topic",
    "intent_confidence": 0.86,
    "intent_source": "classifier",
    "low_confidence": false,
    "memory_depth": "focused",
    "personality": "balanced",
    "sensitive_mode": false,
    "max_explicit_memories": 2,
    "query": "妻子 很累",
    "reasons": ["classifier: 代词指向妻子 (conf=0.86)"],
    "router_version": "v1.5"
  },
  "activated": [
    {"source": "relationship", "text": "妻子全职带孩子", "usage": "explicit_ok",
     "reason": "用户用代词'她'延续妻子话题", "score": 0.84}
  ],
  "context_layers": {
    "stable_profile": [...],
    "relevant_relationships": [...],
    "relevant_events": [...],
    "relevant_memories": [...],
    "background_only": [...]
  },
  "snapshot_stats": {
    "episodic_total": 12,
    "episodic_filtered_deprecated": 1,
    "episodic_filtered_banned": 0,
    "events_filtered_banned": 0,
    "banned_entities": []
  }
}
```

`snapshot_stats` 是真实聊天评估的核心字段，详见 §12。

### 6.5 事件记忆使用策略

事件记忆按类型决定默认 usage、主动跟进窗口和注入优先级。

| event_type | 默认 usage | 主动跟进 | 时间窗口 | 说明 |
|------------|------------|----------|----------|------|
| `plan` | follow_up_once | 是 | 明确日期：前 1 天到后 3 天；无日期：创建后 3-7 天 | 同一事件最多主动一次 |
| `pain_point` | background_only | 否 | 仅相关话题触发 | 不主动戳痛点 |
| `status_change` | explicit_ok/background_only | 可一次 | 创建后 7 天内 | 30 天后可转稳定背景候选 |
| `achievement` | explicit_ok | 不反复 | 相关话题触发 | 可祝贺或延续 |
| `experience` | background_only | 否 | 相关话题触发 | 普通经历过期快 |
| `feedback` | background_only | 否 | 立即生效 | 主要影响回复风格 |

**状态草案**

| status | 含义 | 使用方式 |
|--------|------|----------|
| `active` | 仍可参与对话 | 正常参与路由 |
| `followed_up` | 已主动跟进过 | 不再主动提，相关时可用 |
| `resolved` | 用户明确完成/结束 | 作为历史，不主动跟进 |
| `archived` | 用户/系统归档 | 不使用，除非用户主动问 |
| `superseded` | 被新事件替代 | 不使用旧事件 |

v1 可先通过 `status` 和 `details_json` 记录跟进状态，后续再做字段迁移。

**事件筛选流程**

```text
route.intent
  → 选择候选事件类型
  → 过滤 archived/superseded
  → 按时间窗口过滤
  → 按相关性、importance、人格主动性排序
  → 最多注入 3 条
  → 为每条事件标注 usage
```

### 6.6 模块划分与接入点

建议新增服务模块：

```text
backend/app/services/
  memory_router.py      # intent 识别、人格配置、route 生成
  memory_context.py     # 拉取/筛选/排序/usage 标注
  prompt_composer.py    # 分区组装 system prompt + prompt_meta
```

`chat.py` 接入方式：

```python
# 当前
profile_text = await _load_profile_text(user.id, db)
events_text = await _load_events_text(user.id, db)
memory_list = _search_memories_raw(user.id, message)
system_prompt = _build_system_prompt(memory_text, profile_text, events_text)

# v1
route = memory_router.route(
    user_id=user.id,
    message=message,
    recent_history=history[-10:],
    personality=personality,
)
context = await memory_context_builder.build(route, db)
system_prompt, prompt_meta = prompt_composer.compose(context)
```

**Prompt 透明面板**：SSE `type=prompt` 的完整结构见 §6.4 末尾。

### 6.7 v1 非目标

为控制复杂度，v1 暂不做：
- 独立 LLM Router（**v1.5 见 [Memory-Router-v1.5.md](./Memory-Router-v1.5.md)**：仅 intent 分类，非完整 Route 生成）
- 主动推送/定时提醒
- 完整事件状态机迁移
- 每条记忆的用户反馈按钮
- 独立 `user_settings` 表（先复用画像 JSON）
- 多人格独立长期 prompt 训练

---

## 7. LLM 使用策略

### 7.1 模型分配（v1.2 升级到 Qwen3.7）

| 场景 | 模型 | enable_thinking | 原因 |
|------|------|---|------|
| 对话生成 | `qwen3.7-max` | **false** | 流式聊天延迟敏感，thinking 模式会让首字 token 延迟到 1.5s+ |
| 记忆 intent 分类 | `qwen3.7-plus` | **false** | 仅输出 JSON，temperature=0，与 Chat 隔离 |
| 画像提取 | `qwen3.7-plus` | **false** | 结构化 JSON 输出 |
| 事件提取 | `qwen3.7-plus` | **false** | 结构化 JSON 输出 |
| 情节记忆候选提取 | `qwen3.7-plus` | **false** | 高频任务 |
| 情节记忆合并 | `qwen3.7-plus` | **false** | 高频但输入短 |
| **纠错判断（v1.2 新增）** | `qwen3.7-plus` | **false** | 小模型 JSON 决策候选记忆的 action |
| 手动记忆压缩 | `qwen3.7-max` | **false** | 低频关键清洗 |
| 手动社会关系清洗 | `qwen3.7-max` | **false** | 低频关键清洗 |
| 评测 Judge | `qwen3.7-plus` | **false** | L1 启发式为主，LLM Judge 暂未启用 |
| 对话标题生成 | 规则截取首条用户消息 | — | 未单独调用 LLM |

### 7.2 enable_thinking 关键说明

Qwen3.7 系列默认是 **hybrid thinking model**，开启 thinking 会产出 `<think>...</think>` 段且首字延迟显著。MindMem 所有 LLM 调用统一显式传 `extra_body={"enable_thinking": False}`：

```python
client.chat.completions.create(
    model=os.getenv("EXTRACT_MODEL", "qwen3.7-plus"),
    messages=[...],
    extra_body={"enable_thinking": False},  # ← 必填
)
```

涉及文件：`chat.py`、`intent_classifier.py`、`celery_worker.py`、`correction_engine.py`、`profile_engine.py`、`event_engine.py`、`eval_runner.py`、`compact_memories.py`、`clean_relationships.py`。

环境变量级别也提供 `ENABLE_THINKING=false`，便于运维一键切换（默认 false）。

---

## 8. 前端架构

### 8.1 技术栈

- **框架**：Vue 3 (Composition API) + TypeScript
- **状态管理**：Pinia（4 个 store：chat / memory / profile / events）
- **UI 组件**：Element Plus
- **路由**：Vue Router 4
- **构建**：Vite 5

### 8.2 页面结构

```
/login      -- 登录页
/chat       -- 主聊天页（左侧对话 + 右侧历史抽屉 + 底部 Prompt 抽屉）
/memory     -- 记忆画廊（4 标签页：情节/画像/事件/关系图）
```

### 8.3 Store 职责

| Store | 核心状态 | 核心方法 |
|-------|---------|---------|
| chat | messages, conversations, currentId | sendMessage, loadConversations, newConversation |
| memory | memories, loading | fetchMemories, deleteMemory, importMemories |
| profile | profileData, conflictLog | fetchProfile, fetchConflictLog |
| events | events, loading | fetchEvents, deleteEvent, archiveEvent |

---

## 9. 部署架构

### 9.1 Docker Compose 服务依赖

```
frontend ──→ backend ──→ qdrant
                   └──→ redis ──→ celery
                              └──→ celery-beat
```

### 9.2 数据持久化

| 卷名 | 挂载路径 | 内容 |
|------|---------|------|
| memobot_data | /app/data | SQLite 数据库文件 |
| qdrant_data | /qdrant/storage | 向量数据 |
| redis_data | /data | Redis AOF/RDB |

### 9.3 环境变量

| 变量名 | 默认值 | 说明 |
|--------|-------|------|
| OPENAI_API_KEY | - | DashScope API Key（必填） |
| OPENAI_BASE_URL | dashscope.aliyuncs.com | API 接入点 |
| **CHAT_MODEL** | `qwen3.7-max` | 对话模型（v1.2 升级） |
| **INTENT_MODEL** | `qwen3.7-plus` | 意图分类模型（v1.2 升级） |
| **EXTRACT_MODEL** | `qwen3.7-plus` | 画像/事件/记忆提取模型（v1.2 升级） |
| **CORRECTION_MODEL** | `qwen3.7-plus` | 纠错判断模型（v1.2 新增） |
| **ENABLE_THINKING** | `false` | 全局关闭 Qwen3.7 thinking 模式，避免首字延迟 |
| **PROMPT_TIMEZONE** | `Asia/Shanghai` | Prompt 中"当前时间"的时区 |
| JWT_SECRET | memobot-dev-secret | JWT 签名密钥（生产必须修改） |
| REDIS_URL | redis://redis:6379/0 | Celery Broker |
| QDRANT_HOST | qdrant | Qdrant 服务地址 |
| USER_DB_PATH | /app/data/memobot.db | SQLite 文件路径 |
| DEV_MODE | true | 开发模式（跳过部分校验） |

---

## 10. 关键设计决策

### 10.1 为什么用 SQLite 而非 PostgreSQL？

情节记忆已存 Qdrant，结构化数据量（用户、事件、会话）在百万行以内，SQLite 零运维成本且满足需求。

### 10.2 为什么画像存 JSON 而非关系型字段？

画像结构动态扩展，不同用户字段覆盖率差异大，JSON blob + Python 解析成本最低，查询主要为全量读取。

### 10.3 为什么事件去重用双层？

- SQLite 关键词过滤：O(1) 快速排除明显不相关记录，降低 Qdrant 查询量
- Qdrant 向量相似度：捕获语义相似但表述不同的重复事件（0.75 阈值覆盖度量变体）

### 10.4 为什么画像冲突不需要用户确认？

AI 伴侣场景下用户不应被频繁打扰确认信息。三类字段策略（累加/覆盖/合并）覆盖 95% 以上更新场景，审计日志提供事后透明度。

### 10.5 为什么纠错走"软删除 + Tombstone"而不是物理删除？

- **可审计**：用户事后想找"为什么这条记忆被删了"，可在 `memory_deprecations` 找到原文 + 原因 + 触发会话
- **可回滚**：`restored_at` 字段支持"撤销纠错"
- **避免误删**：LLM 判断有 confidence，低于阈值的只 `audit_only`，不动数据
- **跨层一致**：episodic 在 Mem0 没有简单的"标 tombstone"接口，统一在 SQLite 集中管理 deprecation 列表，主链路只读

### 10.6 为什么 Qwen3.7 需要显式 `enable_thinking=false`？

Qwen3.7 系列默认开启 thinking 模式，会先输出一段 `<think>…</think>` 推理过程再给最终答案。在流式聊天场景下首字延迟会从 ~500ms 涨到 1.5-3s，严重影响体感；后台抽取任务也会让 JSON 解析失败率升高。统一显式关掉。

---

## 11. 在线记忆纠错管线 *(v1.2)*

详见 [Correction-Pipeline.md](./Correction-Pipeline.md)。本节给出关键集成点。

### 11.1 流程图

```text
用户："你记错了，小鹏不是动物，是儿子的同学"
        │
        ▼ (chat.py)
Layer 2 分类器 → intent=correction (confidence=0.94)
        │
        ▼
prompt_composer 注入【纠错指引】(强制：第一人称承认 + 复述用户事实)
        │
        ▼
Chat 模型生成回复："记错了，小鹏是儿子的同学，不是宠物，我按这个来。"
        │
        ▼ (chat.py 流式结束)
if intent == "correction":
    run_correction_cleanup_task.delay(user_id, user_msg, asst_msg, conv_id, turn_id)
    # ↑ 关键：跳过 extract_*，避免再学错
        │
        ▼ (celery_worker)
correction_engine.run_cleanup_for_correction():
  ├── 提取纠错目标 token（用户/asst 文本去停用词）
  ├── 多 token 检索 episodic / event / profile 候选
  ├── LLM 判断 → {actions: [...], banned_entities: [...]}
  ├── 写入 memory_deprecations 表（episodic/event/profile/entity 四种 source）
  └── profile 同步追加 interaction_history.user_corrections
        │
        ▼ (下次对话)
memory_context.build():
  ├── 读 deprecated_episodic_ids → 过滤 Mem0 召回结果
  ├── 读 banned_entities → 过滤含 banned 实体的 episodic / event
  └── snapshot_stats 记录"过滤了多少"
```

### 11.2 涉及的代码模块

| 文件 | 职责 |
|---|---|
| `backend/app/models/deprecation.py` | `MemoryDeprecation` ORM |
| `backend/app/services/correction_engine.py` | 候选检索 + LLM 判断 + 三层 apply + banned 写入 |
| `backend/celery_worker.py` | `run_correction_cleanup_task` + extract\_\* 加 banned 过滤 |
| `backend/app/routers/chat.py` | `intent == correction` 时跳过 extract\_\* |
| `backend/app/services/memory_context.py` | 读 deprecated/banned 做硬过滤 + 写 snapshot_stats |
| `backend/app/services/prompt_composer.py` | 注入 `INTENT_GUIDES[correction]` |
| `backend/scripts/selftest_p0.py` | `test_correction_engine_pure` 测试集 |

### 11.3 LLM 判断 schema

```python
class CorrectionLLMOutput(TypedDict):
    actions: list[CorrectionAction]
    banned_entities: list[str]   # 用户明确否定的实体词

class CorrectionAction(TypedDict):
    ref_id: str
    source: str                  # episodic / event / profile
    action: str                  # keep / deprecate / update / audit_only
    new_text: str | None         # 仅 action=update 时填
    confidence: float            # 0.0 - 1.0
    reason: str
```

- LLM 模型：`CORRECTION_MODEL`（默认 `qwen3.7-plus`）
- `temperature=0` + `response_format={"type":"json_object"}` + `extra_body={"enable_thinking": False}`
- `confidence ≥ 0.6` 才落库；`audit_only` 永远只写 audit 不动数据

### 11.4 Banned Entities 工作机制

被否定的实体写入 `memory_deprecations(source='entity', ref_id=<entity_text>)`，主链路有两处使用：

| 使用点 | 行为 |
|---|---|
| `memory_context._load_banned_entities()` | 召回时过滤命中 banned 的 episodic 和 event |
| `celery_worker.extract_*` 写入前 | 过滤命中 banned 的新提取候选，避免下轮再写入 |

辅助函数：`correction_engine.load_banned_entities(user_id, db)` / `text_hits_banned(text, banned)`。

---

## 12. 真实聊天评估 Pipeline *(v1.2)*

详见 [Real-Chat-Eval.md](./Real-Chat-Eval.md)。本节给出技术骨架。

### 12.1 数据模型

每条 assistant 消息在写库时携带一份 `prompt_meta`（详见 §6.4），关键字段：

```jsonc
{
  "version": "turn_meta_v1",
  "route": { ... },
  "activated": [ ... ],
  "context_layers": { stable_profile, relevant_relationships, ... },
  "snapshot_stats": {
    "episodic_total": 12,
    "episodic_filtered_deprecated": 1,
    "episodic_filtered_banned": 0,
    "events_filtered_banned": 0,
    "banned_entities": []
  }
}
```

评估时**只读** assistant 消息里的 `prompt_meta`，不需要重放 Memory Router → 评估成本 ≈ 0 token（除 LLM judge 规则外，启发式规则为本地 Python）。

### 12.2 模块清单

| 文件 | 职责 |
|---|---|
| `backend/app/services/chat_audit.py` | 把会话 + 每轮 prompt_meta 拼成 `chat_audit_v1` JSON |
| `backend/app/services/eval_chat_review.py` | L1 启发式规则集（10+ 条），输入 audit pack，输出 review pack |
| `backend/app/routers/eval.py` | API `POST /api/eval/chat-review/{conversation_id}` |
| `backend/app/routers/conversations.py` | 导出 `chat_audit_v1` JSON |
| `frontend/src/views/EvalView.vue` + `EvalQueryPanel.vue` | 评测实验室 UI，新增「线上聊天记录」Tab |

### 12.3 L1 启发式规则（v1.2 列出当前 10 条）

| 规则 ID | 描述 | 实现思路 |
|---|---|---|
| `reply_off_topic` | 回复与用户问题主题脱节 | 用户问句关键 token 在 reply 里的覆盖率 < 30% |
| `recall_for_challenge` | 用户质问记忆时，AI 引用了池里实体 | 检查 reply 是否提到 `activated` 中的实体 |
| `data_vs_activation_gap` | 数据库里有但本轮没激活 | 比 `context_layers` 与 `activated` 的差集 |
| `fabrication_under_challenge` | 质问时 AI 编造池外实体 | reply 中提到的实体不在 `context_layers` 任何一层 |
| `correction_persisted` | 上一轮被纠正的实体仍在本轮 `context_layers` | 跨轮检查（用 `prev_turn` 入参） |
| `correction_no_concrete_ack` | 纠错时 AI 含糊承认/推卸 | 命中 `_CORRECTION_DEFLECT_PHRASES` |
| `forbidden_phrases_in_reply` | 客服腔/卖萌/鸡汤 | 与 `BASE_PERSONA` 禁用句式表同源 |
| `sensitive_unsolicited` | 敏感记忆未被询问就主动提 | 用户消息不含 trigger 词，但 reply 提了 background_only 条目 |
| `relationship_subject_inferred` | 代词消解错 | reply 把"她"指向了错误关系人 |
| `followup_overflow` | 单轮 follow-up 事件 > 1 | 数 `activated` 里 `usage=follow_up_once` 的条数 |

### 12.4 API

```
GET    /api/conversations/{id}/audit       -- 导出 chat_audit_v1 JSON
POST   /api/eval/chat-review/{id}          -- 跑 L0+L1 评估，返回 review pack
GET    /api/eval/chat-review/{id}          -- 取已跑过的 review（可选缓存）
```

### 12.5 评估输出

```jsonc
{
  "version": "eval_review_v1",
  "conversation_id": "...",
  "turns": [
    {
      "turn_id": "...",
      "user": "...",
      "assistant": "...",
      "l0": {"ok": true, "missing": []},
      "l1": [
        {"rule": "correction_persisted", "severity": "high",
         "msg": "上一轮纠正的实体「小鹏」仍出现在本轮 background_only"}
      ],
      "attribution": "上一轮 correction cleanup 未生效"
    }
  ],
  "summary": {"high": 1, "medium": 2, "low": 0, "total_turns": 8}
}
```

### 12.6 成本与触发策略

- 评估**不自动触发**，用户在 EvalView 主动点击「评估」按钮
- 评估本身是纯 Python，**0 LLM 调用**（启发式 + audit pack）
- 报告可被导出，下次相同 conv 不重跑（前端缓存）
- 后续如引入 LLM Judge（v1.3 规划），仅对启发式标红的 turn 调 LLM，控制成本
