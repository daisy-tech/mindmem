# 真实聊天评估 Real-Chat-Eval

> 版本：1.0 | 日期：2026-06-06 | 状态：Released
> 关联文档：[PRD §2.9.2](./PRD.md#292-真实聊天评估线上聊天记录) · [TDD §12](./TDD.md#12-真实聊天评估-pipeline-v12)

---

## 1. 为什么需要"真实聊天评估"

合成评测（persona_a_zhang + 50 case）解决"已知问题不回归"；真实聊天评估解决"用户的实际体感是不是真的好"。

| 维度 | 合成评测 | 真实聊天评估 |
|---|---|---|
| 数据来源 | 固定 case fixture | 用户线上历史会话 |
| 触发方式 | 一键跑 Smoke / Full | 用户在评测实验室点会话 + 点"评估" |
| LLM 成本 | 每跑一次几十次 LLM 调用 | **0 次 LLM 调用**（纯启发式 + audit pack） |
| 价值 | 防回归、做 PR gate | 定位"这条对话哪里不舒服" |

两者互补：用户在真实聊天评估发现的失败模式 → 提炼成 case 灌回合成评测。

---

## 2. 数据基础：`chat_audit_v1`

### 2.1 设计原则：评估时不重放

如果评估时再跑一遍 Memory Router、Composer，会有两个问题：
- 召回结果不稳定（向量库可能已经变了）
- 成本高（每次评估 ≈ 一次重放）

解决：**每条 assistant 消息写库时就携带当时的 `prompt_meta`（嵌在 message content 里的 HTML 注释或 messages_json 的 meta 字段）**。

### 2.2 `chat_audit_v1` 包结构

```jsonc
{
  "version": "chat_audit_v1",
  "conversation_id": "conv_xxx",
  "user_id": "u_xxx",
  "exported_at": "2026-06-06T22:54:00+08:00",
  "stats": {"total_turns": 8, "broken_turns": 0},
  "turns": [
    {
      "turn_id": "t1",
      "input": {"user_message": "...", "timestamp": "..."},
      "output": {"assistant_reply": "...", "error": null},
      "audit": {
        "available": true,
        "prompt_meta": { /* §6.4 描述的完整对象 */ },
        "derived": {
          "consistency_checks": [
            {"name": "activated_subset_of_layers", "pass": true, "severity": "high"},
            {"name": "explicit_in_system_section", "pass": true, "severity": "high"},
            ...
          ]
        }
      }
    }
  ]
}
```

### 2.3 `available=false` 的兼容

老历史会话（v1.2 之前）没有 `prompt_meta`。此时 `audit.available=false`，评估只能跑 `_rule_error_reply` 这一条降级规则，其它跳过。

---

## 3. 模块结构

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend/app/services/chat_audit.py` | 689 | 把 conversation + messages 整理为 `chat_audit_v1`，跑 L0 `consistency_checks` |
| `backend/app/services/eval_chat_review.py` | 675 | L1 启发式规则集 + `review_turn` / `review_audit_pack` |
| `backend/app/routers/conversations.py` | — | `GET /api/conversations/{id}/audit` |
| `backend/app/routers/eval.py` | — | `POST /api/eval/chat-review/{conv_id}` |
| `frontend/src/views/EvalView.vue` | — | 主视图，左 Tab「合成评测」/「线上聊天记录」 |
| `frontend/src/stores/eval.ts` | — | 评估状态 + 调用后端 |

---

## 4. L0：结构自检（`consistency_checks`）

`chat_audit.run_consistency_checks(prompt_meta, reply)` 跑 7+ 条规则，输出 `[{name, pass, severity, msg}, ...]`。

| 规则 | severity | 检测点 |
|---|---|---|
| `activated_subset_of_layers` | high | `activated` 中的每条都能在 `context_layers` 某层找到，不出现凭空激活 |
| `explicit_in_system_section` | high | `usage='explicit_ok'` 的条目，文本片段确实出现在 system prompt 的「可显性引用」段 |
| `background_in_system_section` | medium | `background_only` 条目出现在「背景信息」段（v1.2 瘦身后已弱化，因为空块不渲染） |
| `cap_respected` | medium | explicit 数量 ≤ `route.max_explicit_memories` |
| `sensitive_no_explicit_episodic` | high | sensitive_mode 下 `relevant_memories` 不该是 explicit_ok |
| `casual_no_career_leak` | medium | `casual`/`knowledge_task` 时 `stable_profile` 不带职业/行业字段 |
| `snapshot_stats_present` | low | 是否带 `snapshot_stats`（v1.2 新增字段） |

L0 一旦 high 失败，整 turn 直接 `final_status=bad`，归因兜底 `[C, D]`（Composer / Context Builder 层有 bug）。

---

## 5. L1：启发式规则（v1.2 共 10 条）

所有规则签名 `_rule_xxx(...) -> {"name", "status", "severity", "msg", "attribution"}`。

| 规则 | severity | 触发 | attribution |
|---|---|---|---|
| `error_reply` | high | reply 为空或 `[连接出错...]` | E（chat 链路） |
| `recall_for_challenge` | high | `intent==memory_challenge` 时，reply 是否真引用了 activated 池中实体 | A（router）/ B（context） |
| `recall_for_self_summary` | high | `intent==self_summary` 时，reply 覆盖率是否足够 | A / B |
| `data_vs_activation_gap` | medium | DB 里有但本轮没激活的关键实体（用 user_msg token 比 `context_layers` 全集） | B |
| `generic_reply` | medium | 任何 intent 下，reply 命中"听起来你/会更好地…"等通用客服腔 | D（prompt composer 没约束住）|
| `reply_off_topic` | medium | reply 关键 token 与 user_msg 关键 token 的覆盖率 < 30% | E |
| `correction_persisted` | high | 上一轮 `intent==correction` 中被纠正的实体/关键词，本轮 `context_layers` 仍含 | F（correction pipeline）|
| `correction_no_concrete_ack` | high | `intent==correction` 时 reply 没第一人称承认 / 命中 `_CORRECTION_DEFLECT_PHRASES` | D |
| `fabrication_under_challenge` | high | `intent==memory_challenge` 时 reply 含池外实体（来自 `_ENTITY_TOKENS` 表） | D |

### 5.1 三条新增规则（v1.2）

#### 5.1.1 `correction_persisted`

跨轮规则：`review_turn(turn, prev_turn)` 用 `prev_turn`。

```python
prev_user = (prev_turn["input"]["user_message"] or "")
# 找出上一轮用户纠正语中提到的"该被忘掉"的实体
banned_hint_tokens = {w for w in _ENTITY_TOKENS if w in prev_user} | recall_keywords
# 看本轮 context_layers 全集
context_text = " ".join(...all layers...)
hit = [t for t in banned_hint_tokens if t in context_text]
if hit:
    return fail(msg=f"上一轮纠正的实体仍出现：{hit}", attribution=["F"])
```

#### 5.1.2 `correction_no_concrete_ack`

```python
if intent != "correction":
    return skip
ack_signals = ["我记错", "我搞混", "是我没分清", "我把…当成"]  # 第一人称承认
deflects   = ["请你再告诉我一次", "可以告诉我吗", "我会更新记忆", ...]
if not has_any(reply, ack_signals) or has_any(reply, deflects):
    return fail(attribution=["D"])
```

#### 5.1.3 `fabrication_under_challenge`

```python
if intent != "memory_challenge":
    return skip
mentioned = {w for w in _ENTITY_TOKENS if w in reply}
in_pool   = {w for w in mentioned if w in any_layer_text(context_layers)}
fabricated = mentioned - in_pool
if fabricated:
    return fail(msg=f"reply 含池外实体：{fabricated}", attribution=["D"])
```

---

## 6. 归因体系（root cause attribution）

每条规则带 `attribution: list[str]`，最终聚合为一组建议归因。归因码：

| 码 | 含义 |
|---|---|
| `A` | Memory Router 意图识别错 |
| `B` | Memory Context Builder 召回/过滤错 |
| `C` | 三层数据本身有质量问题（脏画像、错事件） |
| `D` | Prompt Composer 没约束住 LLM 输出 |
| `E` | Chat 模型链路错（超时、空回复） |
| `F` | Correction Pipeline 没生效 |

报告里展示为 `"suggested_root_cause": ["B", "D"]`，便于研发定位修哪一层。

---

## 7. Review 输出包 `eval_review_v1`

```jsonc
{
  "version": "eval_review_v1",
  "conversation_id": "...",
  "user_id": "...",
  "reviewed_at": "2026-06-06T22:54:00+08:00",
  "summary": {
    "total_turns": 8,
    "good": 5, "ok": 1, "bad": 2, "skip": 0,
    "l0_high_fail": 1, "l0_medium_fail": 2,
    "high_severity_rule_hits": 3
  },
  "turns": [
    {
      "turn_id": "t3",
      "input": {...}, "output": {...},
      "review": {
        "l0_status": "fail",          // pass / warn / fail / skip
        "l0_high_fail": 1, "l0_medium_fail": 0,
        "l1_status": "fail",          // pass / warn / fail
        "rules": [
          {"name": "correction_persisted", "status": "fail",
           "severity": "high",
           "msg": "上一轮纠正的实体「小鹏」仍出现在本轮 background_only",
           "attribution": ["F"]}
        ],
        "suggested_root_cause": ["F"],
        "final_status": "bad",
        "snapshot_status": "at_turn"  // at_turn / context_only / unavailable
      }
    }
  ]
}
```

`snapshot_status` 取值：
- `at_turn`：用了**当时**的 `snapshot_stats`，最准
- `context_only`：只有 `context_layers`，无 stats（v1.2 之前的会话）
- `unavailable`：连 `prompt_meta` 都没有，只能跑降级规则

---

## 8. API

### 8.1 导出 audit（只读，无成本）

```
GET /api/conversations/{conv_id}/audit
→ 返回 chat_audit_v1 JSON，附带 L0 consistency_checks
```

### 8.2 跑 review

```
POST /api/eval/chat-review/{conv_id}
→ 返回 eval_review_v1 JSON

GET /api/eval/chat-review/{conv_id}
→ 返回上次跑过的 review（若有缓存）
```

### 8.3 触发与缓存策略

| 场景 | 行为 |
|---|---|
| 用户首次点「评估」 | POST 跑 review，结果缓存到内存 / 临时文件 |
| 用户再次点同一会话 | 优先 GET 返回缓存 |
| 用户在该会话又聊了新轮次 | 缓存失效，下次 POST 重跑 |
| 用户在 EvalView 切换会话 | 不自动跑，必须用户点按钮 |

---

## 9. 前端

### 9.1 EvalView 左侧 Tab

```text
┌─────────────────────┐  ┌─────────────────────────────┐
│ ▼ 合成评测           │  │                              │
│   Smoke 20           │  │   评估报告区                  │
│   Full 50            │  │                              │
│ ▼ 线上聊天记录       │  │   - 会话级 summary            │
│   ▶ 2026-06-06       │  │   - 逐 turn final_status      │
│     [对话标题1]      │  │   - 规则命中 + 归因           │
│     [对话标题2]      │  │   - 一键导出 JSON             │
│     [对话标题3]      │  │                              │
│   ▶ 2026-06-05       │  │                              │
│     ...              │  │                              │
└─────────────────────┘  └─────────────────────────────┘
```

### 9.2 单 turn 可视化

每条 turn 卡片含：
- 用户消息 + 助手回复
- final_status 色标（good/ok/bad/skip）
- 命中规则 chip 列表（按 severity 配色）
- 归因码 badge
- 可展开 raw prompt_meta（debug 用）

---

## 10. 与合成评测的串通

发现真实聊天的失败模式 → 在 `backend/eval/full_cases.json` 增加一条 case：

```jsonc
{
  "id": "A-x07",
  "user": "你忘了我们家养什么了吗",
  "expected": {
    "intents": ["memory_challenge"],
    "must_contain": ["猫"],
    "forbidden": ["小鹏", "我没注意到", "你可以告诉我"]
  },
  "tags": ["fabrication_check", "from_real_chat"]
}
```

下次跑 Full 50 时就能自动回归。

---

## 11. 已知限制 & 后续规划

| 项 | v1.2 现状 | 后续 |
|---|---|---|
| 启发式规则数量 | 10 | v1.3 目标 20+ |
| LLM Judge | 未启用 | v1.3 对启发式 fail 的 turn 调小模型做语义判分 |
| 规则覆盖率 | 局限于 audit_pack 字段 | v1.3 增加"用户后续消息"信号（如下一轮用户表达不满） |
| 报告对比 | 单会话 | v1.3 跨会话 trend / 用户级评分卡 |
| 实体词典 `_ENTITY_TOKENS` | 硬编码 | v1.3 从 user_profile.social.relationships 动态加载 |
