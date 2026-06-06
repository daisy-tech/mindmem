# 在线记忆纠错管线 Correction Pipeline

> 版本：1.0 | 日期：2026-06-06 | 状态：Released
> 关联文档：[PRD §2.10](./PRD.md#210-在线记忆纠错correction-pipeline) · [TDD §11](./TDD.md#11-在线记忆纠错管线-v12)

---

## 1. 设计目标

让用户在对话中能用人话纠正 AI 的记忆错误，AI 必须：

1. **当下**用第一人称承认，并**复述用户给出的正确事实**，让用户能判断"听懂了没"
2. **自然不解释**：不要说"我会更新字段"、"系统是怎么记的"等技术细节
3. **不再犯**：下次相同问题，AI 不能再次说出被纠正过的实体/事实

技术目标：

- 不影响主对话延迟（清理走 Celery 异步）
- 不在 LLM 不确定时误删数据（带 confidence 阈值 + audit_only 兜底）
- 可审计、可回滚（软删除 + tombstone 表）
- 跨三层（episodic / event / profile）联动，加上「实体硬封禁」

---

## 2. 触发与隔离

### 2.1 触发条件

`memory_router.route()` 返回 `intent == "correction"` 时触发。判定路径：

| 路径 | 触发方式 |
|---|---|
| Layer 1 硬规则 R1 | 命中"你记错了 / 别再提 / 忘掉…"等强模式词 |
| Layer 2 分类器 | 小模型识别为 correction，confidence ≥ 0.5（低于则降级） |

### 2.2 当轮跳过 extract\_\*（关键）

在 `backend/app/routers/chat.py` 流式结束的副作用阶段：

```python
if route.intent == "correction":
    run_correction_cleanup_task.delay(
        user_id=user.id,
        conversation_id=conv_id,
        turn_id=turn_id,
        messages=messages_for_extract,
    )
else:
    extract_and_store_memory.delay(...)
    extract_and_update_profile.delay(...)
    extract_and_store_events.delay(...)
```

**为什么必须跳过 extract\_\*？**

用户在纠错时，assistant 的回复里仍可能复述错误事实的相关词（如"你提到的小鹏…"）。如果照常跑 extract，会把"被纠正的错误"当成"新事实"再学一遍 → 记忆永久污染。

实测：未做这个隔离前，连续纠错 3 轮后旧错误事实数量反而**增加**了。

---

## 3. 数据模型

### 3.1 `memory_deprecations` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `user_id` | TEXT INDEX | 用户隔离 |
| `source` | TEXT | `episodic` / `event` / `profile` / `entity` |
| `ref_id` | TEXT INDEX | mem0_id / event_id / dimension_path / entity_text |
| `original_text` | TEXT | 被废弃的原文（便于审计/回滚） |
| `reason` | TEXT | LLM 给出的简短理由 |
| `correction_conversation_id` | TEXT | 触发会话 |
| `correction_turn_id` | TEXT | 触发轮次 |
| `llm_confidence` | REAL | LLM 给出的置信度 |
| `action` | TEXT | `deprecate` / `update` / `audit_only` |
| `new_text` | TEXT | 仅 `action='update'` 时填 |
| `deprecated_at` | DATETIME | 写入时间 |
| `restored_at` | DATETIME | 软撤销时填，仅 NULL 时生效 |

### 3.2 ORM 模型

ORM 见 `backend/app/models/deprecation.py`，38 行，无外键，所有写入由 `correction_engine` 集中管理。

---

## 4. 核心模块：`correction_engine.py`

### 4.1 入口

```python
def run_correction_cleanup(
    user_id: str,
    conversation_id: str,
    turn_id: str,
    messages: list[dict],
) -> dict[str, Any]:
    """对单次 correction turn 跑清理。
    
    Returns: {"actions_applied": int, "banned_entities": [...], "errors": [...]}
    所有异常被吞，只 warning。
    """
```

### 4.2 处理流程

```text
messages (最近 6 轮)
    │
    ▼ _slice_recent_turns + _extract_correction_target
找到 [被纠正的 asst 回复, 用户纠正语]
    │
    ▼ _extract_query_tokens
拆分用户/asst 文本为关键 token（中文 2-gram/3-gram + 去停用词）
    │
    ├──▶ _search_episodic_candidates_multi → Mem0 多 token 检索 (≤6 条)
    ├──▶ _search_event_candidates → SQLite 关键词扫描 (≤5 条)
    └──▶ _flatten_profile_fields → 扁平化 profile 高置信字段 (≤6 条)
    │
    ▼ _llm_judge (qwen3.7-plus, JSON output, temperature=0)
{
  "actions": [{ref, action, confidence, reason, new_text?}, ...],
  "banned_entities": [...]
}
    │
    ▼ 按 source 路由
    ├──▶ _apply_episodic   →  写 memory_deprecations(source='episodic')
    ├──▶ _apply_event      →  UPDATE user_events SET status='deprecated'
    │                          + 写 memory_deprecations(source='event')
    ├──▶ _apply_profile    →  清字段 + 追加 interaction_history.user_corrections
    │                          + 写 memory_deprecations(source='profile', action='update')
    ├──▶ _apply_audit_only →  低置信度仅审计，不动数据
    └──▶ _apply_banned_entities → 写 memory_deprecations(source='entity') × N
```

### 4.3 关键参数

| 常量 | 默认值 | 来源 |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | 0.7 | `CORRECTION_CONFIDENCE_THRESHOLD` env |
| `CORRECTION_MODEL` | `qwen3.7-plus` | `CORRECTION_MODEL` env，否则 fallback `EXTRACT_MODEL` |
| `MAX_EPISODIC` | 6 | 防 prompt 超长 |
| `MAX_EVENTS` | 5 | 防 prompt 超长 |
| `MAX_PROFILE_FIELDS` | 6 | 防 prompt 超长 |

### 4.4 LLM 判断 Schema

System prompt（节选）：

```text
你是一个【记忆维护助手】。用户刚刚在对话里【纠正了】MemoBot 的某个说法。
你的任务：从下列「候选旧记忆」中找出与用户纠正【直接矛盾】的条目，判断如何处理。
**不要泛化、不要清扫所有看起来沾边的旧记忆。**

输出严格 JSON 对象：
{
  "actions": [{"ref": "...", "action": "keep"|"deprecate"|"update",
               "confidence": 0.0-1.0, "reason": "≤30字", "new_text": "仅 update 必填"}],
  "banned_entities": ["<被本次纠正彻底否定的实体词，≤8字>"]
}
```

API 调用：

```python
client.chat.completions.create(
    model=CORRECTION_MODEL,
    messages=[{"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
              {"role": "user", "content": payload}],
    temperature=0,
    response_format={"type": "json_object"},
    extra_body={"enable_thinking": False},
)
```

### 4.5 三层 apply 细节

#### 4.5.1 `_apply_episodic`

- 不真的 `mem0.delete()`，而是写 `memory_deprecations(source='episodic', ref_id=<mem0_id>)`
- 主链路 `memory_context._load_deprecated_episodic_ids()` 召回时硬过滤
- `action='update'`：先写 deprecation 标记旧条，再 `mem0.add(new_text, infer=False)` 加一条新条

#### 4.5.2 `_apply_event`

- `UPDATE user_events SET status='deprecated' WHERE event_id=?`
- 同步写 `memory_deprecations(source='event')`，便于审计
- `event_engine.search_events()` 默认只取 `status='active'`，自动跳过

#### 4.5.3 `_apply_profile`

- 字段路径如 `basic.location`，深路径用 `_clear_profile_field()` 把值清空（或设回 `null`）
- 在 `interaction_history.user_corrections` 追加：
  ```json
  {"field": "basic.location", "old": "杭州", "new": "北京",
   "reason": "...", "at": "2026-06-06T22:54:00+08:00",
   "turn_id": "..."}
  ```
- 写 `memory_deprecations(source='profile', ref_id=<dimension_path>, action='update')`

#### 4.5.4 `_apply_audit_only`

confidence 不达标的，只写一条 `action='audit_only'` 的记录，**不动数据**。便于后续：
- 调阈值后批量重判
- 审计 LLM 判断质量
- 训练数据沉淀

---

## 5. Banned Entities 硬封禁机制

### 5.1 写入

LLM 在 `banned_entities` 字段输出"被用户明确否定的实体词"。例：

> 用户："小鹏不是动物，是儿子的同学"  
> LLM 输出：`"banned_entities": ["小鹏"]`（注意：不是 "小鹏不是动物"，是单一实体词）

去重后写入 `memory_deprecations(source='entity', ref_id=<entity_text>, action='deprecate')`。

### 5.2 读取

`correction_engine.load_banned_entities(conn, user_id) -> list[str]`：

```sql
SELECT DISTINCT ref_id FROM memory_deprecations
WHERE user_id=? AND source='entity' AND restored_at IS NULL
ORDER BY deprecated_at DESC LIMIT 100
```

### 5.3 使用点

| 位置 | 行为 |
|---|---|
| `memory_context._load_banned_entities()` | 召回时，**episodic** 文本含 banned → 直接过滤；**events** 同理 |
| `celery_worker.extract_and_store_memory` 写入前 | 新提取候选含 banned → 跳过 |
| `celery_worker.extract_and_update_profile` 写入前 | 新画像 fact 含 banned → 跳过 |
| `celery_worker.extract_and_store_events` 写入前 | 新事件 summary 含 banned → 跳过 |

辅助函数：`correction_engine.text_hits_banned(text, banned) -> bool`（精确子串匹配）。

### 5.4 撤销

用户日后说"小鹏现在确实是只仓鼠了"（极少见），可走运维脚本 `UPDATE memory_deprecations SET restored_at=now() WHERE source='entity' AND ref_id='小鹏'`。本期不提供 UI。

---

## 6. Prompt 侧配合

`prompt_composer.INTENT_GUIDES["correction"]` 强制约束 LLM 的回复结构：

```text
【纠错指引】
1. 一句话第一人称承认："我记错了 / 我搞混了 / 是我没分清"
2. **复述用户给出的正确事实**作为"我已更新到的认识"——这是用户判断你听懂的关键
   ✅ "记错了，小孙孙和小魏魏是儿子的同学，不是宠物，我按这个来。"
   ❌ "请你告诉我一下，我会更新记忆。"（推卸）
   ❌ 继续用被纠正的实体词（错上加错）
≪禁止≫再次提及被纠正的实体名词；把被纠正的关联话题当原话题继续追问；
"我会更新记忆/修正字段"等机械系统语；解释"系统是怎么记的"
做完上面 1-2 后自然停下，或问一句与本次纠错无关的开放追问。
```

这保证当下回复就先满足体感，后台清理是"额外保险"。

---

## 7. 评估配合

`eval_chat_review.py` 新增 3 条 L1 规则，定向覆盖纠错失败模式：

| 规则 | 检测 | 严重度 |
|---|---|---|
| `correction_persisted` | 上一轮被纠正的实体仍在本轮 `context_layers` | high |
| `correction_no_concrete_ack` | 纠错 reply 无第一人称承认 / 命中含糊推卸短语 | high |
| `fabrication_under_challenge` | memory_challenge 时 reply 含池外实体 | high |

含糊推卸短语示例（`_CORRECTION_DEFLECT_PHRASES`）：

```python
[
    "请你再告诉我一次",
    "可以告诉我吗",
    "我会更新记忆",
    "我会修正",
    "请你告诉我",
    "稍后会调整",
    # ...
]
```

---

## 8. 测试

`backend/scripts/selftest_p0.py` 中 `test_correction_engine_pure` 覆盖：

- `_extract_correction_target` 在不同 messages 长度下的稳定性
- `_extract_query_tokens` 中文 2-gram/3-gram 去停用词
- `_flatten_profile_fields` 各类嵌套画像的扁平化
- `_apply_episodic / _apply_event / _apply_profile` 不抛异常
- `_apply_banned_entities` 去重写入 + 不重复入表
- `text_hits_banned` 命中/不命中
- `_llm_judge` 解析合法/非法 JSON 的鲁棒性

当前 68/68 通过。

---

## 9. 运维与排查

### 9.1 查看某用户的所有 deprecation

```sql
SELECT source, ref_id, original_text, action, llm_confidence, deprecated_at
FROM memory_deprecations
WHERE user_id='<uid>' AND restored_at IS NULL
ORDER BY deprecated_at DESC;
```

### 9.2 临时关闭硬封禁（debug）

直接在 SQLite 把 `source='entity'` 的记录 `restored_at` 写当前时间即可，不需要重启服务（每次召回都现查）。

### 9.3 调阈值

```bash
# 让 LLM 判断更激进
docker compose exec backend env CORRECTION_CONFIDENCE_THRESHOLD=0.55 ...
# 让 LLM 判断更保守（仅高置信才落库）
docker compose exec backend env CORRECTION_CONFIDENCE_THRESHOLD=0.85 ...
```

未达阈值的都会走 `audit_only`，事后可以 SELECT 出来重判。

---

## 10. 已知限制 & 后续规划

| 项 | v1.2 现状 | 后续 |
|---|---|---|
| 事件层 | 复用 `status='deprecated'`，未单独 tombstone | v1.3 考虑独立 `event_tombstones` |
| Banned 撤销 UI | 无 | 用户高频纠错回滚需求出现后再做 |
| Vector 层语义 banned | 仅子串匹配 | v1.3 用 embedding 近邻匹配（如"小鹏" → "小朋"） |
| 多轮联动纠错 | 单 turn LLM 决策 | v1.3 跨多 turn 累积 evidence |
| 跨 user banned 学习 | 不共享 | 隐私敏感，长期不做 |
