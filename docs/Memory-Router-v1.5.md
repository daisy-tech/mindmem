# Memory Router v1.5 技术方案

> 方案代号：**Hybrid Router A**（规则 + 小模型意图分类）  
> 版本：1.2 | 日期：2026-06-06 | 状态：Released  
> 关联文档：[PRD.md §2.7](./PRD.md) · [TDD.md §6.2](./TDD.md) · [Correction-Pipeline.md](./Correction-Pipeline.md) · [Real-Chat-Eval.md](./Real-Chat-Eval.md)

---

## 1. 背景与动机

### 1.1 v1 现状

Memory Router v1 采用 **规则打分 + 关键词匹配 + 优先级覆盖**（`backend/app/services/memory_router.py`），不调用独立 LLM。Layer 3 策略查表（`load_layers` / `memory_depth` / `event_policy`）与 PRD §2.7 一致，设计合理且可解释。

### 1.2 v1 暴露的问题（评测证据）

在合成用户 A（persona_a_zhang）灌库后的 Full 50 评测（`20260531_141805_full`）中：

| 指标 | 结果 |
|------|------|
| 通过率 | 30/50（60%） |
| 主失败原因 | **intent 路由错误**（约 19/20 条失败） |
| 记忆召回 | intent 正确时，关键词命中率良好 |
| 典型失败 | 「你对我印象怎么样」「解释一下 OKR」「HR 那边还没信儿」→ 误回退 `casual` |

结论：**瓶颈已从「记忆库 Ground Truth」转向「Layer 2 意图识别泛化能力」**。继续堆关键词无法覆盖自然语言的同义改写，且维护成本指数上升。

### 1.3 v1.5 目标

- **保留** Layer 1 硬规则与 Layer 3 策略查表（可解释、可评测、产品约束稳定）
- **升级** Layer 2：用小模型做 **intent 分类**，替代关键词穷举
- **不改动** MemoryContextBuilder、Prompt 编排、四层记忆存储的主链路
- **对齐** 评测实验室：批量评测仍用 persona_a_zhang；单条调试仍用登录用户真实记忆

### 1.4 非目标（v1.5 不做）

- 不用 LLM 直接输出完整 `MemoryRoute`（避免黑盒、不稳定、与 Chat 模型职责混淆）
- 不扩展 intent 到 20+ 话题类（见 §4）
- 不替换 Chat 模型的对话理解与生成能力
- 不在 v1.5 首版实现「检索信号反推 intent」（可作 v1.6 增强）

---

## 2. 设计原则

| 原则 | 说明 |
|------|------|
| **策略与理解分离** | LLM 只判 `intent`（+ confidence）；记忆怎么用仍由 Layer 3 查表决定 |
| **硬规则优先** | `correction` 等安全/写操作类 intent 不被分类器覆盖 |
| **可解释** | Prompt 抽屉展示：`intent`、`confidence`、是否走硬规则、Layer 3 策略摘要 |
| **可评测** | L1 自动判分继续以 intent + 记忆策略为主；支持 `optional_intents` |
| **保守兜底** | 低置信度 → `casual` 或继承上一轮 intent（见 §6.4） |
| **成本可控** | 分类调用使用便宜、快、温度 0 的小模型；与 Chat 模型分离 |

---

## 3. 总体架构

```text
用户消息 + 最近 3–5 轮 history
        │
        ▼
┌───────────────────┐
│ Layer 1：硬规则    │  correction / 极短问候 / memory_challenge 强模式
│ （规则，零 LLM）   │  命中 → 直接定 intent，跳过 Layer 2
└─────────┬─────────┘
          │ 未命中
          ▼
┌───────────────────┐
│ Layer 2：意图分类  │  小模型 JSON 分类 → intent + confidence
│ （小 LLM）        │  输入：当前句 + 压缩上下文 + relationship_keys 摘要
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ Layer 3：策略查表  │  intent → load_layers / memory_depth / event_policy
│ （规则，零 LLM）   │  + 人格微调 max_explicit / sensitive_mode / query 构建
└─────────┬─────────┘
          │
          ▼
     MemoryRoute → MemoryContextBuilder → PromptComposer → Chat
```

与 v1 的 **唯一实质差异**：Layer 2 由「关键词打分」改为「小模型分类」；Layer 1 收窄、Layer 3 基本不变。

---

## 4. Intent 设计说明（9 类是否够用）

### 4.1 定位

9 个 intent **不是**「人类聊天的全面 taxonomy」，而是 **「长期记忆在本轮的使用策略标签」**（Memory Use Policy Labels）。

分类依据是记忆系统的四个维度：

1. 要不要用个人记忆？用多少？
2. 打开哪几层？（profile / relationships / events / episodic）
3. 每条记忆怎么用？（explicit / background / follow_up / avoid）
4. 是否触发记忆写操作？（correction）

### 4.2 Intent 枚举（与 v1 保持一致）

| intent | 记忆策略摘要 |
|--------|--------------|
| `correction` | 记忆修正/删除/纠错，最高优先级 |
| `memory_challenge` | 用户质问「你应该知道」，可谨慎引用记忆 |
| `self_summary` | 用户要 Bot 总结「你对我了解什么」 |
| `knowledge_task` | 工具/通用知识，默认不用私人事实 |
| `emotional_support` | 情绪承接，痛点仅 background |
| `relationship_topic` | 锚在人/关系/家庭，强 relationships |
| `plan_followup` | 计划、待办、时间线、跟进 |
| `preference_request` | 需要个人偏好给建议 |
| `casual` | 闲聊/问候/兜底，minimal 记忆 |

**不新增第 10 个 intent 的判据**：若两种说法共享同一套 Layer 3 策略，则共用 intent，用 `optional_intents` 或 tags 处理边界 case。

### 4.3 可选副信号（v1.5 预留，首版可不实现）

```json
{
  "intent": "plan_followup",
  "confidence": 0.86,
  "tags": ["has_named_entity", "emotional_hint"]
}
```

`tags` 仅微调 Layer 3 参数（如 emotional_hint → pain_point 更保守），**不改变主查表 intent**。

---

## 5. Layer 1：硬规则（保留并收窄）

### 5.1 职责

处理 **必须确定性、不可被分类器覆盖** 的场景。

### 5.2 硬规则清单

| 规则 ID | 条件 | 输出 intent | 说明 |
|---------|------|-------------|------|
| R1 | 命中 correction 强模式 | `correction` | 见 §5.3 |
| R2 | 命中 memory_challenge 强模式 | `memory_challenge` | 「你忘了」「你应该知道」等 |
| R3 | 极短问候（≤12 字 + 问候词）且无其它强信号 | `casual` | 「在吗」「早」 |
| R4 | （可选）用户显式 `/route-preview` 调试 | 走完整链路 | 评测实验室 |

**移除 / 弱化 v1 中交给 Layer 2 的规则**：self_summary、plan、emotional、relationship、knowledge 的大段关键词加分。

### 5.3 correction 强模式（示例）

```text
你记错 / 记错了 / 你弄错 / 纠正 / 更正 / 别再提 / 不要提 / 忘掉 / 删除记忆
不是…是…（年龄/职位等纠错句式，需结合正则，非裸「不是」）
```

`correction` 一旦命中 R1，**不调用** Layer 2 分类器。

### 5.4 输出

```python
class HardRuleResult(BaseModel):
    matched: bool
    intent: str | None = None
    rule_id: str | None = None
    reasons: list[str] = []
```

---

## 6. Layer 2：小模型意图分类

### 6.1 模型选型

| 项 | 建议 |
|----|------|
| 模型 | 与线上一致可读 `INTENT_MODEL` env，默认 `qwen-plus` 或 `qwen-turbo` |
| 温度 | **0**（或 0.1） |
| 最大 tokens | 输出 ≤ 150 tokens |
| 与 Chat 关系 | **独立 system prompt**，不与 MemoBot 聊天 prompt 混用 |
| 与 Judge 关系 | 评测 Judge 仍用 Chat 同模型；Router 分类器单独配置 |

环境变量：

```bash
INTENT_MODEL=qwen-plus          # 意图分类模型
INTENT_MODEL_TEMPERATURE=0
INTENT_CLASSifier_ENABLED=true  # false 时回退 v1 规则（便于 A/B）
```

### 6.2 分类器输入

```python
class IntentClassifierInput(BaseModel):
    message: str                          # 当前用户句
    recent_history: list[ChatTurn]        # 最近 3–5 轮，role + content
    relationship_keys: list[str]          # 已知关系人名，如 ["小雅","小宇","阿明"]
    profile_one_liner: str | None = None  # 可选："张明远，上海，研发总监"
    previous_intent: str | None = None    # 上一轮 intent（上下文继承，见 §6.4）
```

**不传入**：完整 system prompt、全部记忆正文、Chat 历史超长文本。

**上下文压缩格式**（拼进 user message）：

```text
【最近对话】
user: 老婆在家带小宇，我基本帮不上忙。
assistant: 你这段时间确实很难两边都顾到。

【当前用户消息】
她最近还是很累

【已知关系人】小雅, 小宇, 阿明
```

### 6.3 分类器输出（结构化 JSON）

```python
class IntentClassifierOutput(BaseModel):
    intent: Literal[
        "correction", "memory_challenge", "self_summary",
        "knowledge_task", "emotional_support", "relationship_topic",
        "plan_followup", "preference_request", "casual"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=120)  # 简短理由，供调试面板展示
```

**API 调用**：OpenAI 兼容 `chat.completions` + `response_format={"type":"json_object"}`（或 function calling），DashScope 兼容模式。

### 6.4 System Prompt（分类器专用）

```text
你是 MindMem 的记忆路由分类器，不是聊天助手。
任务：根据【当前用户消息】和【最近对话】，判断本轮应使用的「记忆策略 intent」。

intent 含义（只选一个主 intent）：
- correction：用户在纠正、删除、要求不要再提某记忆
- memory_challenge：用户在质问你是否记得/应该知道
- self_summary：用户在问你对 ta 的整体了解、重要的人、聊过什么
- knowledge_task：用户在问通用知识、工具、代码、天气等，与个人经历无关
- emotional_support：用户在表达压力、焦虑、难受、失眠等情绪，主要需要承接
- relationship_topic：话题围绕某具体的人/关系/家庭事务
- plan_followup：话题围绕计划、待办、面试、HR、时间节点、跟进进度
- preference_request：用户在要推荐、建议、怎么选、怎么安排（依赖个人偏好）
- casual：普通闲聊、问候、无明确记忆策略需求

规则：
1. 只输出 JSON：{"intent":"...", "confidence":0.0-1.0, "rationale":"..."}
2. 不要输出聊天回复
3. 结合上下文理解短句和代词（「她」「那家公司」）
4. 若多种 intent 都可能，选「对记忆加载策略影响最大」的一个
5. confidence 表示你对主 intent 的确信度

禁止：编造用户未提供的事实。
```

### 6.5 置信度与兜底

| confidence | 行为 |
|------------|------|
| ≥ 0.75 | 采用分类 intent |
| 0.50 – 0.74 | 采用分类 intent，标记 `low_confidence=true`，Prompt 抽屉展示警告 |
| < 0.50 | **兜底**：若存在 `previous_intent` 且当前句 ≤16 字 → 继承上一轮；否则 → `casual` |

**上下文继承（v1.5 建议实现）**：

```text
「嗯」「她呢」「后来呢」「那家公司有回音吗」
→ 若无硬规则命中，且句子短，继承 previous_intent 而非判 casual
```

实现：在 `MemoryRouteInput` 增加 `previous_intent`，由 `chat.py` 从会话状态传入。

### 6.6 失败与降级

| 场景 | 降级策略 |
|------|----------|
| 分类 API 超时（>800ms） | 回退 v1 关键词打分（保留旧函数 `score_intents_v1`） |
| JSON 解析失败 | 重试 1 次；仍失败则回退 v1 或 `casual` |
| `INTENT_CLASSifier_ENABLED=false` | 完全使用 v1 规则 |

---

## 7. Layer 3：策略查表（沿用 v1）

Layer 2 产出 `intent` 后，**逻辑与 v1 完全相同**：

```python
layers = LAYER_PRESETS[intent]
depth = DEPTH_BY_INTENT[intent]
event_policy = EVENT_POLICY_BY_INTENT[intent]
# + personality → max_explicit_memories
# + subjects 推断 → query 构建
# + sensitive_mode 判定
```

### 7.1 查表定义（不变）

见 `memory_router.py` 中 `LAYER_PRESETS`、`DEPTH_BY_INTENT`、`EVENT_POLICY_BY_INTENT` 及 TDD §6.2。

### 7.2 casual 禁词与 stable profile（v1.5 顺带修复）

评测 case A-c05：`casual` 时 stable profile 的 `industry` 含「云迹」触发 `forbidden_phrases_in_system`。

**策略（二选一，建议 A）**：

- **A**：`casual` + `minimal` 时，`_extract_stable_profile` 仅输出 name/location，**不输出 industry/公司名**
- **B**：L1 禁词检查只扫描「激活记忆区」，不扫描 stable profile 块

---

## 8. 代码结构与改造点

### 8.1 新增 / 修改文件

| 文件 | 变更 |
|------|------|
| `backend/app/services/intent_classifier.py` | **新增** Layer 2 分类器 |
| `backend/app/services/memory_router.py` | 重构 `route()`：Layer1 → Layer2 → Layer3 |
| `backend/app/services/memory_router_v1.py` | **可选** 旧规则打分迁出，作 fallback |
| `backend/app/routers/chat.py` | 传入 `previous_intent`；`prompt_meta.route` 增加 confidence |
| `backend/app/services/eval_runner.py` | L1 判分支持 `optional_intents` 加权；禁词 scope 调整 |
| `frontend/src/components/PromptChainPanel.vue` | 展示 confidence、low_confidence、rule_id |

### 8.2 核心接口

```python
# intent_classifier.py
async def classify_intent(inp: IntentClassifierInput) -> IntentClassifierOutput: ...

# memory_router.py
def apply_hard_rules(inp: MemoryRouteInput) -> HardRuleResult: ...

def route(inp: MemoryRouteInput) -> MemoryRoute:
    hard = apply_hard_rules(inp)
    if hard.matched:
        intent = hard.intent
        reasons = hard.reasons
    else:
        out = await classify_intent(...)  # 或 sync 包装
        intent = resolve_intent_with_fallback(out, inp.previous_intent)
        reasons = [f"classifier: {out.rationale} (conf={out.confidence})"]
    return _build_route_from_intent(intent, inp, reasons)
```

### 8.3 MemoryRoute 扩展字段

```python
class MemoryRoute(BaseModel):
    intent: str
    intent_confidence: float | None = None      # 新增
    intent_source: Literal["hard_rule", "classifier", "fallback_v1", "inherited"]  # 新增
    ...
```

`prompt_meta.route` 同步暴露，供评测实验室穿透。

---

## 9. 与评测体系的关系

### 9.1 批量评测（persona_a_zhang）

- 灌库：**手动一次**，不自动灌库（已实现）
- 跑批：仍对 eval 固定用户；**不**对登录用户
- 报告字段：已有 `persona_ref`、`eval_user_id`

### 9.2 L1 判分调整（建议与 v1.5 同步）

| 项 | v1 | v1.5 建议 |
|----|-----|-----------|
| intent | 单 gold intent | 保留 gold + 扩大 `optional_intents` 使用 |
| 硬 intent | — | `correction` 必须精确匹配 |
| soft intent | — | `casual` vs `emotional_support` 边界 case 可 accept 集合 |
| 禁词 | 扫整段 system | casual 不扫 industry（§7.2） |
| 门禁 | 60% | 首版目标 **≥75%**（Full 50，无 Chat） |

### 9.3 评测集扩展（后续）

- 每个 intent 增加 **5–10 条 paraphrase**，测 Layer 2 泛化，而非只测一条 gold 句
- Smoke 20 保留为 PR 快检；Full 50 作 nightly

---

## 10. 性能与成本

| 指标 | 目标 |
|------|------|
| 分类延迟 P95 | < 500ms（国内 DashScope） |
| 额外 token | 输入 ~300–600 tokens/轮 |
| 失败降级 | 不阻塞聊天；fallback ≤ 50ms（v1 规则） |
| 缓存 | v1.5 不做；v1.6 可对「相同 message+history hash」短缓存 60s |

---

## 11. 上线与回滚

### 11.1 发布步骤

1. 部署 `intent_classifier.py` + 重构 `route()`，`INTENT_CLASSIFIER_ENABLED=false` 默认关
2. ECS 灌库 persona_a_zhang，跑 Full 50 基线（v1 规则）
3. 开启 `INTENT_CLASSIFIER_ENABLED=true`，再跑 Full 50 对比
4. 前端 Prompt 抽屉确认 confidence 展示
5. 达标后默认开启；保留 env 一键回滚

### 11.2 回滚

```bash
INTENT_CLASSIFIER_ENABLED=false
```

立即回到 v1 关键词路由，无需改数据库。

### 11.3 A/B 对比（可选）

报告 JSON 增加 `router_version: "v1" | "v1.5"`，评测实验室可按 run 对比 pass_rate。

---

## 12. 验收标准

### 12.1 功能

- [ ] Layer 1 硬规则：`correction` / 极短问候行为与 v1 一致或更准
- [ ] Layer 2：141805 中 15 条「误 casual」case **至少修复 10 条**
- [ ] Layer 3：intent 相同时，`load_layers` / `event_policy` 与 v1 查表一致
- [ ] Prompt 抽屉展示：`intent`、`confidence`、`intent_source`、`reasons`
- [ ] 分类失败可降级，聊天链路不 500

### 12.2 评测（persona_a_zhang，run_chat=false）

- [ ] Full 50 pass_rate ≥ **75%**
- [ ] `relationship_topic` ≥ **6/7**
- [ ] `correction` ≥ **4/5**
- [ ] `casual` 禁词 case A-c02–A-c05 仍通过（含 A-c05 修复）

### 12.3 非功能

- [ ] 分类 P95 延迟 < 500ms
- [ ] 环境变量可关闭分类器回退 v1

---

## 13. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 分类器与 Chat 同模型，bias | 独立 system prompt；分类输入不含完整 memory prompt |
| 短句仍判 casual | previous_intent 继承 + 上下文拼进分类输入 |
| 多 intent 混合句 | 主 intent + optional_intents 评测；tags 微调 |
| 成本上升 | turbo 模型 + 输入压缩；仅 Layer 2 调用 |
| 回归 v1 已通过 case | 发布前跑 Smoke 20 diff；保留 fallback |

---

## 14. 版本演进

| 版本 | 内容 |
|------|------|
| **v1** | 全规则（当前生产） |
| **v1.5** | 本方案：硬规则 + 小模型分类 + 策略查表 |
| v1.6 | embedding 原型兜底；检索信号辅助；intent 短缓存 |
| v2 | 多 intent tags；会话级 route 状态机；在线学习 paraphrase |

---

## 15. 附录

### 15.1 141805 失败 case → v1.5 预期

| Case | v1 实际 | v1.5 预期 intent |
|------|---------|------------------|
| A-s03~05 | casual | self_summary |
| A-pf03~05 | casual | plan_followup |
| A-k02,04,05 | casual | knowledge_task |
| A-x02~05 | casual/relationship | correction |
| A-e02 | casual | emotional_support |
| A-r02 | emotional_support | relationship_topic（或 optional 双 accept） |
| A-c05 | sys 禁词 | Layer 3 profile 裁剪修复 |

### 15.2 参考实现位置

| 模块 | 路径 |
|------|------|
| Router v1 | `backend/app/services/memory_router.py` |
| Context 构建 | `backend/app/services/memory_context.py` |
| Chat 入口 | `backend/app/routers/chat.py` |
| 评测 runner | `backend/app/services/eval_runner.py` |
| Persona 灌库 | `backend/app/services/eval_persona.py` |
| Full cases | `backend/eval/full_cases.json` |

### 15.3 文档修订记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-05-31 | 1.0 | 初稿：Hybrid Router A（规则 + 小模型分类） |
| 2026-06-06 | 1.1 | 配合记忆纠错管线与评估系统的小幅修订：① Layer 2 失败降级阈值上调至 conf<0.5；② query 构建只取最近 1 句 user 消息，避免历史稀释；③ `MemoryRoute` 增加 `router_version` 字段（写入 prompt_meta）便于评测分桶；④ `correction` 触发后 chat.py 跳过 extract_*（见 Correction-Pipeline §2.2）|
| 2026-06-06 | 1.2 | LLM 全链路升级到 Qwen3.7：`INTENT_MODEL=qwen3.7-plus`，并显式 `extra_body={"enable_thinking": False}`；与 Chat `qwen3.7-max` 隔离；分类器延迟保持 P95 < 500ms |
| 2026-06-06 | 1.2 | Prompt Composer 瘦身：去掉 `BACKGROUND_USAGE_RULES`；空块不渲染；本轮规则去掉调试元信息；intent guide 平均压缩 50% → 平均 prompt 节省 ~45% tokens（详见 TDD §6.4） |
