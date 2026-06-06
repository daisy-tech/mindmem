"""Layer 2：小模型 intent 分类（Memory Router v1.5）。"""
from __future__ import annotations

import json
import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

from app.services.memory_router import ChatTurn

logger = logging.getLogger(__name__)

DASHSCOPE_BASE_URL = os.getenv(
    "OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
INTENT_MODEL = os.getenv("INTENT_MODEL", os.getenv("CHAT_MODEL", "qwen3.7-plus"))
INTENT_MODEL_TEMPERATURE = float(os.getenv("INTENT_MODEL_TEMPERATURE", "0"))
INTENT_CLASSIFIER_ENABLED = (
    os.getenv("INTENT_CLASSIFIER_ENABLED", "true").lower() == "true"
)
INTENT_CLASSIFIER_TIMEOUT_SEC = float(os.getenv("INTENT_CLASSIFIER_TIMEOUT_SEC", "8"))
# Qwen3.5+ Plus 是混合思考模型，默认会先思考再说话——分类器在主链路上，必须关闭
INTENT_ENABLE_THINKING = (
    os.getenv("INTENT_ENABLE_THINKING", "false").lower() == "true"
)

VALID_INTENTS = frozenset(
    {
        "correction",
        "memory_challenge",
        "self_summary",
        "knowledge_task",
        "emotional_support",
        "relationship_topic",
        "plan_followup",
        "preference_request",
        "casual",
    }
)

CLASSIFIER_SYSTEM = """你是 MindMem 的记忆路由分类器，不是聊天助手。
任务：根据【当前用户消息】和【最近对话】，判断本轮应使用的「记忆策略 intent」。

intent 含义（只选一个主 intent）：
- correction：用户在纠正、删除、要求不要再提某记忆
- memory_challenge：用户在质问你是否记得/应该知道（"你不是知道吗" "你怎么不记得"）
- self_summary：用户在问你对 ta 的整体了解、重要的人、聊过什么
- knowledge_task：用户在问通用知识、工具、代码、天气等，与个人经历无关
- emotional_support：用户在表达压力、焦虑、难受、失眠等情绪，主要需要承接
- relationship_topic：话题围绕某具体的人/关系/家庭事务
- plan_followup：话题围绕计划、待办、面试、HR、时间节点、跟进进度
- preference_request：用户在要推荐、建议、怎么选、怎么安排（依赖个人偏好）
- casual：普通闲聊、问候、无明确记忆策略需求

判定优先级（多个 intent 都成立时按以下顺序选）：
1. 当消息主语是【已知关系人】（她/他/老婆/儿子/同事 + 上下文已锚定为某人）→
   优先 relationship_topic；只有用户在说"自己（我）累/烦/睡不着"时才归 emotional_support。
   例："她最近还是很累"（上文聊老婆）→ relationship_topic（不是 emotional_support）
2. 当消息含【时间锚点】（明天/下周/那天/下周一/那场/那次）+【询问/确认】
   （记得吧/还记得吗/你说过的/你看到了吗）→ 优先 plan_followup。
   只有当用户用质问/责备语气（你怎么不记得、你不是知道吗）时才归 memory_challenge。
   例："下周一那场你记得吧" → plan_followup（不是 memory_challenge）
3. 当用户在【明确计划场景下】表达紧张/担心/期待 → optional 可在
   plan_followup 和 emotional_support 之间，倾向选 plan_followup（让后端加载计划记忆）。
   例：上文"下周一有面试"，本轮"明天有点紧张" → plan_followup
4. 含纠错语义（你记错了 / 别再提）一律 correction，最高优先。
5. 短问候、无明确信号 → casual。

输出规则：
1. 只输出 JSON：{"intent":"...", "confidence":0.0-1.0, "rationale":"..."}
2. 不要输出聊天回复
3. 结合上下文理解短句和代词（「她」「那家公司」「那场」）
4. confidence 表示你对主 intent 的确信度（0-1 浮点数）
5. rationale 控制在 60 字内，说明判定依据

禁止：编造用户未提供的事实。

【示例 1】
最近对话：
  user: 老婆在家带小宇，我基本帮不上忙。
  assistant: 你这段时间确实很难两边都顾到。
当前消息：她最近还是很累
输出：{"intent":"relationship_topic","confidence":0.9,"rationale":"主语\"她\"指代老婆，话题围绕妻子状态，应加载关系/家庭记忆"}

【示例 2】
最近对话：（无）
当前消息：下周一那场你记得吧
输出：{"intent":"plan_followup","confidence":0.85,"rationale":"\"下周一那场\"是时间锚点 + 计划事件，询问语气在确认而非质问"}

【示例 3】
最近对话：
  user: 下周一有个面试。
  assistant: 是什么岗位？
当前消息：明天有点紧张
输出：{"intent":"plan_followup","confidence":0.75,"rationale":"上文为面试计划，本轮紧张源于该计划，按 plan 加载记忆更有用"}

【示例 4】
最近对话：（无）
当前消息：你不是知道吗，我之前跟你说过
输出：{"intent":"memory_challenge","confidence":0.95,"rationale":"质问语气\"你不是知道吗\" + \"我之前说过\"，典型记忆挑战"}
"""


class IntentClassifierInput(BaseModel):
    message: str
    recent_history: list[ChatTurn] = Field(default_factory=list)
    relationship_keys: list[str] = Field(default_factory=list)
    profile_one_liner: str | None = None
    previous_intent: str | None = None


class IntentClassifierOutput(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


def _format_history(history: list[ChatTurn]) -> str:
    lines: list[str] = []
    for t in history[-6:]:
        role = "user" if t.role == "user" else "assistant"
        content = (t.content or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "（无）"


def _build_user_prompt(inp: IntentClassifierInput) -> str:
    parts = [
        "【最近对话】",
        _format_history(inp.recent_history),
        "",
        "【当前用户消息】",
        inp.message.strip(),
    ]
    if inp.relationship_keys:
        parts.extend(["", "【已知关系人】", ", ".join(inp.relationship_keys[:20])])
    if inp.profile_one_liner:
        parts.extend(["", "【用户摘要】", inp.profile_one_liner])
    if inp.previous_intent:
        parts.extend(["", "【上一轮 intent】", inp.previous_intent])
    return "\n".join(parts)


def _parse_classifier_json(raw: str) -> IntentClassifierOutput:
    data = json.loads(raw)
    intent = str(data.get("intent") or "casual").strip()
    if intent not in VALID_INTENTS:
        intent = "casual"
    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(data.get("rationale") or "")[:120]
    return IntentClassifierOutput(intent=intent, confidence=confidence, rationale=rationale)


async def classify_intent(inp: IntentClassifierInput) -> IntentClassifierOutput:
    """调用小模型做 intent 分类。"""
    import openai

    client = openai.AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=DASHSCOPE_BASE_URL,
        timeout=INTENT_CLASSIFIER_TIMEOUT_SEC,
    )
    resp = await client.chat.completions.create(
        model=INTENT_MODEL,
        temperature=INTENT_MODEL_TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": _build_user_prompt(inp)},
        ],
        extra_body={"enable_thinking": INTENT_ENABLE_THINKING},
    )
    raw = resp.choices[0].message.content or "{}"
    return _parse_classifier_json(raw)


def resolve_intent_with_fallback(
    out: IntentClassifierOutput,
    message: str,
    previous_intent: str | None,
) -> tuple[str, float, bool, Literal["classifier", "inherited", "low_confidence_casual"]]:
    """根据 confidence 与上下文继承决定最终 intent。"""
    msg = (message or "").strip()
    conf = out.confidence
    intent = out.intent

    if conf >= 0.50:
        return intent, conf, conf < 0.75, "classifier"

    if previous_intent and previous_intent in VALID_INTENTS and len(msg) <= 16:
        return previous_intent, conf, True, "inherited"

    return "casual", conf, True, "low_confidence_casual"
