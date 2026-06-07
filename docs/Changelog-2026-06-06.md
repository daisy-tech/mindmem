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

## 11. v1.2.1 增量 — 人格契约可执行化（2026-06-07）

### 11.1 背景

回放 6/7 三份真实聊天（内向/中性/外向）后用户反馈"三种性格区分度不明显"，且发现 prompt 里并没有可执行的差异。

### 11.2 根因

| 问题 | 现象 |
|---|---|
| `PERSONALITY_TONE` 太宽 | 仅"短/平/多"三类描述，模型无法转化为可执行规则 |
| `sensitive_mode` 注入屏蔽 | `memory_challenge` / `emotional_support` / `correction` 全部走 `sensitive_mode=True`，性格契约被完全跳过，结果三种人格在敏感场景输出几乎一样 |
| `INTENT_GUIDES["casual"]` 与契约冲突 | guide 允许"反问一句开放问题"，与内向契约"不主动反问"矛盾，LLM 优先按 guide 执行 |
| `_PERSONALITY_SKIP_INTENTS` 过宽 | eval 也跳过敏感 intent，掩盖了 AI 行为同质化 |

### 11.3 修复

1. **`PERSONALITY_CONTRACT` 重写**（`prompt_composer.py`）：每段含 4 个子分支
   - 普通话题 / 质问记忆时 / 纠错时 / 情绪场景
   - 三段长度差异：内向 ≤30 字、中性 ≤60 字、外向 ≤90 字
   - 内向"不主动反问"、中性"最多 1 开放反问"、外向"1 反问 + 1 具体建议"
2. **`PERSONALITY_CONTRACT_INTENTS` 扩充**：加入 `memory_challenge` / `emotional_support` / `correction`
3. **去 `sensitive_mode` 注入条件**：除 `knowledge_task` 外全部注入
4. **`INTENT_GUIDES["casual"]` 修复冲突**：删除"可反问一句开放问题"，改为"反问数量与是否引用记忆由本轮人格契约决定"
5. **`eval_chat_review._PERSONALITY_SKIP_INTENTS` 收窄**：仅保留 `knowledge_task`
6. **`_rule_personality_signature` 调整**：内向"主动引用记忆"豁免列表加上 `memory_challenge` / `correction`（这些 intent 本就要求引用）

### 11.4 回放验证（6/7 三份对话）

| 文件 | 人格 | 共轮 | final_status | personality_signature |
|---|---|---:|---|---|
| 内向型 | introvert | 15 | bad:14, suspicious:1 | **fail:14**, skip:1 |
| 中性型 | balanced | 11 | ok:5, bad:3, suspicious:3 | **pass:8**, skip:3 |
| 外向型 | extrovert | 21 | ok:9, bad:8, suspicious:4 | **pass:16**, skip:4, fail:1 |

**结论**：
- 评估系统现在能精准识别"人格不一致"——内向型 14/15 fail 与用户直观感受完全吻合（AI 在反问 + 主动引用 3~9 条旧记忆，典型外向行为）。
- 外向型唯一 fail 是 "我能想到的是 X" 泛泛模板，恰好被新契约禁用，规则准确命中。
- 中性/外向 pass 率高，说明 LLM 在这两种契约下行为基本符合预期。

### 11.5 v1.2.2 hotfix — 移除 BASE_PERSONA 与契约重复 / 矛盾

用户检查 v1.2.1 prompt 后指出 BASE_PERSONA 里几条与人格契约存在重复或冲突。

| 旧条目 | 与契约冲突点 |
|---|---|
| BASE「单句≤30字；一般 1-3 句；情绪场景可只一句」 | "单句"vs"总长"语义混淆；"1-3 句"与外向"2-4 句"直接冲突 |
| BASE「不主动复述记忆里/用户已说过的事实」 | 与中性/外向"可引用 1 条具体旧记忆"语义打架 |
| `PERSONALITY_TONE` 字典 + 「人格：{persona_tone}」一行 | 残留旧描述式人格，与契约段重复 |
| HARD_RULES「敏感场景下人格"主动性"被覆盖，不主动翻旧账、不复述背景」 | v1.2.1 已让敏感场景注入契约，契约里"质问记忆时"明确允许"挑 1 个具体候选"，与本条直接打架 |

**修复**（`prompt_composer.py`）：
1. BASE_PERSONA 删除字数/句数条 + 改"不主动复述记忆"为"不机械复述用户刚说过的话"
2. BASE_PERSONA 新增一行：「回复字数、句数、是否反问、是否引用旧记忆 —— 全部由本轮人格契约决定」明确分工
3. 整段删除 `PERSONALITY_TONE` 字典
4. 删除 `compose()` 里 `persona_tone` 读取和"【本轮】人格：{persona_tone}"渲染行
5. HARD_RULES 末行改为「敏感场景（情绪/纠错/质问）：执行本轮人格契约里对应子分支」，把权威指引交回契约段，消除冲突

selftest 102/102 仍通过。TDD §6.4 样例同步更新。

### 11.6 后续

内向型大面积 fail 是**模型实际行为**问题（不是契约设计问题）。下一步可选：
- 在 chat.py 后置做"长度截断"硬约束（违反契约时截掉超出部分）
- 把契约前置到 `BASE_PERSONA` 上方，提升 LLM 注意权重
- 收集若干 fail case 做 few-shot 注入

### 11.7 v1.2.3 增量 — 评估结果服务端落盘 + 列表持久化展现

**背景**：用户反馈两点：
1. 评估实验室的下载文件落到 mac 本地，每次都要再 scp 到 ECS 才能让我（Cursor）分析
2. 已评估过的对话每次都要再点「评估」才能看到结果

**改动**：
- 新增 `backend/app/services/eval_chat_store.py`：覆盖式落盘到 `backend/eval/exports/reviews/{user_id}/{conv_id}.json`（NFS 共享后 mac/ECS/容器同一份文件）
- `GET /api/eval/chat-audit/{conv_id}` 加 `force` 参数；默认命中磁盘直接返回，force=true 才重跑覆盖
- 新增 `GET /api/eval/chat-audit-stored`（列表）和 `DELETE /api/eval/chat-audit-stored/{conv_id}`
- 前端 `stores/evalChat.ts`：
  - 加 `storedMeta` 状态，进入页面顺带拉
  - `select(convId)` 自动 silent 加载已落盘 review（无 loading 旋钮）
  - 加 `deleteStored()`
- `EvalChatPanel.vue`：
  - 列表项加左侧绿色侧栏 + ✓标 + 三色 mini-bar（ok/suspicious/bad 按比例切分）
  - 工具栏按钮根据 stored 状态切换：未存→「开始评估」/已存→「重新评估」+「清除已存」
  - 选中已评估的对话直接显示报告，无需用户再点
- `docker-compose.yml` 加 `EVAL_CHAT_REVIEWS_DIR=/app/eval/exports/reviews`
- `.gitignore` 加 `backend/eval/exports/`（含真实对话内容，不入仓）
- selftest 加 9 条 `test_eval_chat_store`（落盘/读盘/列表/删除/路径穿越防御）

selftest: **111/111 通过**

### 11.8 测试

```
backend/scripts/selftest_p0.py
合计: 111/111 通过 ✅
```

新增/调整：
- `test_prompt_composer`：emotional_support / memory_challenge 注入契约的断言；knowledge_task 不注入的断言
- `test_personality_signature_rule`：内向 memory_challenge / self_summary 引用应 pass；内向 emotional_support 短回应 pass、反问 fail；balanced correction 承认 pass；knowledge_task skip

---

## 12. 后续规划（v1.3 预告）

- L1 启发式 → 引入 LLM Judge（仅对启发式标红的 turn 调，控成本）
- 评测实验室加跨会话 trend / 用户级评分卡
- Banned entities 支持语义近邻匹配（embedding 而非子串）
- 事件层独立 tombstone 表（不再复用 `status`）
- Real-Chat-Eval 报告一键回灌合成评测集
