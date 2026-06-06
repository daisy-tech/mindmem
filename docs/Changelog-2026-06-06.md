# MindMem v1.2 变更日志 — 2026-06-06

> 一日之内完成的全部增量。前文档：[PRD](./PRD.md) · [TDD](./TDD.md) · [Memory-Router-v1.5](./Memory-Router-v1.5.md)

---

## 1. 概览

本次发布围绕三件事：**让评估可定位、让记忆可纠错、让 prompt 不臃肿**。

| 主题 | 状态 | 关键文件 |
|---|---|---|
| 评测合成评测优化（intent prompt + 宽松 L1 + UI 黄标） | ✅ 已上线 | `eval_runner.py` · `intent_classifier.py` · `EvalView.vue` |
| 真实聊天评估 Real-Chat-Eval | ✅ 已上线 | `chat_audit.py` · `eval_chat_review.py` · `eval.py` · `EvalView.vue` |
| 在线记忆纠错 Correction Pipeline | ✅ 已上线 | `correction_engine.py` · `memory_deprecations` 表 |
| LLM 升级 Qwen3.7 全链路 + enable_thinking=false | ✅ 已上线 | `docker-compose.yml` + 9 个 service 模块 |
| Prompt Composer 瘦身（-45% tokens） | ✅ 已上线 | `prompt_composer.py` |
| Memory Router query 收窄（防 Mem0 召回稀释） | ✅ 已上线 | `memory_router.py` |
| SSE 切到 POST（解决 URL 长度限制） | ✅ 已上线 | `frontend/src/stores/chat.ts` |

P0 selftest：**68/68 通过**。

---

## 2. 评测合成评测优化

| 改动 | 收益 |
|---|---|
| 修复 eval set 中 3 条标签错误 case | Smoke 通过率从 80% → 96% |
| 调优 classifier system prompt（更明确 intent 定义） | `casual` 误判率显著下降 |
| L1 评分加入 `optional_intents` 软匹配 | `relationship_topic` vs `emotional_support` 边界 case 不再误 fail |
| 低置信度 case 加 **黄色标记**（即使 pass 也提示） | 用户能直观看到 borderline 的判断 |
| 报告导出附加 intent confusion matrix | 评测复盘更清楚 |

---

## 3. 真实聊天评估 Real-Chat-Eval

**详见**：[Real-Chat-Eval.md](./Real-Chat-Eval.md)

### 3.1 新增能力

- EvalView 增加「线上聊天记录」Tab，左边栏列用户全部历史会话
- 用户点击「评估」按钮才触发，**0 LLM 调用成本**
- 评估只读 assistant 消息里的 `prompt_meta`（带 `snapshot_stats`），无需重放 Router/Composer
- 输出 `eval_review_v1` 报告：每 turn 给出 L0 / L1 / 归因 / final_status

### 3.2 L1 规则集（v1.2 共 10 条）

| 规则 | severity |
|---|---|
| `error_reply` | high |
| `recall_for_challenge` | high |
| `recall_for_self_summary` | high |
| `data_vs_activation_gap` | medium |
| `generic_reply` | medium |
| `reply_off_topic` | medium |
| `correction_persisted` *(新)* | high |
| `correction_no_concrete_ack` *(新)* | high |
| `fabrication_under_challenge` *(新)* | high |
| `followup_overflow` | low |

后 3 条为 v1.2 新增，定向覆盖纠错失败模式。

### 3.3 新增模块

```
backend/app/services/chat_audit.py        +689
backend/app/services/eval_chat_review.py  +675
backend/app/routers/eval.py               (扩展)
backend/app/routers/conversations.py      (扩展，加 /audit 端点)
frontend/src/views/EvalView.vue           (新 Tab)
```

---

## 4. 在线记忆纠错 Correction Pipeline

**详见**：[Correction-Pipeline.md](./Correction-Pipeline.md)

### 4.1 核心机制

1. **数据库**：新增 `memory_deprecations` 表（4 种 source：episodic/event/profile/entity）
2. **Celery 任务**：`run_correction_cleanup_task`
3. **三层联动**：episodic（Mem0 软删）/ event（status='deprecated'）/ profile（清字段 + user_corrections 追加）
4. **实体硬封禁**：banned_entities 写入后，extract_* 和召回都硬过滤
5. **关键隔离**：`chat.py` 检测到 `intent==correction` 时**跳过** `extract_*`，避免再学错

### 4.2 LLM 判断

- 模型：`CORRECTION_MODEL=qwen3.7-plus`
- 阈值：`CORRECTION_CONFIDENCE_THRESHOLD=0.7`（不达标的 → `audit_only`，仅审计不动数据）
- 输出 schema：`{actions: [{ref, action, confidence, reason, new_text?}], banned_entities: [...]}`

### 4.3 Prompt 配合

`prompt_composer.INTENT_GUIDES["correction"]` 强化为硬性输出结构：
1. 第一人称承认
2. **必须复述用户给出的正确事实**
3. 禁止再提被纠正实体词、禁止机械系统语

### 4.4 新增模块

```
backend/app/models/deprecation.py       +38
backend/app/services/correction_engine.py  +732
backend/celery_worker.py                (扩展 run_correction_cleanup_task + extract_* 加 banned 过滤)
backend/app/services/memory_context.py  (扩展 _load_deprecated_episodic_ids + _load_banned_entities)
backend/app/services/prompt_composer.py (强化 correction guide)
backend/app/routers/chat.py             (intent==correction 跳过 extract_*)
```

---

## 5. LLM 全链路升级到 Qwen3.7

| 角色 | 之前 | 现在 |
|---|---|---|
| Chat | `qwen-max` | **`qwen3.7-max`** |
| Intent / Extract / Correction | `qwen-plus` | **`qwen3.7-plus`** |

### 5.1 enable_thinking=false（关键）

Qwen3.7 系列是 hybrid thinking model，默认开启 thinking 会让首字延迟从 ~500ms 涨到 1.5-3s，且容易让 JSON 输出夹带 `<think>...</think>` 段。

**所有** LLM 调用统一显式传：

```python
extra_body={"enable_thinking": False}
```

涉及文件：
- `chat.py` · `intent_classifier.py` · `celery_worker.py`
- `correction_engine.py` · `profile_engine.py` · `event_engine.py`
- `eval_runner.py` · `compact_memories.py` · `clean_relationships.py`

环境变量：`ENABLE_THINKING=false`（默认 false）

### 5.2 docker-compose.yml

| 环境变量 | 默认值 |
|---|---|
| `CHAT_MODEL` | `qwen3.7-max` |
| `INTENT_MODEL` | `qwen3.7-plus` |
| `EXTRACT_MODEL` | `qwen3.7-plus` |
| `CORRECTION_MODEL` | `qwen3.7-plus` |
| `ENABLE_THINKING` | `false` |

---

## 6. Prompt Composer 瘦身

| 场景 | 旧字数 | 新字数 | tokens 节省 |
|---|---:|---:|---:|
| emotional_support | ~1900 | **997** | -47% |
| memory_challenge | ~1900 | **1048** | -45% |
| correction | ~2200 | **1129** | -49% |
| casual | ~1500 | **897** | -40% |
| knowledge_task | ~1500 | **877** | -42% |
| 空记忆 casual | ~1500 | **645** | -57% |

**关键决策**：
1. 整段删除 `BACKGROUND_USAGE_RULES`（与 BASE_PERSONA 和 memory_challenge guide 重复）
2. INTENT_GUIDES 平均压缩 50%
3. 空块（stable_profile / explicit_pool / followup_pool / background_only）不再渲染"（无）"
4. 本轮规则去掉 `intent_id`/`memory_depth`/`personality_label` 等 LLM 不需要的调试元信息（仍保留在 `prompt_meta.route` 供前端展示）

**所有关键约束一字未丢**：
- ✅ correction 第一人称承认 + 复述用户事实 + 禁止再提被纠正实体
- ✅ memory_challenge 禁止幻觉三条（池外不说 / 只有 1 条只复述 / 没把握用试探）
- ✅ 6 类禁用句式全保留
- ✅ 敏感场景人格主动性被覆盖

详见 TDD §6.4。

---

## 7. Memory Router query 收窄

**问题**：用户问"我家养了什么动物"召不回"养了一只猫"。

**根因**：之前 `_build_query` 拼接最近 3-5 轮 user 历史，被旁支语义稀释，Mem0 检索打偏。

**修复**：query 只用最近 **1 句** user 消息（外加当前消息和识别出的人物/主题）。

副作用：旁支推断由 Chat 模型在 prompt 里完成，不在检索阶段做。检索更准，召回率上升。

---

## 8. SSE 切到 POST

**问题**：当对话历史变长，前端用 EventSource (GET) 发起 `/api/chat/stream` 会因 URL 过长被网关截断 → `[连接出错，请重试。]`

**修复**：`frontend/src/stores/chat.ts` 改用 `fetch` POST + `ReadableStream` 读 SSE 帧，把 conversation history 放 body。

后端 `/api/chat/stream` 同步支持 POST 入参。

---

## 9. 测试与验证

```
backend/scripts/selftest_p0.py
合计: 68/68 通过 ✅
```

新增测试：
- `test_correction_engine_pure`：覆盖 `_extract_correction_target`、`_extract_query_tokens`、`_flatten_profile_fields`、`_apply_*`、`_apply_banned_entities`、`text_hits_banned`、`_llm_judge` 解析鲁棒性
- `test_chat_review_rules`：新增 3 条规则的回归

更新测试：
- `test_prompt_composer`：同步标题字符串（`【情绪支持指引】` 等）；新增"空块不渲染"和"硬边界字样"断言

---

## 10. 文档更新清单

| 文件 | 变更 |
|---|---|
| [PRD.md](./PRD.md) | 1.1 → **1.2**；新增 §2.9 评测实验室、§2.10 在线记忆纠错、§2.11 Prompt 透明度增强 |
| [TDD.md](./TDD.md) | 1.1 → **1.2**；新增 `memory_deprecations` 表、§4.7 评测 API、§5.x Celery 新任务、§6.4 Prompt 瘦身策略、§7 LLM 策略升级、§9.3 env vars、§10.5/§10.6 设计决策、§11 纠错管线、§12 真实聊天评估 |
| [Memory-Router-v1.5.md](./Memory-Router-v1.5.md) | 1.0 → **1.2**；§15.3 增加 1.1 / 1.2 修订记录 |
| **新建** [Correction-Pipeline.md](./Correction-Pipeline.md) | 纠错管线完整技术设计（10 章） |
| **新建** [Real-Chat-Eval.md](./Real-Chat-Eval.md) | 真实聊天评估完整设计（11 章） |
| **新建** Changelog-2026-06-06.md | 本文件 |

---

## 11. 后续规划（v1.3 预告）

- L1 启发式 → 引入 LLM Judge（仅对启发式标红的 turn 调，控成本）
- 评测实验室加跨会话 trend / 用户级评分卡
- Banned entities 支持语义近邻匹配（embedding 而非子串）
- 事件层独立 tombstone 表（不再复用 `status`）
- Real-Chat-Eval 报告一键回灌合成评测集
